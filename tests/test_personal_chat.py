from __future__ import annotations

import io
import json
import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import BaseHTTPRequestHandler
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import lark_oapi as lark
from lark_oapi.api.im.v1 import ListChatResponse, ListMessageResponse, ReplyMessageResponse, ReplyMessageRequest
from lark_oapi.core.token.auth import verify

from agents.speaker_agent import main as speaker
from agents.speaker_agent.adapters.feishu.personal import PersonalPoller
from agents.speaker_agent.domain.intake import IntakeDecision
from agents.speaker_agent.orchestration.personal import PersonalIntake
from agents.speaker_agent.orchestration.workspace import SpeakerWorkspace
from agents.speaker_agent.persistence.personal import PersonalInbox
from shared.feishu.auth.oauth import UserOAuth, request_json
from shared.feishu.config import FeishuConfig
from shared.feishu.client import FeishuClient
from shared.feishu.errors import FeishuApiError
from shared.feishu.messages import MessageClient
from shared.feishu.personal import PersonalChat, PersonalChatClient, PersonalMessage, message_text, user_options
from shared.llm.contracts import ModelError
from test_agent import settings
from test_ledger_sources import MemoryWorkspace
from helpers import required


class OAuthTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.oauth = UserOAuth(FeishuConfig("cli_test", "fixture-secret"), self.root)
        self.record = {"app_id": "cli_test", "open_id": "ou_self", "name": "测试用户",
                       "access_token": "test-user-token", "refresh_token": "test-refresh-token", "expires_at": 0}

    def test_store_encrypts_windows_secrets_and_round_trips(self):
        self.oauth.store.save(self.record)
        self.assertEqual(self.oauth.store.load(), self.record)
        if os.name == "nt":
            self.assertNotIn(b"test-user-token", self.oauth.store.path.read_bytes())
            self.assertTrue(self.oauth.store.path.read_bytes().startswith(b"DPAPI\n"))

    def test_missing_login_and_app_mismatch_fail_without_network(self):
        with patch("shared.feishu.auth.oauth.request_json") as request:
            with self.assertRaisesRegex(RuntimeError, "--login"):
                self.oauth.access_token()
            self.oauth.store.save({**self.record, "app_id": "different"})
            with self.assertRaisesRegex(RuntimeError, "不匹配"):
                self.oauth.access_token()
            request.assert_not_called()

    def test_concurrent_refresh_rotates_once_and_persists(self):
        self.oauth.store.save(self.record)
        response = {"access_token": "new-user", "refresh_token": "new-refresh", "expires_in": 7200}
        with patch("shared.feishu.auth.oauth.request_json", return_value=response) as request:
            with ThreadPoolExecutor(max_workers=4) as pool:
                self.assertEqual(list(pool.map(lambda _: self.oauth.access_token(), range(8))), ["new-user"] * 8)
            request.assert_called_once()
            self.assertEqual(request.call_args.kwargs["payload"]["grant_type"], "refresh_token")
        self.assertEqual(self.oauth.store.load()["refresh_token"], "new-refresh")
        self.assertEqual(UserOAuth(self.oauth.config, self.root).access_token(), "new-user")

    def test_uncertain_refresh_is_not_replayed_after_restart(self):
        self.oauth.store.save(self.record)
        with patch("shared.feishu.auth.oauth.request_json", side_effect=RuntimeError("网络中断")) as request:
            with self.assertRaises(RuntimeError):
                self.oauth.access_token()
            with self.assertRaisesRegex(RuntimeError, "不确定"):
                UserOAuth(self.oauth.config, self.root).access_token()
            request.assert_called_once()
        self.assertEqual(self.oauth.store.load()["refresh_token"], self.record["refresh_token"])

    def test_account_switch_is_rejected_for_bound_provider(self):
        self.oauth.store.save({**self.record, "expires_at": time.time() + 7200})
        self.oauth.expected_user = "another-user"
        with self.assertRaisesRegex(RuntimeError, "账号已变化"):
            self.oauth.access_token()

    def test_transport_does_not_expose_secret_error_body(self):
        from urllib.error import HTTPError
        error = HTTPError("https://example.test", 403, "fixture-secret", Message(), io.BytesIO(b"secret-body"))
        with patch("shared.feishu.auth.oauth.urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                request_json("https://example.test", payload={"secret": "fixture-secret"})
        self.assertNotIn("fixture-secret", str(caught.exception))
        self.assertNotIn("secret-body", str(caught.exception))

    def test_login_validates_state_and_pkce_without_real_browser_or_network(self):
        browser_urls: list[str] = []
        requests: list[dict] = []
        test = self

        class Server:
            def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler]):
                test.assertEqual(address, ("127.0.0.1", 8765))
                self.handler = handler

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def handle_request(self):
                state = parse_qs(urlsplit(browser_urls[0]).query)["state"][0]
                callback = object.__new__(self.handler)
                callback.wfile = io.BytesIO()
                callback.send_error = Mock()
                callback.send_response = Mock()
                callback.send_header = Mock()
                callback.end_headers = Mock()
                callback.path = "/callback?state=wrong&code=bad"
                getattr(callback, "do_GET")()
                callback.send_error.assert_called_once()
                callback.path = f"/callback?state={state}&code=test-code"
                getattr(callback, "do_GET")()
                callback.send_response.assert_called_once_with(200)

        def response(url: str, *, payload: dict | None = None, token: str = "") -> dict:
            if payload:
                requests.append(payload)
                return {"access_token": "login-user", "refresh_token": "login-refresh", "expires_in": 7200}
            return {"data": {"open_id": "ou_self", "name": "测试用户"}}

        with patch("shared.feishu.auth.oauth.HTTPServer", Server), \
             patch("shared.feishu.auth.oauth.webbrowser.open", side_effect=lambda url: browser_urls.append(url)), \
             patch("shared.feishu.auth.oauth.request_json", side_effect=response), patch("builtins.print"):
            self.oauth.login()
        self.assertEqual(requests[0]["code"], "test-code")
        self.assertTrue(requests[0]["code_verifier"])
        query = parse_qs(urlsplit(browser_urls[0]).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertIn("offline_access", query["scope"][0])
        self.assertEqual(self.oauth.store.load()["open_id"], "ou_self")


class PersonalApiTests(unittest.TestCase):
    def setUp(self):
        self.sdk = Mock()
        self.tokens = Mock()
        self.tokens.access_token.return_value = "fixture-user"
        self.api = PersonalChatClient(self.sdk, self.tokens)
        self.chat = PersonalChat("oc_peer", "ou_peer")

    def test_lists_all_pages_and_only_human_p2p_with_explicit_user_identity(self):
        self.sdk.im.v1.chat.list.side_effect = [
            ListChatResponse({"code": 0, "data": {"has_more": True, "page_token": "next", "items": [
                {"chat_id": "oc_bot", "chat_mode": "p2p", "p2p_target_type": "bot", "p2p_target_id": "bot"}]}}),
            ListChatResponse({"code": 0, "data": {"has_more": False, "items": [
                {"chat_id": "oc_peer", "chat_mode": "p2p", "p2p_target_type": "user", "p2p_target_id": "ou_peer"}]}}),
        ]
        self.assertEqual(self.api.chats(), [self.chat])
        for call in self.sdk.im.v1.chat.list.call_args_list:
            request, option = call.args
            self.assertEqual(request.types, "p2p")
            self.assertEqual(request.token_types, {lark.AccessTokenType.USER})
            self.assertEqual(option.user_access_token, "fixture-user")

    def test_message_pages_filter_self_bots_deleted_and_media(self):
        def message(mid: str, sender_id: str = "ou_peer", **extra):
            return {"message_id": mid, "msg_type": "text", "create_time": "11000",
                    "sender": {"id": sender_id, "id_type": "open_id", "sender_type": "user"},
                    "body": {"content": '{"text":"支持导出"}'}, **extra}
        self.sdk.im.v1.message.list.side_effect = [
            ListMessageResponse({"code": 0, "data": {"has_more": True, "page_token": "p2", "items": [
                message("self", "ou_self"), message("deleted", deleted=True), message("image", msg_type="image"),
                message("bot", sender={"id": "ou_peer", "id_type": "open_id", "sender_type": "app"})]}}),
            ListMessageResponse({"code": 0, "data": {"has_more": False, "items": [message("incoming")]}}),
        ]
        self.assertEqual(self.api.messages(self.chat, 10, 20), [PersonalMessage("incoming", "oc_peer", "ou_peer", "支持导出", 11000)])
        request = self.sdk.im.v1.message.list.call_args.args[0]
        self.assertEqual((request.start_time, request.end_time), ("10", "20"))

    def test_pagination_failure_does_not_return_partial_messages(self):
        self.sdk.im.v1.message.list.return_value = ListMessageResponse({"code": 0, "data": {"has_more": True, "page_token": "same"}})
        with self.assertRaisesRegex(FeishuApiError, "分页"):
            self.api.messages(self.chat, 10, 20)
        self.assertEqual(self.sdk.im.v1.message.list.call_count, 2)

    def test_user_reply_failure_never_retries_as_bot(self):
        self.sdk.im.v1.message.reply.return_value = ReplyMessageResponse({"code": 999, "msg": "permission denied"})
        messages = MessageClient(self.sdk, user_tokens=self.tokens)
        with self.assertRaises(FeishuApiError):
            messages.reply_text("om_source", "结果正文", uuid="stable")
        self.sdk.im.v1.message.reply.assert_called_once()
        request, options = self.sdk.im.v1.message.reply.call_args.args
        self.assertEqual(request.token_types, {lark.AccessTokenType.USER})
        self.assertEqual(options.user_access_token, "fixture-user")
        self.assertEqual(request.request_body.uuid, "stable")
        self.sdk.im.v1.message.create.assert_not_called()
        self.tokens.access_token.side_effect = RuntimeError("过期")
        with self.assertRaises(RuntimeError):
            messages.reply_text("om_source", "正文")
        self.sdk.im.v1.message.reply.assert_called_once()

    def test_real_sdk_auth_keeps_user_identity_without_requesting_tenant_token(self):
        client = FeishuClient(FeishuConfig("cli_test", "test-secret"), user_tokens=self.tokens)
        request = ReplyMessageRequest.builder().message_id("om_source").build()
        option = user_options(request, self.tokens)
        with patch("lark_oapi.core.token.auth.TokenManager.get_self_tenant_token") as tenant:
            verify(required(client.client.config), request, option)
            tenant.assert_not_called()
        self.assertEqual(request.token_types, {lark.AccessTokenType.USER})
        self.assertEqual(option.user_access_token, "fixture-user")

    def test_rich_text_extracts_text_but_does_not_invent_attachment_contents(self):
        self.assertEqual(message_text("post", json.dumps({"zh_cn": {"title": "导出", "content": [
            [{"tag": "text", "text": "保留格式"}, {"tag": "img", "image_key": "key"}]]}})), "导出\n保留格式")
        self.assertEqual(message_text("file", '{"file_name":"需求.docx"}'), "")


class PersonalIntakeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.inbox = PersonalInbox(self.root / "inbox.sqlite3", 100000)
        self.chat = PersonalChat("oc_peer", "ou_peer")
        self.api = MemoryWorkspace()
        self.workspace = SpeakerWorkspace(replace(settings(self.root), notify_chat_id=""), feishu=self.api)
        self.model = Mock()
        self.model.interpret.return_value = IntakeDecision(True, title="支持导出", requirements="导出保留格式")
        for agent in self.workspace.agents.values():
            agent.interpreter = self.model
        self.intake = PersonalIntake(self.workspace, self.inbox, interpreter=self.model)

    def stage(self, mid: str, text: str, created: int = 110000):
        self.inbox.stage(self.chat, [PersonalMessage(mid, self.chat.chat_id, self.chat.peer_id, text, created)], created // 1000 + 1)

    def test_first_start_boundary_overlap_dedup_and_restart(self):
        self.stage("history", "旧聊天", 99999)
        self.stage("new", "产品甲增加导出")
        self.stage("new", "产品甲增加导出")
        restarted = PersonalInbox(self.inbox.path, 999999)
        batches = restarted.pending(120)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["source"]["message_id"], "new")
        self.intake.process(batches[0])
        self.assertFalse(restarted.pending(130))
        self.stage("new", "产品甲增加导出")
        self.assertFalse(restarted.pending(140))

    def test_chat_fragments_are_grouped_and_create_only_one_requirement(self):
        self.stage("one", "产品甲增加导出")
        self.stage("two", "需要保留格式", 111000)
        self.assertFalse(self.inbox.pending(113))
        batch = self.inbox.pending(120)[0]
        self.intake.process(batch)
        agent = self.workspace.agents[self.api.refs[0].key]
        agent.process_item(required(agent._next_item()))
        self.assertEqual(len(self.api.rows["one"]), 4)
        self.assertEqual(len(self.api.rows["two"]), 3)
        self.assertEqual(self.model.interpret.call_count, 1)
        self.assertTrue(any(mid == "two" for mid, _ in self.api.replies))
        self.assertFalse(self.inbox.pending(130))

    def test_greeting_sends_away_reply_without_creating_requirement(self):
        self.model.interpret.return_value = IntakeDecision(False)
        self.stage("greeting", "你好")
        self.intake.process(self.inbox.pending(120)[0])
        self.assertEqual(self.api.replies, [("greeting", "本人不在，普通聊天 DeepSeek 暂时不回复。")])
        self.assertTrue(all(agent._next_item() is None for agent in self.workspace.agents.values()))
        self.assertEqual(self.api.writes, [])
        self.stage("greeting", "你好")
        self.assertFalse(self.inbox.pending(130))

    def test_missing_product_keeps_context_for_next_answer(self):
        self.stage("original", "增加导出，保留格式")
        self.intake.process(self.inbox.pending(120)[0])
        self.assertIn("哪个台账", self.api.replies[0][1])
        self.stage("answer", "产品乙", int(time.time() * 1000) - 10000)
        batch = self.inbox.pending(time.time())[0]
        self.assertIn("增加导出", batch["source"]["text"])
        self.intake.process(batch)
        self.assertIsNone(self.workspace.agents[self.api.refs[0].key]._next_item())
        self.assertIsNotNone(self.workspace.agents[self.api.refs[1].key]._next_item())

    def test_failed_reply_reuses_cached_decision_and_stable_message_uuid(self):
        for is_requirement in (True, False):
            with self.subTest(is_requirement=is_requirement):
                self.model.reset_mock()
                self.model.interpret.return_value = IntakeDecision(is_requirement, title="导出" if is_requirement else "")
                self.stage(f"unknown-{is_requirement}", "增加导出" if is_requirement else "谢谢")
                batch = self.inbox.pending(120)[0]
                with patch.object(self.api, "reply_text", side_effect=RuntimeError("暂不可用")) as failed:
                    with self.assertRaises(RuntimeError):
                        self.intake.process(batch)
                recovered = PersonalInbox(self.inbox.path, 999999).pending(130)[0]
                with patch.object(self.api, "reply_text") as reply:
                    self.intake.process(recovered)
                    self.assertTrue(reply.call_args.kwargs["uuid"])
                    self.assertEqual(reply.call_args, failed.call_args)
                self.model.interpret.assert_called_once()

    def test_model_failures_retry_at_most_three_times_across_restart(self):
        self.stage("failed", "产品甲增加导出")
        self.model.interpret.side_effect = ModelError("服务暂不可用")
        for attempt in range(3):
            batch = self.inbox.pending(120 + attempt)[0]
            if attempt < 2:
                with self.assertRaises(ModelError):
                    self.intake.process(batch)
            else:
                self.intake.process(batch)
        self.assertEqual(self.model.interpret.call_count, 3)
        self.assertFalse(self.inbox.pending(200))
        self.assertTrue(all(agent._next_item() is None for agent in self.workspace.agents.values()))

    def test_polling_failure_preserves_cursor_and_other_chat_continues(self):
        api = Mock()
        other = PersonalChat("oc_other", "ou_other")
        api.chats.return_value = [self.chat, other, PersonalChat("oc_self", "ou_self")]
        api.messages.side_effect = [RuntimeError("第二页失败"), [PersonalMessage("other", "oc_other", "ou_other", "需求", 110000)]]
        with patch("agents.speaker_agent.adapters.feishu.personal.time.time", return_value=200), self.assertLogs(level="ERROR"):
            PersonalPoller(api, self.inbox, "ou_self").poll()
        self.assertEqual(self.inbox.cursor(self.chat.chat_id), 100)
        self.assertEqual(self.inbox.cursor(other.chat_id), 168)
        self.assertEqual(api.messages.call_count, 2)
        self.assertEqual(self.inbox.pending(200)[0]["source"]["message_id"], "other")

    def test_interrupted_personal_runtime_stops_workers_without_bot_socket(self):
        workspace = Mock()
        workspace._stopped.is_set.return_value = False
        workspace._stopped.wait.side_effect = KeyboardInterrupt
        oauth = Mock()
        oauth.profile.return_value = {"open_id": "ou_self", "name": "测试"}
        with patch.object(speaker, "UserOAuth", return_value=oauth), \
             patch.object(speaker, "FeishuClient"), patch.object(speaker, "SpeakerWorkspace", return_value=workspace), \
             patch.object(speaker, "PersonalPoller"), patch.object(speaker, "PersonalIntake"), \
             patch.object(speaker, "_poll_loop"), patch.object(speaker, "create_socket_client") as socket:
            with self.assertRaises(KeyboardInterrupt):
                speaker.run_agent(settings(self.root))
        workspace.stop.assert_called_once()
        socket.assert_not_called()

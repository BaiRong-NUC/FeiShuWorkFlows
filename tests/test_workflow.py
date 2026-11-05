from __future__ import annotations

from helpers import required, as_message_event, as_record_event

import json
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from dotenv import dotenv_values

from agents.speaker_agent.domain.contracts import AnalysisResult, DownstreamResult, ReviewResult, result_from_dict
from agents.speaker_agent.adapters.feishu.events import on_message
from agents.speaker_agent.adapters.feishu.gateway import FeishuApiError, FeishuGateway
from agents.speaker_agent.intake.normalizer import RequirementNormalizer
from agents.speaker_agent.domain.models import IncomingDemand
from agents.speaker_agent.presentation.reply import ReplyFormatter
from agents.speaker_agent.orchestration.service import DemandAgent
from test_agent import FakeGateway, _record, settings


COMPLETE = DownstreamResult(AnalysisResult("需要按日期导出报表。"), ReviewResult("可实施", "复用查询接口。"))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = settings(self.root)
        self.gateway = FakeGateway(_record())
        self.agent = DemandAgent(self.config, self.gateway)

    def start_record(self):
        self.agent.enqueue_record("rec1")
        result = required(self.agent.process_item(required(self.agent._next_item())))
        self.assertTrue(result.ok, result.error)
        return required(self.agent.workflows.latest_job("rec1"))

    def test_queue_survives_restart_before_processing(self):
        self.agent.accept_message(IncomingDemand("需求标题：报表\n业务目标：提效\n功能要求：导出", "ou_sender", "om1", "oc_origin"))
        restarted = DemandAgent(self.config, self.gateway)
        self.assertTrue(required(restarted.process_item(required(restarted._next_item()))).ok)
        job = required(restarted.workflows.latest_job("rec-created"))
        self.assertEqual(job["brief"]["goal"], "提效")
        self.assertEqual(job["brief"]["source_chat_id"], "oc_origin")
        self.assertFalse(restarted.accept_message(IncomingDemand("重复", "ou_sender", "om1")))
        self.assertEqual(len(self.gateway.created), 1)

    def test_partial_results_merge_across_restart_and_preserve_reply_route(self):
        self.agent.accept_message(IncomingDemand("需要按日期导出报表", "ou_sender", "om_origin"))
        required(self.agent.process_item(required(self.agent._next_item())))
        job = required(self.agent.workflows.latest_job("rec-created"))
        request_id = job["id"]
        self.agent.submit_result(request_id, DownstreamResult(analysis=COMPLETE.analysis))
        self.assertEqual(self.gateway.record.status, "生成中")
        self.assertEqual(self.gateway.results, [])
        restarted = DemandAgent(self.config, self.gateway)
        result = restarted.submit_result(request_id, DownstreamResult(review=COMPLETE.review))
        self.assertTrue(result.ok)
        self.assertEqual(self.gateway.replies[-1], ("om_origin", self.gateway.results[-1][1]))
        counts = (len(self.gateway.results), len(self.gateway.replies), len(self.gateway.messages))
        restarted.submit_result(request_id, COMPLETE)
        self.assertEqual(counts, (len(self.gateway.results), len(self.gateway.replies), len(self.gateway.messages)))

    def test_file_handoff_completes_with_independent_agent_files(self):
        job = self.start_record()
        request = json.loads((self.root / "handoff/outbox" / f'{job["id"]}.json').read_text(encoding="utf-8"))
        self.assertEqual(request["brief"]["requirements"], "支持按日期筛选")
        for stage, value in (("analysis", COMPLETE.analysis), ("review", COMPLETE.review)):
            path = self.root / "handoff/inbox" / f'{job["id"]}.{stage}.json'
            path.write_text(json.dumps({"schema_version": 1, "request_id": job["id"],
                                        "result": {stage: asdict(required(value))}}, ensure_ascii=False), encoding="utf-8")
            self.agent.resume_pending()
        self.assertEqual(self.gateway.record.status, "已生成")
        self.assertEqual(self.gateway.results[0][1], self.gateway.messages[-1][2])
        self.assertEqual(self.gateway.documents, [])

    def test_changed_requirement_rejects_stale_result(self):
        job = self.start_record()
        self.gateway.record = replace(self.gateway.record, fields={**self.gateway.record.fields, "功能要求": "只要在线查看"})
        with self.assertRaisesRegex(ValueError, "旧结果"):
            self.agent.submit_result(job["id"], COMPLETE)
        self.assertEqual(self.gateway.results, [])
        self.assertEqual(required(self.agent.workflows.get_job(job["id"]))["state"], "superseded")

    def test_requirement_changes_during_evaluation_are_not_overwritten(self):
        gateway = self.gateway

        class ChangingDownstream:
            def evaluate(self, brief):
                gateway.record = replace(gateway.record, fields={**gateway.record.fields, "功能要求": "需求已经改变"})
                return COMPLETE

        self.agent = DemandAgent(self.config, gateway, downstream=ChangingDownstream())
        self.agent.enqueue_record("rec1")
        with self.assertLogs("agents.speaker_agent.orchestration.service", level="ERROR"):
            result = required(self.agent.process_item(required(self.agent._next_item())))
        self.assertFalse(result.ok)
        self.assertEqual(gateway.results, [])

    def test_markdown_labels_and_hyperlink_targets_are_preserved(self):
        normalizer = RequirementNormalizer(self.config)
        brief = normalizer.from_message(IncomingDemand("# 需求标题：导出\n- 业务目标：提效", "ou1", "om1"))
        self.assertEqual(brief.title, "导出")
        linked = normalizer.from_record(replace(_record(), fields={"相关链接": {"text": "设计稿", "link": "https://example.com/spec"}}))
        self.assertIn("https://example.com/spec", linked.links)

    def test_polling_can_process_same_record_again_after_reset(self):
        first = self.start_record()
        self.agent.submit_result(first["id"], COMPLETE)
        self.gateway.record = replace(self.gateway.record, status=self.config.trigger_status)
        self.assertEqual(self.agent.scan_pending(), 1)
        required(self.agent.process_item(required(self.agent._next_item())))
        second = required(self.agent.workflows.latest_job("rec1"))
        self.assertNotEqual(first["id"], second["id"])
        self.agent.submit_result(second["id"], COMPLETE)
        self.assertEqual(len(self.gateway.results), 2)

    def test_notification_failure_retries_only_failed_destination(self):
        job = self.start_record()
        original = self.gateway.send_text
        attempted = []

        def fail_person(receive_id_type, receive_id, text, *, uuid=""):
            kind, target = receive_id_type, receive_id
            attempted.append((kind, uuid))
            if kind == "open_id":
                raise FeishuApiError("temporary failure")
            original(kind, target, text, uuid=uuid)

        self.gateway.send_text = fail_person
        with self.assertRaises(FeishuApiError):
            self.agent.submit_result(job["id"], COMPLETE)
        self.assertEqual(attempted[1][0], "chat_id")
        self.assertEqual(self.gateway.record.status, "已生成")
        group_count = len([m for m in self.gateway.messages if m[0] == "chat_id"])
        self.gateway.send_text = original
        restarted = DemandAgent(self.config, self.gateway)
        restarted.resume_pending()
        self.assertEqual(required(restarted.workflows.get_job(job["id"]))["state"], "done")
        self.assertEqual(group_count, len([m for m in self.gateway.messages if m[0] == "chat_id"]))
        self.assertEqual(len(self.gateway.results), 1)

    def test_record_write_succeeded_but_response_was_lost(self):
        job = self.start_record()
        original = self.gateway.write_result

        def lost_response(*args, **kwargs):
            original(*args, **kwargs)
            raise FeishuApiError("response lost")

        self.gateway.write_result = lost_response
        with self.assertRaises(FeishuApiError):
            self.agent.submit_result(job["id"], COMPLETE)
        self.gateway.write_result = original
        self.agent.resume_pending()
        self.assertEqual(required(self.agent.workflows.get_job(job["id"]))["state"], "done")

    def test_create_succeeded_then_read_failed_does_not_recreate(self):
        original = self.gateway.get_record
        self.gateway.get_record = lambda record_id: (_ for _ in ()).throw(FeishuApiError("read failed"))
        self.agent.accept_message(IncomingDemand("需要按日期导出报表", "ou_sender", "om1"))
        item = required(self.agent._next_item())
        with self.assertLogs("agents.speaker_agent.orchestration.service", level="ERROR"):
            self.assertFalse(required(self.agent.process_item(item)).ok)
        self.gateway.get_record = original
        self.assertTrue(required(self.agent.process_item(item)).ok)
        self.assertEqual(len(self.gateway.created), 1)
        self.assertEqual(len(self.gateway.client_tokens[0]), 36)

    def test_duplicate_events_do_not_acknowledge_twice(self):
        self.start_record()
        count = len(self.gateway.messages)
        self.agent.enqueue_record("rec1", event_id="another-event")
        required(self.agent.process_item(required(self.agent._next_item())))
        self.assertEqual(count, len(self.gateway.messages))
        self.assertEqual(len(list((self.root / "handoff/outbox").glob("*.json"))), 1)

    def test_malformed_result_cannot_mark_complete(self):
        job = self.start_record()
        path = self.root / "handoff/inbox" / f'{job["id"]}.json'
        path.write_text(json.dumps({"schema_version": 1, "request_id": "wrong", "result": asdict(COMPLETE)}))
        with self.assertLogs("agents.speaker_agent.orchestration.service", level="ERROR"):
            self.agent.resume_pending()
        self.assertEqual(self.gateway.results, [])
        with self.assertRaises(ValueError):
            result_from_dict({"analysis": {"summary": "x", "risks": "not an array"}})
        with self.assertRaises(ValueError):
            self.agent.submit_result("unknown", COMPLETE)

    def test_custom_markdown_template_is_used_verbatim(self):
        template = self.root / "reply.md"
        template.write_text("**${title}**\n${analysis_summary}\n${recommended_plan}\n", encoding="utf-8")
        brief = RequirementNormalizer(self.config).from_record(_record())
        text = ReplyFormatter(template).render(brief, COMPLETE)
        self.assertEqual(text, "**导出报表**\n需要按日期导出报表。\n复用查询接口。\n")
        template.write_text("${unknown}", encoding="utf-8")
        with self.assertRaises(KeyError):
            ReplyFormatter(template)

    def test_missing_risks_are_not_claimed_as_no_risk(self):
        brief = RequirementNormalizer(self.config).from_record(_record())
        text = ReplyFormatter().render(brief, COMPLETE)
        self.assertIn("未提供风险项", text)
        self.assertIn("未提供，待确认", text)

    def test_chat_mentions_preserve_multiline_fields(self):
        event = self.message_event("@_bot 需求\n需求标题：报表\n业务目标：提效\n功能要求：按日期导出\n验收标准：数目一致", "ou_bot")
        on_message(self.agent, as_message_event(event))
        self.assertTrue(required(self.agent.process_item(required(self.agent._next_item()))).ok)
        brief = required(self.agent.workflows.latest_job("rec-created"))["brief"]
        self.assertEqual(brief["title"], "报表")
        self.assertEqual(brief["goal"], "提效")
        self.assertEqual(brief["acceptance"], "数目一致")
        self.assertIn("\n", self.gateway.created[0]["需求原文"])

    def test_mentioning_another_person_does_not_trigger_bot(self):
        on_message(self.agent, as_message_event(self.message_event("@_bot 需求 帮我导出报表", "ou_someone")))
        self.assertIsNone(self.agent._next_item())
        self.assertEqual(self.gateway.replies, [])

    def test_bot_sender_and_nonobject_message_are_ignored(self):
        event = self.message_event("@_bot 需求 帮我导出报表", "ou_bot")
        event.event.sender.sender_type = "app"
        on_message(self.agent, as_message_event(event))
        event.event.sender.sender_type = "user"
        event.event.message.content = "[]"
        on_message(self.agent, as_message_event(event))
        self.assertIsNone(self.agent._next_item())

    def test_foreign_table_link_and_empty_demand_do_not_queue(self):
        self.agent.handle_command("整理需求 https://example.feishu.cn/base/other?record=rec1", "om1")
        self.agent.handle_command("需求", "om2")
        self.assertIsNone(self.agent._next_item())

    def test_partial_structured_chat_preserves_original_and_missing_fields(self):
        brief = RequirementNormalizer(self.config).from_message(IncomingDemand("业务目标：提效\n还有一句没归类的话", "ou1", "om1"))
        self.assertIn("没归类", brief.raw_text)
        self.assertIn("功能要求", brief.missing_fields)

    def test_example_configuration_contains_real_chinese(self):
        root = Path(__file__).resolve().parents[1]
        values = dotenv_values(root / ".env.example", encoding="utf-8")
        self.assertEqual(values["FEISHU_TRIGGER_STATUS"], "待生成方案")
        for path in [root / "README.md", root / ".env.example", *root.glob("agents/**/*.py")]:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("\\u", text, str(path))
            self.assertNotIn(chr(0xFFFD), text, str(path))

    @staticmethod
    def message_event(text, mention_id):
        return SimpleNamespace(event=SimpleNamespace(
            sender=SimpleNamespace(sender_type="user", sender_id=SimpleNamespace(open_id="ou_sender")),
            message=SimpleNamespace(message_id="om1", chat_id="oc_origin", chat_type="group", message_type="text",
                                    content=json.dumps({"text": text}),
                                    mentions=[SimpleNamespace(key="@_bot", id=SimpleNamespace(open_id=mention_id))])))


class GatewayTests(unittest.TestCase):
    def test_result_text_and_completion_are_one_update(self):
        captured = []
        response = SimpleNamespace(success=lambda: True, data=SimpleNamespace())
        update = lambda request: captured.append(request) or response
        client = SimpleNamespace(bitable=SimpleNamespace(v1=SimpleNamespace(app_table_record=SimpleNamespace(update=update))))
        gateway = FeishuGateway(settings(Path("unused")), client=client)
        gateway.write_result("rec1", "# 中文结论\n正文")
        fields = captured[0].request_body.fields
        self.assertEqual(fields["状态"], "已生成")
        self.assertEqual(fields["处理结论"], "# 中文结论\n正文")
        self.assertEqual(fields["处理备注"], "")


if __name__ == "__main__":
    unittest.main()

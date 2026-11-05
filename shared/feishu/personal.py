"""个人身份的会话枚举与历史消息读取。所有读取均显式使用用户 Token。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

import lark_oapi as lark
from lark_oapi.api.im.v1 import ListChatRequest, ListMessageRequest
from shared.feishu.auth.oauth import AccessTokenProvider
from shared.feishu.errors import FeishuApiError, require_service, required_data


@dataclass(frozen=True)
class PersonalChat:
    chat_id: str
    peer_id: str
    name: str = ""


@dataclass(frozen=True)
class PersonalMessage:
    message_id: str
    chat_id: str
    sender_id: str
    text: str
    created_ms: int


class PersonalChatAPI(Protocol):
    def chats(self) -> list[PersonalChat]: ...
    def messages(self, chat: PersonalChat, start: int, end: int) -> list[PersonalMessage]: ...


def user_options(request: lark.BaseRequest, tokens: AccessTokenProvider) -> lark.RequestOption:
    token = tokens.access_token()
    if not token:
        raise FeishuApiError("个人消息接口缺少用户 Token，请运行 python main.py --login")
    request.token_types = {lark.AccessTokenType.USER}
    return lark.RequestOption.builder().user_access_token(token).build()


def message_text(kind: str, raw: str) -> str:
    try:
        body = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    if kind == "text":
        return body.get("text", "").strip() if isinstance(body.get("text"), str) else ""
    if kind != "post":
        return ""
    if "content" not in body:
        body = body.get("zh_cn") or body.get("en_us") or next(iter(body.values()), {})
    if not isinstance(body, dict):
        return ""
    lines = [body.get("title", "")] if isinstance(body.get("title"), str) else []
    content = body.get("content", [])
    if isinstance(content, list):
        for row in content:
            if not isinstance(row, list):
                continue
            lines.append("".join(str(item.get("text", "")) for item in row
                                 if isinstance(item, dict) and item.get("tag") in {"text", "a", "md"}))
    return "\n".join(lines).strip()


def next_page(has_more: bool | None, token: str | None, seen: set[str]) -> str:
    if not has_more:
        return ""
    if not token or token in seen:
        raise FeishuApiError("飞书返回无效分页游标，本轮不推进消息读取位置")
    seen.add(token)
    return token


class PersonalChatClient:
    def __init__(self, client: lark.Client, tokens: AccessTokenProvider) -> None:
        self.client, self.tokens = client, tokens

    def chats(self) -> list[PersonalChat]:
        result: dict[str, PersonalChat] = {}
        token, seen = "", set()
        while True:
            request = (ListChatRequest.builder().types("p2p").user_id_type("open_id")
                       .page_size(100).page_token(token).build())
            data = required_data(require_service(self.client.im).v1.chat.list(
                request, user_options(request, self.tokens)), "读取个人单聊列表")
            for chat in data.items or []:
                if chat.chat_mode == "p2p" and chat.p2p_target_type == "user" and chat.chat_id and chat.p2p_target_id:
                    result[chat.chat_id] = PersonalChat(chat.chat_id, chat.p2p_target_id, chat.name or "")
            token = next_page(data.has_more, data.page_token, seen)
            if not token:
                return list(result.values())

    def messages(self, chat: PersonalChat, start: int, end: int) -> list[PersonalMessage]:
        result: dict[str, PersonalMessage] = {}
        token, seen = "", set()
        while True:
            request = (ListMessageRequest.builder().container_id_type("chat").container_id(chat.chat_id)
                       .start_time(str(start)).end_time(str(end)).sort_type("ByCreateTimeAsc")
                       .page_size(50).page_token(token).build())
            data = required_data(require_service(self.client.im).v1.message.list(
                request, user_options(request, self.tokens)), "读取个人单聊消息")
            for message in data.items or []:
                sender = message.sender
                if message.deleted or not sender or sender.sender_type != "user":
                    continue
                if sender.id_type != "open_id":
                    raise FeishuApiError("个人消息发送人不是 open_id，停止读取以避免误识别账号")
                sender_id = sender.id
                if not sender_id or sender_id != chat.peer_id or not message.message_id:
                    continue
                text = message_text(message.msg_type or "", message.body.content or "") if message.body else ""
                if text and message.create_time is not None:
                    result[message.message_id] = PersonalMessage(message.message_id, chat.chat_id,
                                                                 sender_id, text, int(message.create_time))
            token = next_page(data.has_more, data.page_token, seen)
            if not token:
                return sorted(result.values(), key=lambda message: (message.created_ms, message.message_id))

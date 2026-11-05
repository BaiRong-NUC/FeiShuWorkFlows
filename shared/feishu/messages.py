from __future__ import annotations

import json
from typing import Protocol

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from shared.feishu.errors import require_service, required_data
from shared.feishu.auth.oauth import AccessTokenProvider
from shared.feishu.personal import user_options


class MessageDeliveryObserver(Protocol):
    """让调用方在发送前记录意图、发送后记录消息 ID。"""
    def before_send(self, target_type: str, target_id: str, text: str) -> None: ...
    def after_send(self, message_id: str) -> None: ...


class MessageClient:
    def __init__(self, client: lark.Client, *, user_tokens: AccessTokenProvider | None = None) -> None:
        self.client = client
        self.user_tokens = user_tokens
        self.observer: MessageDeliveryObserver | None = None

    def send_text(self, receive_id_type: str, receive_id: str, text: str, *, uuid: str = "") -> None:
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .uuid(uuid)
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(body)
            .build()
        )
        if self.observer:
            self.observer.before_send(receive_id_type, receive_id, text)
        response = require_service(self.client.im).v1.message.create(
            request, user_options(request, self.user_tokens) if self.user_tokens else lark.RequestOption())
        data = required_data(response, "send Feishu message")
        if self.observer and data.message_id:
            self.observer.after_send(data.message_id)

    def reply_text(self, message_id: str, text: str, *, uuid: str = "") -> None:
        body = (
            ReplyMessageRequestBody.builder()
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .uuid(uuid)
            .build()
        )
        request = (
            ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
        )
        if self.observer:
            self.observer.before_send("message_id", message_id, text)
        response = require_service(self.client.im).v1.message.reply(
            request, user_options(request, self.user_tokens) if self.user_tokens else lark.RequestOption())
        data = required_data(response, "reply to Feishu message")
        if self.observer and data.message_id:
            self.observer.after_send(data.message_id)

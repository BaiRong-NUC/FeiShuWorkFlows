from __future__ import annotations

import json

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from shared.feishu.errors import require_service, required_data


class MessageClient:
    def __init__(self, client: lark.Client) -> None:
        self.client = client

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
        response = require_service(self.client.im).v1.message.create(request)
        required_data(response, "send Feishu message")

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
        response = require_service(self.client.im).v1.message.reply(request)
        required_data(response, "reply to Feishu message")

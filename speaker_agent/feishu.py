from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

import lark_oapi as lark
from lark_oapi.api.bitable.v1 import (
    AppTableRecord,
    CreateAppTableRecordRequest,
    GetAppTableRecordRequest,
    ListAppTableFieldRequest,
    ListAppTableRecordRequest,
    UpdateAppTableRecordRequest,
)
from lark_oapi.api.docx.v1 import (
    Block,
    ConvertDocumentRequest,
    ConvertDocumentRequestBody,
    CreateDocumentBlockDescendantRequest,
    CreateDocumentBlockDescendantRequestBody,
    CreateDocumentRequest,
    CreateDocumentRequestBody,
)
from lark_oapi.api.drive.v1 import (
    BaseMember,
    CreatePermissionMemberRequest,
)
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from .config import Settings
from .models import DemandRecord


class FeishuApiError(RuntimeError):
    """Raised when a Feishu OpenAPI call fails."""


class FeishuGateway:
    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        self.client = client or (
            lark.Client.builder()
            .app_id(settings.app_id)
            .app_secret(settings.app_secret)
            .build()
        )
        self._field_names_by_id: dict[str, str] | None = None

    def get_record(self, record_id: str) -> DemandRecord:
        request = (
            GetAppTableRecordRequest.builder()
            .app_token(self.settings.bitable_app_token)
            .table_id(self.settings.bitable_table_id)
            .record_id(record_id)
            .automatic_fields(True)
            .user_id_type("open_id")
            .build()
        )
        response = self.client.bitable.v1.app_table_record.get(request)
        record = _required_data(response, "read Bitable record").record
        if record is None:
            raise FeishuApiError(f"Bitable record {record_id} has no data")
        return self._to_demand(record)

    def list_records_by_status(self, status: str) -> list[DemandRecord]:
        field_name = self._escape_formula(self.settings.status_field)
        field_value = self._escape_formula(status)
        filter_expression = f'CurrentValue.[{field_name}]="{field_value}"'
        records: list[DemandRecord] = []
        page_token = ""
        while True:
            builder = (
                ListAppTableRecordRequest.builder()
                .app_token(self.settings.bitable_app_token)
                .table_id(self.settings.bitable_table_id)
                .filter(filter_expression)
                .automatic_fields(True)
                .user_id_type("open_id")
                .page_size(100)
            )
            if page_token:
                builder.page_token(page_token)
            response = self.client.bitable.v1.app_table_record.list(builder.build())
            data = _required_data(response, "list Bitable records")
            records.extend(self._to_demand(item) for item in data.items or [])
            if not data.has_more or not data.page_token:
                return records
            page_token = data.page_token

    def update_status(
        self,
        record_id: str,
        status: str,
        *,
        proposal_url: str = "",
        error: str = "",
    ) -> None:
        fields: dict[str, Any] = {self.settings.status_field: status}
        if proposal_url:
            fields[self.settings.proposal_field] = {
                "text": "\u67e5\u770b\u65b9\u6848",
                "link": proposal_url,
            }
        if error:
            fields[self.settings.error_field] = error[:1000]
        body = AppTableRecord.builder().fields(fields).build()
        request = (
            UpdateAppTableRecordRequest.builder()
            .app_token(self.settings.bitable_app_token)
            .table_id(self.settings.bitable_table_id)
            .record_id(record_id)
            .user_id_type("open_id")
            .request_body(body)
            .build()
        )
        response = self.client.bitable.v1.app_table_record.update(request)
        _required_data(response, "update Bitable record")

    def create_record(self, fields: dict[str, Any]) -> str:
        body = AppTableRecord.builder().fields(fields).build()
        request = (
            CreateAppTableRecordRequest.builder()
            .app_token(self.settings.bitable_app_token)
            .table_id(self.settings.bitable_table_id)
            .user_id_type("open_id")
            .request_body(body)
            .build()
        )
        response = self.client.bitable.v1.app_table_record.create(request)
        record = _required_data(response, "create Bitable record").record
        if record is None or not record.record_id:
            raise FeishuApiError("Feishu record creation returned no record ID")
        return str(record.record_id)

    def create_proposal_document(self, title: str, markdown: str) -> tuple[str, str]:
        body_builder = CreateDocumentRequestBody.builder().title(title[:800])
        if self.settings.doc_folder_token:
            body_builder.folder_token(self.settings.doc_folder_token)
        create_request = (
            CreateDocumentRequest.builder().request_body(body_builder.build()).build()
        )
        create_response = self.client.docx.v1.document.create(create_request)
        document = _required_data(create_response, "create Feishu document").document
        if document is None or not document.document_id:
            raise FeishuApiError("Feishu document creation returned no document ID")
        document_id = document.document_id

        converted = self._convert_markdown(markdown)
        if converted:
            children_id, descendants = converted
            descendant_body = (
                CreateDocumentBlockDescendantRequestBody.builder()
                .children_id(children_id)
                .descendants(descendants)
                .index(0)
                .build()
            )
            descendant_request = (
                CreateDocumentBlockDescendantRequest.builder()
                .document_id(document_id)
                .block_id(document_id)
                .document_revision_id(-1)
                .request_body(descendant_body)
                .build()
            )
            descendant_response = self.client.docx.v1.document_block_descendant.create(
                descendant_request
            )
            _required_data(descendant_response, "write Feishu document")

        url = f"https://www.feishu.cn/docx/{document_id}"
        return document_id, url

    def grant_document_view(self, document_token: str, open_id: str) -> None:
        if not open_id:
            return
        member = (
            BaseMember.builder()
            .member_type("openid")
            .member_id(open_id)
            .perm("view")
            .perm_type("container")
            .type("user")
            .build()
        )
        request = (
            CreatePermissionMemberRequest.builder()
            .token(document_token)
            .type("docx")
            .need_notification(False)
            .request_body(member)
            .build()
        )
        response = self.client.drive.v1.permission_member.create(request)
        _required_data(response, "grant document permission")

    def send_text(self, receive_id_type: str, receive_id: str, text: str) -> None:
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(body)
            .build()
        )
        response = self.client.im.v1.message.create(request)
        _required_data(response, "send Feishu message")

    def reply_text(self, message_id: str, text: str) -> None:
        body = (
            ReplyMessageRequestBody.builder()
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        request = (
            ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
        )
        response = self.client.im.v1.message.reply(request)
        _required_data(response, "reply to Feishu message")

    def record_url(self, record_id: str) -> str:
        if self.settings.bitable_url:
            separator = "&" if "?" in self.settings.bitable_url else "?"
            return (
                f"{self.settings.bitable_url}{separator}table={self.settings.bitable_table_id}"
                f"&record={record_id}"
            )
        return (
            "https://www.feishu.cn/base/"
            f"{self.settings.bitable_app_token}?table={self.settings.bitable_table_id}&record={record_id}"
        )

    def _to_demand(self, record: Any) -> DemandRecord:
        raw_fields = record.fields or {}
        fields = {
            self._field_name(str(key)): value
            for key, value in raw_fields.items()
        }
        record_id = str(record.record_id or "")
        return DemandRecord(
            record_id=record_id,
            fields=fields,
            title=self._field_text(fields, self.settings.title_field) or "\u672a\u547d\u540d\u9700\u6c42",
            requester_id=self._person_id(fields.get(self.settings.requester_field)),
            status=self._field_text(fields, self.settings.status_field),
            record_url=str(getattr(record, "record_url", "") or "") or self.record_url(record_id),
        )

    def _field_names(self) -> dict[str, str]:
        if self._field_names_by_id is not None:
            return self._field_names_by_id
        names: dict[str, str] = {}
        page_token = ""
        while True:
            builder = (
                ListAppTableFieldRequest.builder()
                .app_token(self.settings.bitable_app_token)
                .table_id(self.settings.bitable_table_id)
                .page_size(100)
            )
            if page_token:
                builder.page_token(page_token)
            response = self.client.bitable.v1.app_table_field.list(builder.build())
            data = _required_data(response, "list Bitable fields")
            for item in data.items or []:
                if item.field_id and item.field_name:
                    names[item.field_id] = item.field_name
            if not data.has_more or not data.page_token:
                break
            page_token = data.page_token
        self._field_names_by_id = names
        return names

    def _field_name(self, key: str) -> str:
        if not key.startswith("fld"):
            return key
        return self._field_names().get(key, key)

    def _convert_markdown(self, markdown: str) -> tuple[list[str], list[Block]] | None:
        body = (
            ConvertDocumentRequestBody.builder()
            .content_type("markdown")
            .content(markdown)
            .build()
        )
        request = ConvertDocumentRequest.builder().request_body(body).build()
        response = self.client.docx.v1.document.convert(request)
        data = _required_data(response, "convert proposal Markdown")
        first_level = [
            str(block_id)
            for block_id in (data.first_level_block_ids or [])
            if block_id
        ]
        blocks = [item if isinstance(item, Block) else Block(item) for item in data.blocks or []]
        if not first_level or not blocks:
            return None
        return first_level, blocks

    @staticmethod
    def _field_text(fields: Mapping[str, Any], name: str) -> str:
        return _plain_text(fields.get(name))

    @staticmethod
    def _person_id(value: Any) -> str:
        people = value if isinstance(value, list) else [value]
        for person in people:
            if isinstance(person, Mapping):
                for key in ("id", "open_id"):
                    found = str(person.get(key) or "").strip()
                    if found:
                        return found
        return ""

    @staticmethod
    def _escape_formula(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')


def _required_data(response: Any, action: str) -> Any:
    if not response.success():
        message = f"failed to {action}: code={response.code}, msg={response.msg}"
        log_id = getattr(response, "get_log_id", lambda: "")()
        if log_id:
            message += f", log_id={log_id}"
        raise FeishuApiError(message)
    if response.data is None:
        raise FeishuApiError(f"failed to {action}: Feishu returned no data")
    return response.data


def _plain_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _compact(value)
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [_plain_text(item) for item in value]
        return "\u3001".join(part for part in parts if part)
    if isinstance(value, Mapping):
        for key in ("text", "name", "value", "link", "url"):
            if key in value:
                text = _plain_text(value.get(key))
                if text:
                    return text
        parts = [_plain_text(item) for item in value.values()]
        return "\u3001".join(part for part in parts if part)
    return _compact(str(value))


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()

from __future__ import annotations

from typing import Any

from agents.speaker_agent.config import Settings
from agents.speaker_agent.domain.models import DemandRecord
from shared.feishu.client import FeishuClient
from shared.feishu.contracts import BitableRecord, FeishuServices
from shared.feishu.fields import escape_formula, person_id, plain_text


class FeishuGateway:
    """Map Speaker requirements to shared Feishu services."""

    def __init__(self, settings: Settings, *, feishu: FeishuServices | None = None) -> None:
        self.settings = settings
        self.feishu = feishu if feishu is not None else FeishuClient(settings.feishu)
        self.table = self.feishu.table(settings.bitable)

    def get_record(self, record_id: str) -> DemandRecord:
        return self._to_demand(self.table.get_record(record_id))

    def list_records_by_status(self, status: str) -> list[DemandRecord]:
        field_name = escape_formula(self.settings.status_field)
        field_value = escape_formula(status)
        expression = f'CurrentValue.[{field_name}]="{field_value}"'
        return [self._to_demand(record) for record in self.table.list_records(filter_expression=expression)]

    def update_status(
        self,
        record_id: str,
        status: str,
        *,
        proposal_url: str = "",
        error: str = "",
    ) -> None:
        fields: dict[str, Any] = {self.settings.status_field: status, self.settings.error_field: error[:1000]}
        if proposal_url:
            fields[self.settings.proposal_field] = {
                "text": "查看方案",
                "link": proposal_url,
            }
        self.table.update_record(record_id, fields)

    def write_result(self, record_id: str, markdown: str, *, document_url: str = "") -> None:
        fields: dict[str, Any] = {
            self.settings.status_field: self.settings.completed_status,
            self.settings.result_field: markdown,
            self.settings.error_field: "",
        }
        if document_url:
            fields[self.settings.proposal_field] = {"text": "查看结论", "link": document_url}
        self.table.update_record(record_id, fields)

    def create_record(self, fields: dict[str, Any], *, client_token: str = "") -> str:
        return self.table.create_record(fields, client_token=client_token)

    def create_proposal_document(self, title: str, markdown: str) -> tuple[str, str]:
        return self.feishu.documents.create_markdown_document(title, markdown, folder_token=self.settings.doc_folder_token)

    def grant_document_view(self, document_token: str, open_id: str) -> None:
        self.feishu.documents.grant_document_view(document_token, open_id)

    def send_text(self, receive_id_type: str, receive_id: str, text: str, *, uuid: str = "") -> None:
        self.feishu.messages.send_text(receive_id_type, receive_id, text, uuid=uuid)

    def reply_text(self, message_id: str, text: str, *, uuid: str = "") -> None:
        self.feishu.messages.reply_text(message_id, text, uuid=uuid)

    def _to_demand(self, record: BitableRecord) -> DemandRecord:
        fields = record.fields
        return DemandRecord(
            record_id=record.record_id,
            fields=fields,
            title=plain_text(fields.get(self.settings.title_field)) or "未命名需求",
            requester_id=person_id(fields.get(self.settings.requester_field)),
            status=plain_text(fields.get(self.settings.status_field)),
            record_url=record.record_url,
        )

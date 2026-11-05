from __future__ import annotations

from agents.speaker_agent.config import Settings
from shared.feishu.client import FeishuClient
from shared.feishu.contracts import FeishuServices


class FeishuDelivery:
    """Speaker's delivery options, shared by its ledger adapters."""

    def __init__(self, settings: Settings, *, feishu: FeishuServices | None = None) -> None:
        self.settings = settings
        self.feishu = feishu if feishu is not None else FeishuClient(settings.feishu)

    def create_proposal_document(self, title: str, markdown: str) -> tuple[str, str]:
        return self.feishu.documents.create_markdown_document(title, markdown, folder_token=self.settings.doc_folder_token)

    def grant_document_view(self, document_token: str, open_id: str) -> None:
        self.feishu.documents.grant_document_view(document_token, open_id)

    def send_text(self, receive_id_type: str, receive_id: str, text: str, *, uuid: str = "") -> None:
        self.feishu.messages.send_text(receive_id_type, receive_id, text, uuid=uuid)

    def reply_text(self, message_id: str, text: str, *, uuid: str = "") -> None:
        self.feishu.messages.reply_text(message_id, text, uuid=uuid)

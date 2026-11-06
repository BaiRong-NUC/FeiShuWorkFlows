from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from shared.feishu.config import BitableConfig
from shared.feishu.catalog import TableReference


@dataclass(frozen=True)
class BitableRecord:
    record_id: str
    fields: dict[str, Any]
    record_url: str = ""


class BitableAPI(Protocol):
    def get_record(self, record_id: str) -> BitableRecord: ...
    def list_records(self, *, filter_expression: str = "") -> list[BitableRecord]: ...
    def create_record(self, fields: dict[str, Any], *, client_token: str = "") -> str: ...
    def update_record(self, record_id: str, fields: dict[str, Any]) -> None: ...


class MessageAPI(Protocol):
    def send_text(self, receive_id_type: str, receive_id: str, text: str, *, uuid: str = "") -> None: ...
    def reply_text(self, message_id: str, text: str, *, uuid: str = "") -> None: ...


class DocumentAPI(Protocol):
    def create_markdown_document(self, title: str, markdown: str, *, folder_token: str = "") -> tuple[str, str]: ...
    def grant_document_view(self, document_token: str, open_id: str) -> None: ...


class FeishuServices(Protocol):
    @property
    def messages(self) -> MessageAPI: ...

    @property
    def documents(self) -> DocumentAPI: ...

    def table(self, config: BitableConfig) -> BitableAPI: ...


class CatalogAPI(Protocol):
    def discover(self, urls: tuple[str, ...]) -> list[TableReference]: ...


class SheetsAPI(Protocol):
    def read_dropdowns(self, token: str, range_name: str) -> list[dict[str, Any]]: ...
    def read_range(self, token: str, range_name: str, *, value_render_option: str = "ToString") -> list[list[Any]]: ...
    def write_ranges(self, token: str, ranges: list[tuple[str, list[list[Any]]]]) -> None: ...
    def append_rows(self, token: str, range_name: str, rows: list[list[Any]]) -> None: ...


class WorkspaceServices(FeishuServices, Protocol):
    @property
    def catalog(self) -> CatalogAPI: ...

    @property
    def sheets(self) -> SheetsAPI: ...

from __future__ import annotations

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

from shared.feishu.errors import FeishuApiError, require_service, required_data
from shared.feishu.config import BitableConfig
from shared.feishu.contracts import BitableRecord


class BitableClient:
    """Table-bound API without workflow-specific columns or states."""

    def __init__(self, client: lark.Client, config: BitableConfig) -> None:
        self.client = client
        self.config = config
        self._field_names_by_id: dict[str, str] | None = None

    def get_record(self, record_id: str) -> BitableRecord:
        request = (
            GetAppTableRecordRequest.builder()
            .app_token(self.config.app_token)
            .table_id(self.config.table_id)
            .record_id(record_id)
            .automatic_fields(True)
            .user_id_type("open_id")
            .build()
        )
        response = require_service(self.client.bitable).v1.app_table_record.get(request)
        record = required_data(response, "read Bitable record").record
        if record is None:
            raise FeishuApiError(f"Bitable record {record_id} has no data")
        return self._to_record(record)

    def list_records(self, *, filter_expression: str = "") -> list[BitableRecord]:
        records: list[BitableRecord] = []
        page_token = ""
        while True:
            builder = (
                ListAppTableRecordRequest.builder()
                .app_token(self.config.app_token)
                .table_id(self.config.table_id)
                .automatic_fields(True)
                .user_id_type("open_id")
                .page_size(100)
            )
            if filter_expression:
                builder.filter(filter_expression)
            if page_token:
                builder.page_token(page_token)
            response = require_service(self.client.bitable).v1.app_table_record.list(builder.build())
            data = required_data(response, "list Bitable records")
            records.extend(self._to_record(item) for item in data.items or [])
            if not data.has_more or not data.page_token:
                return records
            page_token = data.page_token

    def update_record(self, record_id: str, fields: dict[str, Any]) -> None:
        body = AppTableRecord.builder().fields(fields).build()
        request = (
            UpdateAppTableRecordRequest.builder()
            .app_token(self.config.app_token)
            .table_id(self.config.table_id)
            .record_id(record_id)
            .user_id_type("open_id")
            .request_body(body)
            .build()
        )
        response = require_service(self.client.bitable).v1.app_table_record.update(request)
        required_data(response, "update Bitable record")

    def create_record(self, fields: dict[str, Any], *, client_token: str = "") -> str:
        body = AppTableRecord.builder().fields(fields).build()
        request = (
            CreateAppTableRecordRequest.builder()
            .app_token(self.config.app_token)
            .table_id(self.config.table_id)
            .user_id_type("open_id")
            .client_token(client_token)
            .request_body(body)
            .build()
        )
        response = require_service(self.client.bitable).v1.app_table_record.create(request)
        record = required_data(response, "create Bitable record").record
        if record is None or not record.record_id:
            raise FeishuApiError("Feishu record creation returned no record ID")
        return str(record.record_id)

    def record_url(self, record_id: str) -> str:
        if self.config.url:
            separator = "&" if "?" in self.config.url else "?"
            return (
                f"{self.config.url}{separator}table={self.config.table_id}"
                f"&record={record_id}"
            )
        return (
            "https://www.feishu.cn/base/"
            f"{self.config.app_token}?table={self.config.table_id}&record={record_id}"
        )

    def _field_names(self) -> dict[str, str]:
        if self._field_names_by_id is not None:
            return self._field_names_by_id
        names: dict[str, str] = {}
        page_token = ""
        while True:
            builder = (
                ListAppTableFieldRequest.builder()
                .app_token(self.config.app_token)
                .table_id(self.config.table_id)
                .page_size(100)
            )
            if page_token:
                builder.page_token(page_token)
            response = require_service(self.client.bitable).v1.app_table_field.list(builder.build())
            data = required_data(response, "list Bitable fields")
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

    def _to_record(self, record: AppTableRecord) -> BitableRecord:
        record_id = record.record_id or ""
        return BitableRecord(
            record_id=record_id,
            fields={self._field_name(str(key)): value for key, value in (record.fields or {}).items()},
            record_url=str(getattr(record, "record_url", "") or "") or self.record_url(record_id),
        )

from __future__ import annotations

import logging
import threading
from typing import Any
from uuid import uuid4

from agents.speaker_agent.adapters.feishu.delivery import FeishuDelivery
from agents.speaker_agent.config import Settings
from agents.speaker_agent.domain.models import DemandRecord
from shared.feishu.catalog import TableReference
from shared.feishu.contracts import WorkspaceServices
from shared.feishu.errors import FeishuApiError
from shared.feishu.fields import person_id
from shared.feishu.sheets import column_name

LOGGER = logging.getLogger(__name__)


class SheetGateway(FeishuDelivery):
    """Map spreadsheet rows to demands; re-locate every write using the ID column."""

    def __init__(self, settings: Settings, ref: TableReference, *, feishu: WorkspaceServices) -> None:
        super().__init__(settings, feishu=feishu)
        self.ref = ref
        self.sheets = feishu.sheets
        self._lock = threading.RLock()
        outputs = [settings.sheet_note_field, settings.result_field, settings.brief_field, settings.proposal_field]
        inputs = {settings.sheet_id_field, settings.status_field, settings.raw_field,
                  settings.title_field, settings.requester_field, "描述", "备注", "处理备注", "版本", "责任人"}
        if len(set(outputs)) != len(outputs) or any(name in inputs for name in outputs):
            raise ValueError("电子表格的需求单、结论、链接及 Agent 备注必须使用独立列，不能覆盖原台账字段")

    def _read(self) -> tuple[list[str], list[tuple[int, dict[str, Any]]]]:
        # A bare sheet ID requests its current populated range, including newly appended rows.
        rows = self.sheets.read_range(self.ref.file_token, self.ref.table_id)
        if not rows:
            raise FeishuApiError(f"工作表“{self.ref.title}”没有表头")
        headers = [_text(value) for value in rows[0]]
        while headers and not headers[-1]:
            headers.pop()
        # Unnamed columns may contain user data; append outputs beyond all populated columns.
        used_width = max((i + 1 for row in rows for i, value in enumerate(row) if _text(value)), default=0)
        headers.extend([""] * (used_width - len(headers)))
        names = [name for name in headers if name]
        if len(names) != len(set(names)):
            raise FeishuApiError(f"工作表“{self.ref.title}”含重复表头")
        required = {self.settings.sheet_id_field, self.settings.status_field}
        if not required.issubset(headers) or not self._description_field(headers):
            raise FeishuApiError(f"工作表“{self.ref.title}”需要 ID、状态，以及描述或需求原文列")
        result: list[tuple[int, dict[str, Any]]] = []
        for number, row in enumerate(rows[1:], start=2):
            fields = {name: row[i] if i < len(row) else "" for i, name in enumerate(headers) if name}
            record_id = _text(fields.get(self.settings.sheet_id_field))
            if not record_id:
                if _text(fields.get(self.settings.status_field)) == self.settings.trigger_status:
                    LOGGER.warning("工作表 %s 第 %s 行缺少 ID，已跳过", self.ref.title, number)
                continue
            result.append((number, fields))
        return headers, result

    def _duplicate_ids(self, rows: list[tuple[int, dict[str, Any]]]) -> dict[str, list[int]]:
        locations: dict[str, list[int]] = {}
        for number, fields in rows:
            record_id = _text(fields.get(self.settings.sheet_id_field))
            locations.setdefault(record_id, []).append(number)
        return {record_id: numbers for record_id, numbers in locations.items() if len(numbers) > 1}

    @staticmethod
    def _duplicate_details(duplicates: dict[str, list[int]]) -> str:
        details = []
        for record_id, numbers in list(duplicates.items())[:5]:
            positions = "、".join(map(str, numbers[:20]))
            if len(numbers) > 20:
                positions += f"等共 {len(numbers)} 行"
            details.append(f"ID {record_id[:80]!r}：第 {positions} 行")
        if len(duplicates) > 5:
            details.append(f"共 {len(duplicates)} 组重复 ID，仅列出前 5 组")
        return "；".join(details)

    def _description_field(self, headers: list[str]) -> str:
        return next((name for name in (self.settings.raw_field, "描述") if name in headers), "")

    def _record(self, number: int, fields: dict[str, Any], headers: list[str]) -> DemandRecord:
        raw = _text(fields.get(self._description_field(headers)))
        # Human notes and version are input context; processing notes are never overwritten.
        context = "\n".join(f"{name}：{_text(fields.get(name))}" for name in ("版本", "备注") if _text(fields.get(name)))
        mapped = {**fields, self.settings.raw_field: raw + ("\n" + context if context else "")}
        requester = _text(fields.get("提出人ID"))
        if not requester.startswith("ou_"):
            requester = ""
        return DemandRecord(_text(fields[self.settings.sheet_id_field]), mapped,
                            _text(fields.get(self.settings.title_field)) or raw.split("\n")[0][:80] or "未命名需求",
                            requester, _text(fields.get(self.settings.status_field)),
                            self.ref.url + f"&range=A{number}")

    def get_record(self, record_id: str) -> DemandRecord:
        headers, rows = self._read()
        number, fields = self._find(rows, record_id)
        return self._record(number, fields, headers)

    def list_records_by_status(self, status: str) -> list[DemandRecord]:
        headers, rows = self._read()
        # Check all rows, including completed and untriggered rows: a pending row
        # is still ambiguous if another row with the same ID has a different status.
        duplicates = self._duplicate_ids(rows)
        if duplicates:
            LOGGER.warning("工作表“%s”跳过重复 ID 对应的全部记录，其他唯一 ID 正常处理：%s",
                           self.ref.title, self._duplicate_details(duplicates))
        return [self._record(number, fields, headers) for number, fields in rows
                if _text(fields.get(self.settings.status_field)) == status
                and _text(fields.get(self.settings.sheet_id_field)) not in duplicates]

    def _find(self, rows: list[tuple[int, dict[str, Any]]], record_id: str) -> tuple[int, dict[str, Any]]:
        matches = [(number, fields) for number, fields in rows
                   if _text(fields.get(self.settings.sheet_id_field)) == record_id]
        if len(matches) > 1:
            details = self._duplicate_details({record_id: [number for number, _ in matches]})
            raise FeishuApiError(f"工作表“{self.ref.title}”的 ID 不唯一：{details}；拒绝处理该 ID，避免回填错误")
        if not matches:
            raise FeishuApiError(f"工作表“{self.ref.title}”中找不到记录 ID")
        return matches[0]

    def _columns(self, headers: list[str], names: list[str]) -> list[str]:
        missing = list(dict.fromkeys(name for name in names if name not in headers))
        if not missing:
            return headers
        if self.ref.column_count and len(headers) + len(missing) > self.ref.column_count:
            raise FeishuApiError(f"请在工作表“{self.ref.title}”右侧增加空白列，用于保存需求单和结果")
        # Only append dedicated output headers; never reuse the human 处理备注 column.
        start, end = column_name(len(headers) + 1), column_name(len(headers) + len(missing))
        self.sheets.write_ranges(self.ref.file_token, [(f"{self.ref.table_id}!{start}1:{end}1", [missing])])
        return headers + missing

    def _update(self, record_id: str, values: dict[str, Any]) -> None:
        with self._lock:
            headers, rows = self._read()
            self._find(rows, record_id)
            headers = self._columns(headers, list(values))
            # Re-read after adding headers and re-locate by ID, not a saved row number.
            headers, rows = self._read()
            number, _ = self._find(rows, record_id)
            ranges = [(f"{self.ref.table_id}!{column_name(headers.index(name) + 1)}{number}", [[value]])
                      for name, value in values.items()]
            self.sheets.write_ranges(self.ref.file_token, ranges)

    def update_status(self, record_id: str, status: str, *, proposal_url: str = "", error: str = "") -> None:
        values = {self.settings.status_field: status, self.settings.sheet_note_field: error[:1000]}
        if proposal_url:
            values[self.settings.proposal_field] = proposal_url
        self._update(record_id, values)

    def write_result(self, record_id: str, markdown: str, *, document_url: str = "") -> None:
        values = {self.settings.status_field: self.settings.completed_status,
                  self.settings.result_field: markdown, self.settings.sheet_note_field: ""}
        if document_url:
            values[self.settings.proposal_field] = document_url
        self._update(record_id, values)

    def create_record(self, fields: dict[str, Any], *, client_token: str = "") -> str:
        record_id = "spk_" + (client_token or str(uuid4()))
        with self._lock:
            headers, rows = self._read()
            if any(_text(row.get(self.settings.sheet_id_field)) == record_id for _, row in rows):
                self._find(rows, record_id)
                return record_id
            mapped = {self.settings.sheet_id_field: record_id,
                      self._description_field(headers): fields.get(self.settings.raw_field, ""),
                      self.settings.status_field: fields.get(self.settings.status_field, self.settings.processing_status),
                      self.settings.title_field: fields.get(self.settings.title_field, ""),
                      "提出人ID": person_id(fields.get(self.settings.requester_field))}
            if self.settings.brief_field in fields:
                mapped[self.settings.brief_field] = fields[self.settings.brief_field]
            headers = self._columns(headers, list(mapped))
            row = [mapped.get(name, "") for name in headers]
            self.sheets.append_rows(self.ref.file_token, f"{self.ref.table_id}!A1:{column_name(len(headers))}", [row])
        return record_id


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()

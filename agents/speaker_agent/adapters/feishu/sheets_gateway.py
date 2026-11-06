from __future__ import annotations

import logging
import threading
from typing import Any
from uuid import uuid4

from agents.speaker_agent.adapters.feishu.delivery import FeishuDelivery
from agents.speaker_agent.config import Settings
from agents.speaker_agent.domain.models import DemandRecord
from agents.speaker_agent.persistence.sheet_creations import SheetCreationStore
from shared.feishu.catalog import TableReference
from shared.feishu.contracts import WorkspaceServices
from shared.feishu.errors import FeishuApiError, FeishuRecordNotFoundError
from shared.feishu.sheets import column_name

LOGGER = logging.getLogger(__name__)
INPUT_COLUMNS = ("ID", "版本", "状态", "责任人", "描述", "备注")


class SheetGateway(FeishuDelivery):
    """Map spreadsheet rows to demands; re-locate every write using the ID column."""

    def __init__(self, settings: Settings, ref: TableReference, *, feishu: WorkspaceServices) -> None:
        super().__init__(settings, feishu=feishu)
        self.ref = ref
        self.sheets = feishu.sheets
        self._lock = threading.RLock()
        self.creations = SheetCreationStore(settings.runtime_dir / "sheet-creations.sqlite3")

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
        if not set(INPUT_COLUMNS).issubset(headers):
            raise FeishuApiError(f"工作表“{self.ref.title}”需要 ID、版本、状态、责任人、描述、备注列")
        result: list[tuple[int, dict[str, Any]]] = []
        for number, row in enumerate(rows[1:], start=2):
            fields = {name: row[i] if i < len(row) else "" for i, name in enumerate(headers) if name}
            record_id = _text(fields.get("ID"))
            if not record_id:
                if _text(fields.get("状态")) == self.settings.trigger_status:
                    LOGGER.warning("工作表 %s 第 %s 行缺少 ID，已跳过", self.ref.title, number)
                continue
            result.append((number, fields))
        return headers, result

    def _duplicate_ids(self, rows: list[tuple[int, dict[str, Any]]]) -> dict[str, list[int]]:
        locations: dict[str, list[int]] = {}
        for number, fields in rows:
            record_id = _text(fields.get("ID"))
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

    def _record(self, number: int, fields: dict[str, Any], headers: list[str]) -> DemandRecord:
        raw = _text(fields.get("描述"))
        # Human notes and version are input context; processing notes are never overwritten.
        context = "\n".join(f"{name}：{_text(fields.get(name))}" for name in ("版本", "备注") if _text(fields.get(name)))
        mapped = {**fields, self.settings.raw_field: raw + ("\n" + context if context else "")}
        status = _text(fields.get("状态"))
        status = ({"待修改": self.settings.trigger_status, "待处理": self.settings.trigger_status,
                   "进行中": self.settings.processing_status, "已完成": self.settings.completed_status}).get(status, status)
        return DemandRecord(_text(fields["ID"]), mapped,
                            raw.split("\n")[0][:80] or "未命名需求",
                            "", status,
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
        return [record for number, fields in rows
                if _text(fields.get("ID")) not in duplicates
                and (record := self._record(number, fields, headers)).status == status]

    def _find(self, rows: list[tuple[int, dict[str, Any]]], record_id: str) -> tuple[int, dict[str, Any]]:
        matches = [(number, fields) for number, fields in rows
                   if _text(fields.get("ID")) == record_id]
        if len(matches) > 1:
            details = self._duplicate_details({record_id: [number for number, _ in matches]})
            raise FeishuApiError(f"工作表“{self.ref.title}”的 ID 不唯一：{details}；拒绝处理该 ID，避免回填错误")
        if not matches:
            raise FeishuRecordNotFoundError(
                record_id, f"工作表“{self.ref.title}”中找不到记录 ID {record_id!r}；"
                "请核对该记录是否仍在表中，或 ID 是否发生变化")
        return matches[0]

    def _update(self, record_id: str, values: dict[str, Any], *, result: str = "") -> None:
        with self._lock:
            headers, rows = self._read()
            self._find(rows, record_id)
            if result and "处理备注" not in headers:
                if len(headers) != 6:
                    raise FeishuApiError("缺少处理备注列，请确认表头；不会覆盖其他列")
                self.sheets.write_ranges(self.ref.file_token, [(f"{self.ref.table_id}!G1:G1", [["处理备注"]])])
            # 再次按 ID 定位，不能沿用先前的行号。
            headers, rows = self._read()
            number, current = self._find(rows, record_id)
            if result:
                existing = _text(current.get("处理备注"))
                values["处理备注"] = existing if result in existing else "\n\n".join(filter(None, (existing, result)))
            ranges = [(f"{self.ref.table_id}!{column_name(headers.index(name) + 1)}{number}:{column_name(headers.index(name) + 1)}{number}", [[value]])
                      for name, value in values.items()]
            self.sheets.write_ranges(self.ref.file_token, ranges)

    def update_status(self, record_id: str, status: str, *, proposal_url: str = "", error: str = "") -> None:
        # 台账状态代表开发进度，不能用 Speaker 的分析进度覆盖。
        self.get_record(record_id)

    def write_result(self, record_id: str, markdown: str, *, document_url: str = "") -> None:
        result = markdown.strip() + (f"\n\n文档：{document_url}" if document_url else "")
        self._update(record_id, {}, result=result)

    def _options(self, column: str, row: int) -> set[str] | None:
        # 新行可能尚未创建；读取最后一个现有行的规则作为追加行的约束。
        target_row = min(row, self.ref.row_count) if self.ref.row_count else row
        rules = self.sheets.read_dropdowns(self.ref.file_token,
                                          f"{self.ref.table_id}!{column}{target_row}:{column}{target_row}")
        if not rules and target_row > 2 and self.ref.row_count and row > self.ref.row_count:
            rules = self.sheets.read_dropdowns(self.ref.file_token, f"{self.ref.table_id}!{column}2:{column}2")
        options: set[str] | None = None
        for rule in rules:
            values = rule.get("conditionValues")
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise FeishuApiError("下拉选项无法读取，保留需求待确认，不修改下拉规则")
            choices = set(values)
            options = choices if options is None else options & choices
        return options

    def create_record(self, fields: dict[str, Any], *, client_token: str = "") -> str:
        token = client_token or str(uuid4())
        with self._lock:
            headers, rows = self._read()
            # Reuse older successful writes without ever generating another UUID in the sheet.
            legacy_id = "spk_" + token
            if any(_text(row.get("ID")) == legacy_id for _, row in rows):
                self._find(rows, legacy_id)
                return legacy_id
            saved = self.creations.get(self.ref.key, token)
            if saved and saved["state"] != "prepared":
                return self._recover_creation(token, saved, rows)
            for _ in range(3):
                headers, rows = self._read()
                target, append = self._vacancy()
                mapped = {"描述": fields.get(self.settings.raw_field, ""),
                          **{name: fields.get(name, "") for name in ("版本", "责任人", "备注")}}
                for name in ("版本", "状态", "责任人"):
                    options = self._options(column_name(headers.index(name) + 1), target)
                    if name == "状态":
                        candidates = ("待修改", "待处理", self.settings.trigger_status)
                        mapped[name] = next((value for value in candidates if options is None or value in options), "")
                        if not mapped[name]:
                            raise FeishuApiError(f"工作表“{self.ref.title}”没有可用的待处理状态，请确认现有下拉选项")
                    value = _text(mapped[name])
                    if value and options is not None and value not in options:
                        raise FeishuApiError(f"工作表“{self.ref.title}”的{name}不在已有下拉选项中，请确认；不会新增选项")
                numbers = [int(value) for _, current in rows
                           if (value := _text(current.get("ID"))).isascii() and value.isdecimal()]
                saved = self.creations.reserve(self.ref.key, token, max(numbers, default=0) + 1, mapped)
                current_headers, current_rows = self._read()
                if saved["state"] != "prepared":
                    return self._recover_creation(token, saved, current_rows)
                record_id = str(saved["number"])
                # Another editor may fill the vacancy, insert a row, or claim our number.
                # Re-select before starting a write; never replay an uncertain write.
                if (current_headers != headers or self._vacancy() != (target, append)
                        or any(_text(current.get("ID")) == record_id for _, current in current_rows)):
                    continue
                if not self.creations.start(self.ref.key, token):
                    raise FeishuApiError(f"需求编号 {record_id} 已开始写入，等待回读确认；不会重复写入")
                if append:
                    width = max(headers.index(name) for name in INPUT_COLUMNS) + 1
                    row = [saved["fields"].get(name) if _text(saved["fields"].get(name)) else None
                           for name in headers[:width]]
                    self.sheets.append_rows(self.ref.file_token, f"{self.ref.table_id}!A1:{column_name(width)}", [row])
                else:
                    ranges = []
                    for name, value in saved["fields"].items():
                        if not _text(value):
                            continue
                        column = column_name(headers.index(name) + 1)
                        ranges.append((f"{self.ref.table_id}!{column}{target}:{column}{target}", [[value]]))
                    self.sheets.write_ranges(self.ref.file_token, ranges)
                # A success response alone does not prove that the complete record is readable.
                _, written = self._read()
                return self._recover_creation(token, {**saved, "state": "sending"}, written)
            raise FeishuApiError(f"工作表“{self.ref.title}”正在被编辑，稍后重新寻找空行和可用编号")

    def _vacancy(self) -> tuple[int, bool]:
        # Formula rendering protects cells such as ="" that appear empty in the UI.
        # Inspect the entire row, including unlabelled columns and human notes.
        rows = self.sheets.read_range(self.ref.file_token, self.ref.table_id, value_render_option="Formula")
        if not rows:
            raise FeishuApiError(f"工作表“{self.ref.title}”暂时无法读取，稍后重试")
        for number, row in enumerate(rows[1:], 2):
            if all(value is None or value == "" for value in row):
                return number, False
        number = len(rows) + 1
        return number, bool(self.ref.row_count and number > self.ref.row_count)

    def _recover_creation(self, token: str, saved: dict[str, Any],
                          rows: list[tuple[int, dict[str, Any]]]) -> str:
        record_id = str(saved["number"])
        if saved["state"] == "confirmed":
            return record_id
        # A failed response may still have written the row. Never replay an uncertain write.
        _, current = self._find(rows, record_id)
        if any(_text(current.get(name)) != _text(saved["fields"].get(name)) for name in INPUT_COLUMNS):
            raise FeishuApiError(f"工作表“{self.ref.title}”的编号 {record_id} 与原写入内容不一致，暂停确认；不会改绑人工记录")
        self.creations.confirm(self.ref.key, token)
        return record_id


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()

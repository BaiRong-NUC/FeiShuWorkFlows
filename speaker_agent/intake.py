from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .config import Settings
from .contracts import RequirementBrief
from .models import DemandRecord, IncomingDemand


DETAIL_FIELDS = (
    "需求背景",
    "业务目标",
    "功能要求",
    "非功能要求",
    "约束条件",
    "验收标准",
    "优先级",
    "期望上线时间",
    "相关链接",
)


class RequirementNormalizer:
    """Turn ledger rows and chat messages into one requirement brief."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def from_record(self, record: DemandRecord, *, source: str = "台账") -> RequirementBrief:
        fields = record.fields
        requirements = _join(
            _text(fields.get("功能要求")),
            _text(fields.get("非功能要求")),
        )
        brief = RequirementBrief(
            record_id=record.record_id,
            title=record.title,
            requester_id=record.requester_id,
            source=source,
            background=_text(fields.get("需求背景")),
            goal=_text(fields.get("业务目标")),
            requirements=requirements,
            constraints=_text(fields.get("约束条件")),
            acceptance=_text(fields.get("验收标准")),
            priority=_text(fields.get("优先级")),
            due_date=_text(fields.get("期望上线时间")),
            links=_text(fields.get("相关链接")),
            record_url=record.record_url,
        )
        return RequirementBrief(
            **{**brief.__dict__, "missing_fields": _missing(brief)}
        )

    def from_message(self, demand: IncomingDemand) -> RequirementBrief:
        sections = _sections(demand.text)
        title = _labeled_value(demand.text, "需求标题") or _title_from_text(demand.text)
        requirements = _join(
            sections.get("功能要求", ""),
            sections.get("非功能要求", ""),
        )
        if not any(sections.values()):
            requirements = demand.text
        brief = RequirementBrief(
            record_id="",
            title=title,
            requester_id=demand.sender_id,
            source="飞书会话",
            background=sections.get("需求背景", ""),
            goal=sections.get("业务目标", ""),
            requirements=requirements,
            constraints=sections.get("约束条件", ""),
            acceptance=sections.get("验收标准", ""),
            priority=sections.get("优先级", ""),
            due_date=sections.get("期望上线时间", ""),
            links=sections.get("相关链接", ""),
            record_url="",
        )
        return RequirementBrief(
            **{**brief.__dict__, "missing_fields": _missing(brief)}
        )

    def ledger_fields(self, brief: RequirementBrief) -> dict[str, Any]:
        fields: dict[str, Any] = {
            self.settings.title_field: brief.title,
            self.settings.status_field: self.settings.processing_status,
            "需求背景": brief.background,
            "业务目标": brief.goal,
            "功能要求": brief.requirements,
            "约束条件": brief.constraints,
            "验收标准": brief.acceptance,
            "优先级": brief.priority,
            "期望上线时间": brief.due_date,
            "相关链接": brief.links,
            "来源": brief.source,
        }
        if brief.requester_id:
            fields[self.settings.requester_field] = [{"id": brief.requester_id}]
        return {key: value for key, value in fields.items() if _present(value)}


def _missing(brief: RequirementBrief) -> tuple[str, ...]:
    required = {
        "业务目标": brief.goal,
        "功能要求": brief.requirements,
        "验收标准": brief.acceptance,
    }
    return tuple(name for name, value in required.items() if not value)


def _sections(text: str) -> dict[str, str]:
    result = {name: "" for name in DETAIL_FIELDS}
    current = ""
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("-").strip()
        matched = _split_labeled_line(line)
        if matched is None:
            if current:
                result[current] = _join(result[current], line)
            continue
        name, value = matched
        if name in result:
            current = name
            result[name] = value
        elif current:
            result[current] = _join(result[current], line)
    return result


def _labeled_value(text: str, name: str) -> str:
    for raw_line in text.splitlines():
        matched = _split_labeled_line(raw_line.strip())
        if matched is not None and matched[0] == name:
            return matched[1]
    return ""


def _split_labeled_line(line: str) -> tuple[str, str] | None:
    for separator in ("：", ":"):
        if separator not in line:
            continue
        name, value = line.split(separator, 1)
        return name.strip().strip("*").strip(), value.strip()
    return None


def _title_from_text(text: str) -> str:
    for line in text.splitlines():
        cleaned = line.strip().lstrip("#").lstrip("-").strip()
        if cleaned:
            return cleaned[:80]
    return "未命名需求"


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "、".join(item for item in (_text(item) for item in value) if item)
    if isinstance(value, Mapping):
        for key in ("text", "name", "value", "link", "url"):
            if key in value:
                found = _text(value.get(key))
                if found:
                    return found
    return str(value).strip()


def _join(*values: str) -> str:
    return "\n".join(value.strip() for value in values if value and value.strip())


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


def plain_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _compact(value)
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [plain_text(item) for item in value]
        return "、".join(part for part in parts if part)
    if isinstance(value, Mapping):
        for key in ("text", "name", "value", "link", "url"):
            if key in value:
                text = plain_text(value.get(key))
                if text:
                    return text
        parts = [plain_text(item) for item in value.values()]
        return "、".join(part for part in parts if part)
    return _compact(str(value))

def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()

def person_id(value: Any) -> str:
    people = value if isinstance(value, list) else [value]
    for person in people:
        if isinstance(person, Mapping):
            for key in ("id", "open_id"):
                found = str(person.get(key) or "").strip()
                if found:
                    return found
    return ""

def escape_formula(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')

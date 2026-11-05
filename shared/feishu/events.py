from __future__ import annotations

import json


def parse_text_content(content: str) -> str | None:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and isinstance(payload.get("text"), str):
        return payload["text"]
    return None


def event_field_text(value: object) -> str:
    if isinstance(value, str):
        if value.startswith(('"', '{', '[')):
            try:
                return event_field_text(json.loads(value))
            except json.JSONDecodeError:
                pass
        return value.strip()
    if isinstance(value, dict):
        return event_field_text(value.get("name", value.get("text", value.get("value", ""))))
    if isinstance(value, list):
        return "、".join(event_field_text(item) for item in value)
    text = getattr(value, "text", None)
    if isinstance(text, str):
        return text.strip()
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name.strip()
    return str(value).strip()


def bot_mentioned(mentions: object, bot_open_id: str) -> bool:
    if not bot_open_id or not isinstance(mentions, list):
        return False
    return any(
        getattr(getattr(mention, "id", None), "open_id", "") == bot_open_id
        for mention in mentions
    )


def strip_mentions(text: str, mentions: object, bot_open_id: str) -> str:
    if not isinstance(mentions, list):
        return text.strip()
    result = text
    for mention in mentions:
        key = str(getattr(mention, "key", "") or "")
        if key:
            is_bot = getattr(getattr(mention, "id", None), "open_id", "") == bot_open_id
            result = result.replace(key, " " if is_bot else "@" + str(getattr(mention, "name", key)))
    return "\n".join(line.strip() for line in result.splitlines()).strip()

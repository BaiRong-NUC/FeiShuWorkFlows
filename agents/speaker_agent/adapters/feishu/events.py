from __future__ import annotations

import json
import logging

import lark_oapi as lark
from lark_oapi.event.dispatcher_handler import EventDispatcherHandlerBuilder

from agents.speaker_agent.orchestration.service import DemandAgent


LOGGER = logging.getLogger(__name__)


def register_handlers(builder: EventDispatcherHandlerBuilder, agent: DemandAgent):
    builder.register_p2_drive_file_bitable_record_changed_v1(
        lambda data: on_record_changed(agent, data)
    )
    if agent.settings.enable_chat_commands:
        builder.register_p2_im_message_receive_v1(
            lambda data: on_message(agent, data)
        )
    return builder


def on_record_changed(agent: DemandAgent, data: lark.drive.v1.P2DriveFileBitableRecordChangedV1) -> None:
    event = data.event
    header = data.header
    if event is None or header is None:
        return
    if event.file_token != agent.settings.bitable_app_token:
        return
    if event.table_id != agent.settings.bitable_table_id:
        return
    if event.action_list is None:
        return

    event_id = header.event_id or ""
    for action in event.action_list:
        if action.action == "record_deleted" or not action.record_id:
            continue
        if not _status_triggered(agent, action):
            continue
        agent.enqueue_record(
            action.record_id,
            revision=event.revision,
            event_id=event_id,
        )


def on_message(agent: DemandAgent, data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    event = data.event
    if event is None or event.message is None:
        return
    message = event.message
    if message.message_type != "text" or not message.message_id or not message.content:
        return
    sender = getattr(event, "sender", None)
    if getattr(sender, "sender_type", "user") != "user":
        return
    if message.chat_type == "group" and not _bot_mentioned(message.mentions, agent.settings.bot_open_id):
        return
    try:
        payload = json.loads(message.content)
    except json.JSONDecodeError:
        LOGGER.warning("ignored non-JSON Feishu text message %s", message.message_id)
        return
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        return
    text = _strip_mentions(payload["text"], message.mentions, agent.settings.bot_open_id)
    sender_id = str(getattr(getattr(sender, "sender_id", None), "open_id", "") or "")
    agent.handle_command(text, message.message_id, sender_id, getattr(message, "chat_id", ""))


def _status_triggered(agent: DemandAgent, action: object) -> bool:
    target = agent.settings.trigger_status
    after = _status_values(getattr(action, "after_value", None), agent.settings.status_field_id)
    before = _status_values(getattr(action, "before_value", None), agent.settings.status_field_id)
    if not after:
        # Some tenants only include field IDs in the event. Reading the full
        # record later remains the authoritative status check.
        return True
    return target in after and target not in before


def _status_values(fields: object, status_field_id: str) -> set[str]:
    if not status_field_id or not isinstance(fields, list):
        return set()
    values: set[str] = set()
    for field in fields:
        # field_identity_value contains user identities, not the field name.
        if getattr(field, "field_id", None) != status_field_id:
            continue
        raw_value = getattr(field, "field_value", None)
        if raw_value is None:
            continue
        values.add(_field_text(raw_value))
    return {value for value in values if value}


def _field_text(value: object) -> str:
    if isinstance(value, str):
        if value.startswith(('"', '{', '[')):
            try:
                return _field_text(json.loads(value))
            except json.JSONDecodeError:
                pass
        return value.strip()
    if isinstance(value, dict):
        return _field_text(value.get("name", value.get("text", value.get("value", ""))))
    if isinstance(value, list):
        return "、".join(_field_text(item) for item in value)
    text = getattr(value, "text", None)
    if isinstance(text, str):
        return text.strip()
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name.strip()
    return str(value).strip()


def _bot_mentioned(mentions: object, bot_open_id: str) -> bool:
    if not bot_open_id or not isinstance(mentions, list):
        return False
    return any(
        getattr(getattr(mention, "id", None), "open_id", "") == bot_open_id
        for mention in mentions
    )


def _strip_mentions(text: str, mentions: object, bot_open_id: str) -> str:
    if not isinstance(mentions, list):
        return text.strip()
    result = text
    for mention in mentions:
        key = str(getattr(mention, "key", "") or "")
        if key:
            is_bot = getattr(getattr(mention, "id", None), "open_id", "") == bot_open_id
            result = result.replace(key, " " if is_bot else "@" + str(getattr(mention, "name", key)))
    return "\n".join(line.strip() for line in result.splitlines()).strip()

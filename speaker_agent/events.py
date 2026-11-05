from __future__ import annotations

import json
import logging

import lark_oapi as lark

from .service import DemandAgent


LOGGER = logging.getLogger(__name__)


def register_handlers(builder: lark.EventDispatcherHandlerBuilder, agent: DemandAgent):
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
            event_id=f"{event_id}:{action.record_id}" if event_id else "",
        )


def on_message(agent: DemandAgent, data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    event = data.event
    if event is None or event.message is None:
        return
    message = event.message
    if message.message_type != "text" or not message.message_id or not message.content:
        return
    if message.chat_type == "group" and not _bot_mentioned(message.mentions):
        return
    try:
        payload = json.loads(message.content)
    except json.JSONDecodeError:
        LOGGER.warning("ignored non-JSON Feishu text message %s", message.message_id)
        return
    text = str(payload.get("text") or "")
    text = _strip_mentions(text, message.mentions)
    sender = getattr(event, "sender", None)
    sender_id = str(getattr(getattr(sender, "sender_id", None), "open_id", "") or "")
    agent.handle_command(text, message.message_id, sender_id)


def _status_triggered(agent: DemandAgent, action: object) -> bool:
    target = agent.settings.trigger_status
    after = _status_values(getattr(action, "after_value", None), agent.settings.status_field)
    before = _status_values(getattr(action, "before_value", None), agent.settings.status_field)
    if not after:
        # Some tenants only include field IDs in the event. Reading the full
        # record later remains the authoritative status check.
        return True
    return target in after and target not in before


def _status_values(fields: object, status_field: str) -> set[str]:
    if not isinstance(fields, list):
        return set()
    values: set[str] = set()
    for field in fields:
        identity = getattr(field, "field_identity_value", None)
        identity_name = identity if isinstance(identity, str) else str(getattr(identity, "name", "") or "")
        if identity_name != status_field:
            continue
        raw_value = getattr(field, "field_value", None)
        if raw_value is None:
            continue
        values.add(_field_text(raw_value))
    return {value for value in values if value}


def _field_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    text = getattr(value, "text", None)
    if isinstance(text, str):
        return text.strip()
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name.strip()
    return str(value).strip()


def _bot_mentioned(mentions: object) -> bool:
    if not isinstance(mentions, list):
        return False
    return any(
        str(getattr(getattr(mention, "id", None), "open_id", "") or "").startswith("ou_")
        or getattr(mention, "key", None)
        for mention in mentions
    )


def _strip_mentions(text: str, mentions: object) -> str:
    if not isinstance(mentions, list):
        return text.strip()
    result = text
    for mention in mentions:
        key = str(getattr(mention, "key", "") or "")
        if key:
            result = result.replace(key, " ")
    return " ".join(result.split())

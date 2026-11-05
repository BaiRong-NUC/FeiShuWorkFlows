from __future__ import annotations

import lark_oapi as lark
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler, EventDispatcherHandlerBuilder

from shared.feishu.config import FeishuConfig


def new_event_dispatcher() -> EventDispatcherHandlerBuilder:
    return lark.EventDispatcherHandler.builder("", "")


def create_socket_client(config: FeishuConfig, handler: EventDispatcherHandler, *, log_level: str = "INFO") -> lark.ws.Client:
    config.validate()
    return lark.ws.Client(
        config.app_id,
        config.app_secret,
        event_handler=handler,
        log_level=getattr(lark.LogLevel, log_level, lark.LogLevel.INFO),
    )

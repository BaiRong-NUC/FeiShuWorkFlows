from __future__ import annotations

import logging
import time

from agents.speaker_agent.persistence.personal import PersonalInbox
from shared.feishu.personal import PersonalChatAPI

LOGGER = logging.getLogger(__name__)


class PersonalPoller:
    def __init__(self, api: PersonalChatAPI, inbox: PersonalInbox, own_id: str, *, include_self: bool = False) -> None:
        self.api, self.inbox, self.own_id = api, inbox, own_id
        self.include_self = include_self

    def poll(self) -> None:
        end = int(time.time()) - 2
        chats = self.api.chats()
        fetched, accepted, failures = 0, 0, 0
        for chat in chats:
            if chat.peer_id == self.own_id and not self.include_self:
                continue
            start = self.inbox.cursor(chat.chat_id)
            if start >= end:
                continue
            try:
                messages = self.api.messages(chat, start, end)
                fetched += len(messages)
                accepted += self.inbox.stage(chat, messages, end)
            except Exception:
                failures += 1
                LOGGER.exception("个人会话读取失败，保留该会话游标供下次重试：%s", chat.chat_id)
        LOGGER.info("个人消息轮询：发现单聊=%s，读取文本=%s，新增入队=%s，重复或过滤=%s，失败会话=%s",
                    len(chats), fetched, accepted, fetched - accepted, failures)

from __future__ import annotations

import logging
import time

from agents.speaker_agent.persistence.personal import PersonalInbox
from shared.feishu.personal import PersonalChatAPI

LOGGER = logging.getLogger(__name__)


class PersonalPoller:
    def __init__(self, api: PersonalChatAPI, inbox: PersonalInbox, own_id: str) -> None:
        self.api, self.inbox, self.own_id = api, inbox, own_id

    def poll(self) -> None:
        end = int(time.time()) - 2
        for chat in self.api.chats():
            if chat.peer_id == self.own_id:
                continue
            start = self.inbox.cursor(chat.chat_id)
            if start >= end:
                continue
            try:
                messages = self.api.messages(chat, start, end)
                self.inbox.stage(chat, messages, end)
            except Exception:
                LOGGER.exception("个人会话读取失败，保留该会话游标供下次重试：%s", chat.chat_id)

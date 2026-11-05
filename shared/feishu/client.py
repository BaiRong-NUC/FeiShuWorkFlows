from __future__ import annotations

import lark_oapi as lark

from shared.feishu.bitable import BitableClient
from shared.feishu.config import BitableConfig, FeishuConfig
from shared.feishu.documents import DocumentClient
from shared.feishu.messages import MessageClient


class FeishuClient:
    """Share one SDK client across message, document and table APIs."""

    def __init__(self, config: FeishuConfig, *, client: lark.Client | None = None) -> None:
        config.validate()
        self.client = client if client is not None else (
            lark.Client.builder().app_id(config.app_id).app_secret(config.app_secret).build()
        )
        self.messages = MessageClient(self.client)
        self.documents = DocumentClient(self.client)

    def table(self, config: BitableConfig) -> BitableClient:
        return BitableClient(self.client, config)

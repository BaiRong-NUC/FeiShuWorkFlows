from __future__ import annotations

import lark_oapi as lark

from shared.feishu.bitable import BitableClient
from shared.feishu.config import BitableConfig, FeishuConfig
from shared.feishu.documents import DocumentClient
from shared.feishu.messages import MessageClient
from shared.feishu.catalog import CatalogClient
from shared.feishu.sheets import SheetsClient
from shared.feishu.auth.oauth import AccessTokenProvider


class FeishuClient:
    """Share one SDK client across message, document and table APIs."""

    def __init__(self, config: FeishuConfig, *, client: lark.Client | None = None,
                 user_tokens: AccessTokenProvider | None = None) -> None:
        config.validate()
        self.client = client if client is not None else (
            lark.Client.builder().app_id(config.app_id).app_secret(config.app_secret)
            .enable_set_token(True).timeout(30).build()
        )
        self.messages = MessageClient(self.client, user_tokens=user_tokens)
        self.documents = DocumentClient(self.client)
        self.catalog = CatalogClient(self.client)
        self.sheets = SheetsClient(self.client)

    def table(self, config: BitableConfig) -> BitableClient:
        return BitableClient(self.client, config)

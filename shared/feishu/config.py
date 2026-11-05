from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class FeishuConfig:
    app_id: str
    app_secret: str = field(repr=False)

    @classmethod
    def from_env(cls) -> FeishuConfig:
        config = cls(os.getenv("FEISHU_APP_ID", "").strip(), os.getenv("FEISHU_APP_SECRET", "").strip())
        config.validate()
        return config

    def validate(self) -> None:
        if not self.app_id or not self.app_secret:
            raise ValueError("请配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")


@dataclass(frozen=True)
class BitableConfig:
    app_token: str
    table_id: str
    url: str = ""

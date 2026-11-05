from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass(frozen=True)
class DeepSeekConfig:
    api_key: str = field(default="", repr=False)
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    timeout_seconds: int = 45
    max_tokens: int = 4096

    @classmethod
    def from_env(cls) -> DeepSeekConfig:
        return cls(
            api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip().rstrip("/"),
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip(),
            timeout_seconds=int(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "45")),
            max_tokens=int(os.getenv("DEEPSEEK_MAX_TOKENS", "4096")),
        )

    def validate(self) -> None:
        if not self.api_key:
            raise ValueError("请在 .env 中设置 DEEPSEEK_API_KEY")
        if not self.api_key.isascii() or any(char.isspace() for char in self.api_key):
            raise ValueError("DEEPSEEK_API_KEY 不能包含空白或非 ASCII 字符")
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("DEEPSEEK_BASE_URL 必须是无凭据、查询参数的 HTTPS 地址")
        if not self.model or not 1 <= self.timeout_seconds <= 120:
            raise ValueError("请配置 DEEPSEEK_MODEL，超时须在 1–120 秒之间")
        if not 256 <= self.max_tokens <= 16384:
            raise ValueError("DEEPSEEK_MAX_TOKENS 须在 256–16384 之间")

from __future__ import annotations

import json
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from shared.llm.config import DeepSeekConfig
from shared.llm.contracts import ModelError


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DeepSeekClient:
    """Chat Completions JSON output; transport performs no hidden retries."""

    def __init__(self, settings: DeepSeekConfig) -> None:
        settings.validate()
        self.settings = settings

    def complete(self, system_prompt: str, user_text: str) -> str:
        payload = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "max_tokens": self.settings.max_tokens,
            "stream": False,
        }
        request = Request(
            self.settings.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.settings.api_key,
                     "Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with build_opener(_NoRedirect()).open(request, timeout=self.settings.timeout_seconds) as response:
                raw = response.read(512_001)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise ModelError(f"DeepSeek 请求失败（HTTP {status}）", retryable=status in {408, 429} or status >= 500) from None
        except (URLError, OSError, TimeoutError, HTTPException):
            raise ModelError("DeepSeek 连接失败或超时") from None
        if len(raw) > 512_000:
            raise ModelError("DeepSeek 响应超过大小限制")
        try:
            data = json.loads(raw)
            choice = data["choices"][0]
            content = choice["message"]["content"]
            if choice.get("finish_reason") != "stop":
                raise ModelError("DeepSeek 输出未完整结束")
            if not isinstance(content, str) or not content.strip():
                raise ModelError("DeepSeek 返回了空内容")
            return content
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise ModelError("DeepSeek 响应结构无效") from None

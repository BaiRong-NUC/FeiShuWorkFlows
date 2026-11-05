"""Generic Sheets v2 values APIs, authenticated through the existing SDK."""
from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import quote

import lark_oapi as lark
from lark_oapi.core.enum import AccessTokenType, HttpMethod
from lark_oapi.core.model import BaseRequest

from shared.feishu.errors import FeishuApiError

LOGGER = logging.getLogger(__name__)


def column_name(number: int) -> str:
    if number < 1:
        raise ValueError("列号从 1 开始")
    result = ""
    while number:
        number, digit = divmod(number - 1, 26)
        result = chr(65 + digit) + result
    return result


class SheetsClient:
    def __init__(self, client: lark.Client) -> None:
        self.client = client

    def _request(self, token: str, suffix: str, method: HttpMethod, *,
                 body: dict[str, Any] | None = None, queries: list[tuple[str, str]] | None = None) -> dict[str, Any]:
        request = (BaseRequest.builder().http_method(method)
                   .uri(f"/open-apis/sheets/v2/spreadsheets/{quote(token, safe='')}/{suffix}")
                   .token_types({AccessTokenType.TENANT}).body(body).queries(queries or []).build())
        # 仅重试读取：追加行的响应可能丢失，重放写请求会重复建档。
        response = self.client.request(request)
        for attempt in range(2):
            if response.success() or method != HttpMethod.GET or response.code != 95203:
                break
            LOGGER.warning("电子表格读取遇到暂时性内部错误，%s 秒后重试（%s/2）：code=%s, log_id=%s",
                           2 ** attempt, attempt + 1, response.code, response.get_log_id())
            time.sleep(2 ** attempt)
            response = self.client.request(request)
        if not response.success():
            raise FeishuApiError(f"电子表格接口失败：code={response.code}, log_id={response.get_log_id()}")
        if response.raw is None or not response.raw.content:
            raise FeishuApiError("电子表格接口没有返回响应正文")
        payload = json.loads(response.raw.content)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise FeishuApiError("电子表格接口没有返回 data")
        return data

    def read_range(self, token: str, range_name: str) -> list[list[Any]]:
        data = self._request(token, "values/" + quote(range_name, safe=""), HttpMethod.GET,
                             queries=[("valueRenderOption", "ToString"), ("dateTimeRenderOption", "FormattedString")])
        value_range = data.get("valueRange")
        if not isinstance(value_range, dict):
            raise FeishuApiError("电子表格接口没有返回 valueRange")
        values = value_range.get("values") or []
        if not isinstance(values, list) or any(not isinstance(row, list) for row in values):
            raise FeishuApiError("电子表格数据格式无效")
        return values

    def write_ranges(self, token: str, ranges: list[tuple[str, list[list[Any]]]]) -> None:
        self._request(token, "values_batch_update", HttpMethod.POST,
                      body={"valueRanges": [{"range": name, "values": rows} for name, rows in ranges]})

    def append_rows(self, token: str, range_name: str, rows: list[list[Any]]) -> None:
        self._request(token, "values_append", HttpMethod.POST,
                      body={"valueRange": {"range": range_name, "values": rows}, "insertDataOption": "INSERT_ROWS"})

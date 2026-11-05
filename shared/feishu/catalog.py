"""Resolve document links and enumerate tables, without Agent-specific schemas."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit, urlunsplit

import lark_oapi as lark
from lark_oapi.api.bitable.v1 import ListAppTableRequest
from lark_oapi.api.sheets.v3 import QuerySpreadsheetSheetRequest
from lark_oapi.api.wiki.v2 import GetNodeSpaceRequest

from shared.feishu.errors import FeishuApiError, require_service, required_data


def parse_file_url(url: str) -> tuple[str, str, str]:
    parsed = urlsplit(url.strip())
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or not host.endswith((".feishu.cn", ".larksuite.com", ".larkoffice.com"))):
        raise ValueError("台账链接必须是飞书 HTTPS 文件链接")
    match = re.fullmatch(r"/(wiki|base|sheets)/([A-Za-z0-9_-]+)/?", parsed.path)
    if not match:
        raise ValueError("台账链接须包含 /wiki/、/base/ 或 /sheets/")
    # The sheet/table in a copied link is the currently visible tab, not a filter.
    return match[1], match[2], urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


@dataclass(frozen=True)
class TableReference:
    kind: str
    file_token: str
    table_id: str
    title: str
    file_url: str
    file_title: str = ""
    row_count: int = 0
    column_count: int = 0

    @property
    def key(self) -> str:
        return f"{self.kind}/{self.file_token}/{self.table_id}"

    @property
    def url(self) -> str:
        return self.file_url + "?" + urlencode({"sheet" if self.kind == "sheet" else "table": self.table_id})


class CatalogClient:
    def __init__(self, client: lark.Client) -> None:
        self.client = client

    def discover(self, urls: tuple[str, ...]) -> list[TableReference]:
        tables: dict[str, TableReference] = {}
        files: set[tuple[str, str]] = set()
        for url in urls:
            kind, token, file_url = parse_file_url(url)
            title = ""
            if kind == "wiki":
                request = GetNodeSpaceRequest.builder().token(token).obj_type("wiki").build()
                node = required_data(require_service(self.client.wiki).v2.space.get_node(request), "resolve wiki node").node
                if node is None or not node.obj_token or not node.obj_type:
                    raise FeishuApiError("知识库节点未返回文件类型或文件 token")
                kind, token, title = node.obj_type, node.obj_token, node.title or ""
            kind = {"base": "bitable", "sheets": "sheet"}.get(kind, kind)
            if kind not in {"sheet", "bitable"}:
                raise FeishuApiError(f"链接指向 {kind}，需要电子表格或多维表格文件")
            if (kind, token) in files:
                continue
            files.add((kind, token))
            if kind == "sheet":
                request = QuerySpreadsheetSheetRequest.builder().spreadsheet_token(token).build()
                data = required_data(require_service(self.client.sheets).v3.spreadsheet_sheet.query(request), "list worksheets")
                for sheet in data.sheets or []:
                    if not sheet.sheet_id or sheet.resource_type not in {None, "", "sheet"}:
                        continue
                    grid = sheet.grid_properties
                    ref = TableReference(kind, token, sheet.sheet_id, sheet.title or sheet.sheet_id, file_url,
                                         title, (grid.row_count or 0) if grid else 0,
                                         (grid.column_count or 0) if grid else 0)
                    tables[ref.key] = ref
            else:
                page = ""
                seen: set[str] = set()
                while True:
                    builder = ListAppTableRequest.builder().app_token(token).page_size(100)
                    if page:
                        builder.page_token(page)
                    data = required_data(require_service(self.client.bitable).v1.app_table.list(builder.build()), "list Bitable tables")
                    for table in data.items or []:
                        if table.table_id:
                            ref = TableReference(kind, token, table.table_id, table.name or table.table_id, file_url, title)
                            tables[ref.key] = ref
                    if not data.has_more:
                        break
                    if not data.page_token or data.page_token in seen:
                        raise FeishuApiError("多维表格分页游标无效，未完成全部数据表发现")
                    page = data.page_token
                    seen.add(page)
        return list(tables.values())

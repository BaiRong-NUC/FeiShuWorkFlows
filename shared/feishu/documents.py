from __future__ import annotations

import lark_oapi as lark

from lark_oapi.api.docx.v1 import (
    Block,
    ConvertDocumentRequest,
    ConvertDocumentRequestBody,
    CreateDocumentBlockDescendantRequest,
    CreateDocumentBlockDescendantRequestBody,
    CreateDocumentRequest,
    CreateDocumentRequestBody,
)
from lark_oapi.api.drive.v1 import (
    BaseMember,
    CreatePermissionMemberRequest,
)

from shared.feishu.errors import FeishuApiError, require_service, required_data


class DocumentClient:
    def __init__(self, client: lark.Client) -> None:
        self.client = client

    def create_markdown_document(self, title: str, markdown: str, *, folder_token: str = "") -> tuple[str, str]:
        body_builder = CreateDocumentRequestBody.builder().title(title[:800])
        if folder_token:
            body_builder.folder_token(folder_token)
        create_request = (
            CreateDocumentRequest.builder().request_body(body_builder.build()).build()
        )
        create_response = require_service(self.client.docx).v1.document.create(create_request)
        document = required_data(create_response, "create Feishu document").document
        if document is None or not document.document_id:
            raise FeishuApiError("Feishu document creation returned no document ID")
        document_id = document.document_id

        converted = self._convert_markdown(markdown)
        if converted is None:
            raise FeishuApiError("Feishu Markdown conversion returned no content")
        if converted:
            children_id, descendants = converted
            descendant_body = (
                CreateDocumentBlockDescendantRequestBody.builder()
                .children_id(children_id)
                .descendants(descendants)
                .index(0)
                .build()
            )
            descendant_request = (
                CreateDocumentBlockDescendantRequest.builder()
                .document_id(document_id)
                .block_id(document_id)
                .document_revision_id(-1)
                .request_body(descendant_body)
                .build()
            )
            descendant_response = require_service(self.client.docx).v1.document_block_descendant.create(
                descendant_request
            )
            required_data(descendant_response, "write Feishu document")

        url = f"https://www.feishu.cn/docx/{document_id}"
        return document_id, url

    def grant_document_view(self, document_token: str, open_id: str) -> None:
        if not open_id:
            return
        member = (
            BaseMember.builder()
            .member_type("openid")
            .member_id(open_id)
            .perm("view")
            .perm_type("container")
            .type("user")
            .build()
        )
        request = (
            CreatePermissionMemberRequest.builder()
            .token(document_token)
            .type("docx")
            .need_notification(False)
            .request_body(member)
            .build()
        )
        response = require_service(self.client.drive).v1.permission_member.create(request)
        required_data(response, "grant document permission")

    def _convert_markdown(self, markdown: str) -> tuple[list[str], list[Block]] | None:
        body = (
            ConvertDocumentRequestBody.builder()
            .content_type("markdown")
            .content(markdown)
            .build()
        )
        request = ConvertDocumentRequest.builder().request_body(body).build()
        response = require_service(self.client.docx).v1.document.convert(request)
        data = required_data(response, "convert document Markdown")
        first_level = [
            str(block_id)
            for block_id in (data.first_level_block_ids or [])
            if block_id
        ]
        blocks = [item if isinstance(item, Block) else Block(item) for item in data.blocks or []]
        if not first_level or not blocks:
            return None
        return first_level, blocks

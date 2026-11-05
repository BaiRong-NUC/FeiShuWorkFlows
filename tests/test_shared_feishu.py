from __future__ import annotations

import ast
import json
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import lark_oapi as lark
from lark_oapi.api.bitable.v1 import (
    CreateAppTableRecordResponse, GetAppTableRecordResponse,
    ListAppTableFieldResponse, ListAppTableRecordResponse, UpdateAppTableRecordResponse,
)
from lark_oapi.api.docx.v1 import (
    ConvertDocumentResponse, CreateDocumentBlockDescendantResponse, CreateDocumentResponse,
)
from lark_oapi.api.drive.v1 import CreatePermissionMemberResponse
from lark_oapi.api.im.v1 import CreateMessageResponse, ReplyMessageResponse

from agents.speaker_agent.adapters.feishu.gateway import FeishuGateway
from shared.feishu.client import FeishuClient
from shared.feishu.config import BitableConfig, FeishuConfig
from shared.feishu.connection import create_socket_client, new_event_dispatcher
from shared.feishu.contracts import BitableRecord
from shared.feishu.errors import FeishuApiError
from test_agent import settings


class FakeFeishuServices:
    """A plain Python implementation of shared contracts, without an SDK."""

    def __init__(self) -> None:
        self.messages = self
        self.documents = self
        self.tables: list[BitableConfig] = []
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.filter = ""
        self.record = BitableRecord("rec-custom", {
            "事项": [{"text": "自定义标题"}], "负责人": [{"open_id": "ou_owner"}],
            "流程": "已审核", "原始内容": "第一行\n第二行",
        }, "https://example.feishu.cn/base/custom")

    def table(self, config: BitableConfig) -> FakeFeishuServices:
        self.tables.append(config)
        return self

    def get_record(self, record_id: str) -> BitableRecord:
        return self.record

    def list_records(self, *, filter_expression: str = "") -> list[BitableRecord]:
        self.filter = filter_expression
        return [self.record]

    def create_record(self, fields: dict[str, Any], *, client_token: str = "") -> str:
        return self.record.record_id

    def update_record(self, record_id: str, fields: dict[str, Any]) -> None:
        self.updates.append((record_id, fields))

    def send_text(self, receive_id_type: str, receive_id: str, text: str, *, uuid: str = "") -> None:
        pass

    def reply_text(self, message_id: str, text: str, *, uuid: str = "") -> None:
        pass

    def create_markdown_document(self, title: str, markdown: str, *, folder_token: str = "") -> tuple[str, str]:
        return "doc", "https://example.feishu.cn/docx/doc"

    def grant_document_view(self, document_token: str, open_id: str) -> None:
        pass


class SharedFeishuTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sdk = Mock()
        self.config = FeishuConfig("cli_test", "test-only-secret")
        self.feishu = FeishuClient(self.config, client=self.sdk)
        self.table = self.feishu.table(BitableConfig("base-a", "table-a"))

    def test_record_pagination_and_table_scoped_field_cache(self):
        records = self.sdk.bitable.v1.app_table_record
        fields = self.sdk.bitable.v1.app_table_field
        fields.list.side_effect = [
            ListAppTableFieldResponse({"code": 0, "data": {
                "items": [{"field_id": "fldText", "field_name": "正文"}], "has_more": True, "page_token": "fields-2",
            }}),
            ListAppTableFieldResponse({"code": 0, "data": {
                "items": [{"field_id": "fldType", "field_name": "类别"}], "has_more": False,
            }}),
            ListAppTableFieldResponse({"code": 0, "data": {
                "items": [{"field_id": "fldText", "field_name": "另一张表的列"}], "has_more": False,
            }}),
        ]
        records.list.side_effect = [
            ListAppTableRecordResponse({"code": 0, "data": {
                "items": [{"record_id": "rec1", "fields": {"fldText": "第一行\n第二行"}}],
                "has_more": True, "page_token": "records-2",
            }}),
            ListAppTableRecordResponse({"code": 0, "data": {
                "items": [{"record_id": "rec2", "fields": {"fldType": "分类"}}], "has_more": False,
            }}),
        ]
        result = self.table.list_records(filter_expression='CurrentValue.[类别]="分类"')
        self.assertEqual([r.record_id for r in result], ["rec1", "rec2"])
        self.assertEqual(result[0].fields["正文"], "第一行\n第二行")
        self.assertEqual(records.list.call_args.args[0].page_token, "records-2")
        self.assertEqual(fields.list.call_args.args[0].page_token, "fields-2")
        self.assertEqual(fields.list.call_count, 2)
        records.get.return_value = GetAppTableRecordResponse({"code": 0, "data": {
            "record": {"record_id": "rec3", "fields": {"fldText": "另一张表的内容"}},
        }})
        other = self.feishu.table(BitableConfig("base-b", "table-b"))
        self.assertIn("另一张表的列", other.get_record("rec3").fields)
        self.assertEqual(fields.list.call_args.args[0].table_id, "table-b")
        self.assertEqual(records.get.call_args.args[0].app_token, "base-b")
        self.assertIn("table=table-a", result[0].record_url)

    def test_record_creation_preserves_token_and_arbitrary_fields(self):
        records = self.sdk.bitable.v1.app_table_record
        records.create.return_value = CreateAppTableRecordResponse({"code": 0, "data": {"record": {"record_id": "rec-new"}}})
        records.update.return_value = UpdateAppTableRecordResponse({"code": 0, "data": {}})
        content = {"通用文本": "保留\n换行", "其他业务字段": 42}
        self.assertEqual(self.table.create_record(content, client_token="stable-token"), "rec-new")
        request = records.create.call_args.args[0]
        self.assertEqual(request.client_token, "stable-token")
        self.assertEqual(request.request_body.fields, content)
        self.assertEqual(request.user_id_type, "open_id")
        self.table.update_record("rec-new", content)
        self.assertEqual(records.update.call_args.args[0].record_id, "rec-new")
        self.assertEqual(records.update.call_args.args[0].request_body.fields, content)

    def test_sdk_errors_missing_data_and_nullable_services_are_checked(self):
        for response in (
            GetAppTableRecordResponse({"code": 999, "msg": "rejected"}),
            GetAppTableRecordResponse({"code": 0}),
            GetAppTableRecordResponse({"code": 0, "data": {}}),
        ):
            self.sdk.bitable.v1.app_table_record.get.return_value = response
            with self.subTest(response=response), self.assertRaises(FeishuApiError):
                self.table.get_record("rec1")
        # Construct the SDK's real uninitialized client; do not send a request.
        empty = FeishuClient(self.config, client=lark.Client())
        with self.assertRaisesRegex(FeishuApiError, "not initialized"):
            empty.table(BitableConfig("base", "table")).get_record("rec1")

    def test_message_routing_uuid_and_readable_chinese_survive_extraction(self):
        messages = self.sdk.im.v1.message
        messages.create.return_value = CreateMessageResponse({"code": 0, "data": {}})
        messages.reply.return_value = ReplyMessageResponse({"code": 0, "data": {}})
        text = "第一行\n第二行"
        self.feishu.messages.send_text("open_id", "ou_person", text, uuid="send-uuid")
        request = messages.create.call_args.args[0]
        self.assertEqual(request.receive_id_type, "open_id")
        self.assertEqual(request.request_body.receive_id, "ou_person")
        self.assertEqual(request.request_body.uuid, "send-uuid")
        self.assertIn("第一行", request.request_body.content)
        self.assertEqual(json.loads(request.request_body.content), {"text": text})
        self.feishu.messages.reply_text("om_origin", text, uuid="reply-uuid")
        reply = messages.reply.call_args.args[0]
        self.assertEqual(reply.message_id, "om_origin")
        self.assertEqual(reply.request_body.uuid, "reply-uuid")

    def test_document_conversion_write_and_permission(self):
        docx = self.sdk.docx.v1
        docx.document.create.return_value = CreateDocumentResponse({"code": 0, "data": {"document": {"document_id": "doc-new"}}})
        docx.document.convert.return_value = ConvertDocumentResponse({"code": 0, "data": {
            "first_level_block_ids": ["block1"], "blocks": [{"block_id": "block1", "block_type": 2}],
        }})
        docx.document_block_descendant.create.return_value = CreateDocumentBlockDescendantResponse({"code": 0, "data": {}})
        permission = self.sdk.drive.v1.permission_member.create
        permission.return_value = CreatePermissionMemberResponse({"code": 0, "data": {}})
        doc_id, url = self.feishu.documents.create_markdown_document("通用文档", "# 正文", folder_token="folder")
        self.assertEqual(doc_id, "doc-new")
        self.assertTrue(url.endswith("/docx/doc-new"))
        self.assertEqual(docx.document.create.call_args.args[0].request_body.folder_token, "folder")
        self.assertEqual(docx.document.convert.call_args.args[0].request_body.content, "# 正文")
        request = docx.document_block_descendant.create.call_args.args[0]
        self.assertEqual(request.document_id, "doc-new")
        self.assertEqual(request.request_body.children_id, ["block1"])
        self.feishu.documents.grant_document_view(doc_id, "ou_reader")
        request = permission.call_args.args[0]
        self.assertEqual(request.request_body.member_id, "ou_reader")
        self.assertEqual(request.request_body.perm, "view")
        self.assertFalse(request.need_notification)

    def test_empty_document_conversion_does_not_claim_success(self):
        docx = self.sdk.docx.v1
        docx.document.create.return_value = CreateDocumentResponse({"code": 0, "data": {"document": {"document_id": "doc-new"}}})
        docx.document.convert.return_value = ConvertDocumentResponse({"code": 0, "data": {}})
        with self.assertRaises(FeishuApiError):
            self.feishu.documents.create_markdown_document("通用文档", "# 正文")
        docx.document_block_descendant.create.assert_not_called()

    def test_connection_construction_does_not_start_socket(self):
        handler = new_event_dispatcher().build()
        with patch("shared.feishu.connection.lark.ws.Client") as factory:
            result = create_socket_client(self.config, handler)
            self.assertIs(result, factory.return_value)
            factory.assert_called_once_with("cli_test", "test-only-secret", event_handler=handler, log_level=lark.LogLevel.INFO)
            factory.return_value.start.assert_not_called()
        self.assertNotIn("test-only-secret", repr(self.config))

    def test_speaker_owns_business_fields_and_accepts_shared_protocol_fake(self):
        fake = FakeFeishuServices()
        config = replace(settings(Path("unused")), title_field="事项", requester_field="负责人", status_field="流程",
                         result_field="总结", error_field="备注", completed_status="已审核")
        gateway = FeishuGateway(config, feishu=fake)
        record = gateway.get_record("rec-custom")
        self.assertEqual(record.title, "自定义标题")
        self.assertEqual(record.requester_id, "ou_owner")
        self.assertEqual(record.status, "已审核")
        self.assertEqual(record.fields["原始内容"], "第一行\n第二行")
        gateway.list_records_by_status("待处理")
        self.assertEqual(fake.filter, 'CurrentValue.[流程]="待处理"')
        gateway.write_result("rec-custom", "# 下层结果")
        self.assertEqual(fake.updates, [("rec-custom", {"流程": "已审核", "总结": "# 下层结果", "备注": ""})])
        self.assertEqual(fake.tables, [config.bitable])

    def test_shared_packages_do_not_import_agents(self):
        root = Path(__file__).resolve().parents[1] / "shared"
        for path in root.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = ([node.module or ""] if isinstance(node, ast.ImportFrom)
                         else [item.name for item in node.names] if isinstance(node, ast.Import) else [])
                for name in names:
                    self.assertFalse(name == "agents" or name.startswith("agents."), f"{path}: {name}")


if __name__ == "__main__":
    unittest.main()

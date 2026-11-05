from __future__ import annotations

import copy
import json
import os
import re
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import Mock, patch
from urllib.parse import unquote

from lark_oapi.api.bitable.v1 import ListAppTableResponse
from lark_oapi.api.sheets.v3 import QuerySpreadsheetSheetResponse
from lark_oapi.api.wiki.v2 import GetNodeSpaceResponse
from lark_oapi.core.model import BaseResponse, RawResponse

from agents.speaker_agent.config import ConfigError, Settings
from agents.speaker_agent.adapters.feishu.sheets_gateway import SheetGateway
from agents.speaker_agent.domain.contracts import AnalysisResult, DownstreamResult, ReviewResult
from agents.speaker_agent.orchestration.workspace import SpeakerWorkspace
from shared.feishu.catalog import CatalogClient, TableReference, parse_file_url
from shared.feishu.errors import FeishuApiError
from shared.feishu.sheets import SheetsClient
from test_agent import settings
from test_shared_feishu import FakeFeishuServices
from helpers import required


class MemoryWorkspace(FakeFeishuServices):
    def __init__(self) -> None:
        super().__init__()
        self.catalog = self
        self.sheets = self
        self.refs = [TableReference("sheet", "book", name, title, "https://example.feishu.cn/wiki/node", column_count=26)
                     for name, title in (("one", "产品甲"), ("two", "产品乙"))]
        self.rows: dict[str, list[list[Any]]] = {
            ref.table_id: [["ID", "版本", "状态", "责任人", "描述", "备注", "处理备注"],
                           [1, "1.0", "待生成方案", "张三", "增加导出\n保留原格式", "原始限制", "人工备注"],
                           [2, "1.0", "已完成", "李四", "历史需求", "", "历史结果"]]
            for ref in self.refs}
        self.writes: list[tuple[str, str, list[list[Any]]]] = []
        self.replies: list[tuple[str, str]] = []
        self.lose_append_response = False
        self.dropdowns: dict[str, list[str]] = {}

    def read_dropdowns(self, token: str, range_name: str) -> list[dict[str, Any]]:
        column = re.sub(r"\d+.*", "", range_name.split("!", 1)[1])
        values = self.dropdowns.get(column)
        return [{"conditionValues": values, "dataValidationType": "list", "ranges": [range_name]}] if values is not None else []

    def discover(self, urls: tuple[str, ...]) -> list[TableReference]:
        return self.refs[:]

    def read_range(self, token: str, range_name: str) -> list[list[Any]]:
        return copy.deepcopy(self.rows[range_name])

    def write_ranges(self, token: str, ranges: list[tuple[str, list[list[Any]]]]) -> None:
        for name, values in ranges:
            self.writes.append((token, name, values))
            match = required(re.fullmatch(r"([^!]+)!([A-Z]+)(\d+):[A-Z]+\d+", name))
            table, column, row = match.groups()
            index = 0
            for char in column:
                index = index * 26 + ord(char) - 64
            target = self.rows[table][int(row) - 1]
            needed = index - 1 + len(values[0]) - len(target)
            target.extend([""] * max(0, needed))
            target[index - 1:index - 1 + len(values[0])] = values[0]

    def append_rows(self, token: str, range_name: str, rows: list[list[Any]]) -> None:
        self.rows[range_name.split("!", 1)[0]].extend(copy.deepcopy(rows))
        if self.lose_append_response:
            self.lose_append_response = False
            raise FeishuApiError("响应丢失")

    def reply_text(self, message_id: str, text: str, *, uuid: str = "") -> None:
        self.replies.append((message_id, text))


class SourceTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = settings(self.root)
        self.api = MemoryWorkspace()
        self.gateway = SheetGateway(self.config, self.api.refs[0], feishu=self.api)

    def test_new_url_configuration_requires_no_manual_ids_and_old_keys_do_not_work(self):
        env = {"FEISHU_APP_ID": "cli_test", "FEISHU_APP_SECRET": "test",
               "SPEAKER_INTAKE_MODE": "rules",
               "FEISHU_LEDGER_URLS": "https://example.feishu.cn/wiki/node?sheet=one,https://example.feishu.cn/base/base"}
        with patch.dict(os.environ, env, clear=True):
            config = Settings.from_env()
            config.validate_for_startup()
            self.assertEqual(len(config.ledger_urls), 2)
            self.assertEqual(config.source_table_id, "")
        env.pop("FEISHU_LEDGER_URLS")
        env.update(FEISHU_BITABLE_APP_TOKEN="base", FEISHU_BITABLE_TABLE_ID="table",
                   FEISHU_BITABLE_URL="https://example.feishu.cn/wiki/node")
        with patch.dict(os.environ, env, clear=True), self.assertRaisesRegex(ConfigError, "FEISHU_LEDGER_URLS"):
            Settings.from_env()

    def test_sheet_reads_pending_only_and_never_treats_name_as_open_id(self):
        pending = self.gateway.list_records_by_status("待生成方案")
        self.assertEqual([r.record_id for r in pending], ["1"])
        self.assertEqual(pending[0].requester_id, "")
        self.assertIn("增加导出\n保留原格式", pending[0].fields[self.config.raw_field])
        self.assertEqual(self.api.writes, [])

    def test_reordered_rows_are_found_by_id_and_human_notes_are_preserved(self):
        self.gateway.get_record("1")
        self.api.rows["one"][1], self.api.rows["one"][2] = self.api.rows["one"][2], self.api.rows["one"][1]
        self.gateway.write_result("1", "下层返回的结论")
        self.assertEqual(self.api.rows["one"][1][2], "已完成")
        self.assertEqual(self.api.rows["one"][1][6], "历史结果")
        self.assertEqual(self.api.rows["one"][2][2], "待生成方案")
        self.assertEqual(self.api.rows["one"][2][6], "人工备注\n\n下层返回的结论")
        self.assertEqual(len(self.api.rows["one"][0]), 7)
        self.assertEqual(self.api.rows["two"][1][2], "待生成方案")

    def test_duplicate_or_missing_record_ids_never_write(self):
        self.api.rows["one"][2][0] = 1
        with self.assertRaisesRegex(FeishuApiError, "不唯一"):
            self.gateway.update_status("1", "生成中")
        self.assertEqual(self.api.writes, [])
        self.api.rows["one"][2][0] = 2
        with self.assertRaisesRegex(FeishuApiError, "找不到"):
            self.gateway.write_result("missing", "不应写入")
        self.assertEqual(self.api.writes, [])

    def test_create_is_idempotent_after_response_loss_and_preserves_newlines(self):
        self.api.lose_append_response = True
        fields = {self.config.raw_field: "第一行\n第二行", self.config.title_field: "标题",
                  self.config.requester_field: [{"id": "ou_requester"}], self.config.status_field: "生成中"}
        with self.assertRaisesRegex(FeishuApiError, "响应丢失"):
            self.gateway.create_record(fields, client_token="stable")
        record_id = self.gateway.create_record(fields, client_token="stable")
        self.assertEqual(len(self.api.rows["one"]), 4)
        record = self.gateway.get_record(record_id)
        self.assertEqual(record.fields[self.config.raw_field], "第一行\n第二行")
        self.assertEqual(record.requester_id, "")
        self.assertEqual(len(self.api.rows["one"][0]), 7)
        self.assertEqual(len(self.api.rows["one"][-1]), 6)
        self.assertEqual(self.api.rows["one"][-1][3], "")

    def test_historical_duplicate_ids_do_not_block_unique_records_or_chat_creation(self):
        original = copy.deepcopy(self.api.rows["one"][2])
        self.api.rows["one"].extend([copy.deepcopy(original), [2]])
        with self.assertLogs("agents.speaker_agent.adapters.feishu.sheets_gateway", level="WARNING") as log:
            pending = self.gateway.list_records_by_status("待生成方案")
        self.assertEqual([record.record_id for record in pending], ["1"])
        self.assertIn("ID '2'：第 3、4、5 行", "\n".join(log.output))
        self.assertEqual(self.api.writes, [])
        self.gateway.write_result("1", "唯一记录的结果")
        self.assertEqual(self.api.rows["one"][2:5], [original, original, [2]])
        new_id = self.gateway.create_record({self.config.raw_field: "新的聊天需求"}, client_token="new-chat")
        self.assertEqual(self.gateway.get_record(new_id).fields[self.config.raw_field], "新的聊天需求")
        self.gateway.update_status(new_id, "生成中")
        self.assertEqual(self.api.rows["one"][2:5], [original, original, [2]])

    def test_duplicate_id_is_skipped_even_when_only_one_matching_row_is_pending(self):
        self.api.rows["one"][2][0] = 1
        with self.assertLogs(level="WARNING"):
            self.assertEqual(self.gateway.list_records_by_status("待生成方案"), [])
        with self.assertRaisesRegex(FeishuApiError, "第 2、3 行"):
            self.gateway.get_record("1")
        with self.assertRaisesRegex(FeishuApiError, "不唯一"):
            self.gateway.write_result("1", "不能回填任意一行")
        self.assertEqual(self.api.writes, [])

    def test_duplicate_created_id_cannot_be_reused_as_successful_creation(self):
        self.api.rows["one"][1][0] = "spk_existing"
        self.api.rows["one"][2][0] = "spk_existing"
        before = copy.deepcopy(self.api.rows)
        with self.assertRaisesRegex(FeishuApiError, "不唯一"):
            self.gateway.create_record({self.config.raw_field: "新内容"}, client_token="existing")
        self.assertEqual(self.api.rows, before)
        self.assertEqual(self.api.writes, [])

    def test_duplicate_introduced_during_update_is_rejected_before_data_write(self):
        original_read = self.api.read_range
        reads = 0

        def read(token: str, range_name: str) -> list[list[Any]]:
            nonlocal reads
            reads += 1
            if reads == 2:
                self.api.rows["one"][2][0] = 1
            return original_read(token, range_name)

        with patch.object(self.api, "read_range", side_effect=read):
            with self.assertRaisesRegex(FeishuApiError, "不唯一"):
                self.gateway.write_result("1", "结论")
        self.assertEqual(self.api.writes, [])

    def test_output_columns_cannot_overwrite_source_columns(self):
        gateway = SheetGateway(replace(self.config, result_field="描述"), self.api.refs[0], feishu=self.api)
        original = self.api.rows["one"][1][4]
        gateway.write_result("1", "结果")
        self.assertEqual(self.api.rows["one"][1][4], original)
        self.assertIn("结果", self.api.rows["one"][1][6])

    def test_intake_writes_only_six_columns_and_waiting_never_writes_processing_notes(self):
        self.api.dropdowns = {"B": ["1.5.1"], "C": ["待修改", "进行中", "已完成"], "D": ["龚柏荣"]}
        headers = self.api.rows["one"][0][:]
        record_id = self.gateway.create_record({self.config.raw_field: "缓存冷却改为15s", "版本": "1.5.1",
                                               "责任人": "龚柏荣", "备注": "仅缓存", self.config.brief_field: "本地需求单"},
                                              client_token="six-columns")
        self.gateway.update_status(record_id, "生成中", error="等待下层结果")
        self.assertEqual(self.api.rows["one"][0], headers)
        self.assertEqual(self.api.rows["one"][-1], [record_id, "1.5.1", "待修改", "龚柏荣", "缓存冷却改为15s", "仅缓存"])
        self.assertEqual(self.api.writes, [])
        self.gateway.write_result(record_id, "下层结果")
        self.gateway.write_result(record_id, "下层结果")
        self.assertEqual(self.api.rows["one"][-1][6], "下层结果")
        self.assertEqual(self.api.rows["one"][-1][2], "待修改")
        self.assertEqual(self.api.rows["one"][0], headers)
        self.assertTrue(all(re.fullmatch(r"one!G\d+:G\d+", name) for _, name, _ in self.api.writes))

    def test_processing_note_header_is_added_only_when_lower_result_arrives(self):
        self.api.rows["one"] = [row[:6] for row in self.api.rows["one"]]
        self.gateway.create_record({self.config.raw_field: "新需求"}, client_token="six")
        self.gateway.update_status("1", "生成中", error="等待")
        self.assertEqual(len(self.api.rows["one"][0]), 6)
        self.assertEqual(self.api.writes, [])
        self.gateway.write_result("1", "下层结论")
        self.assertEqual(self.api.rows["one"][0], ["ID", "版本", "状态", "责任人", "描述", "备注", "处理备注"])
        self.assertEqual(self.api.rows["one"][1][6], "下层结论")

    def test_dropdowns_are_checked_and_leaf_pending_option_is_used(self):
        self.api.dropdowns = {"B": ["1.1.0"], "C": ["待处理", "已完成", "暂搁置"], "D": ["龚柏荣"]}
        for invalid in ({"版本": "9.9.9"}, {"责任人": "ou_requester"}):
            with self.assertRaisesRegex(FeishuApiError, "不在已有下拉选项"):
                self.gateway.create_record({self.config.raw_field: "需求", **invalid})
        self.assertEqual(len(self.api.rows["one"]), 3)
        self.gateway.create_record({self.config.raw_field: "新需求"}, client_token="leaf")
        self.assertEqual(self.api.rows["one"][-1][1:4], ["", "待处理", ""])
        self.assertEqual(self.api.writes, [])

    def test_completed_sheet_analysis_is_not_recreated_while_development_is_pending(self):
        self.api.rows["one"][1][2] = "待修改"
        workspace = SpeakerWorkspace(self.config, feishu=self.api)
        agent = workspace.agents[self.api.refs[0].key]
        agent.scan_pending()
        self.assertTrue(required(agent.process_item(required(agent._next_item()))).ok)
        job = required(agent.workflows.latest_job("1"))
        agent.submit_result(job["id"], DownstreamResult(AnalysisResult("分析"), ReviewResult("评审")))
        writes = len(self.api.writes)
        agent.scan_pending()
        self.assertIsNone(agent.process_item(required(agent._next_item())))
        self.assertEqual(required(agent.workflows.latest_job("1"))["id"], job["id"])
        self.assertEqual(len(self.api.writes), writes)
        self.assertEqual(self.api.rows["one"][1][2], "待修改")

    def test_unnamed_column_data_and_padded_empty_cells_are_preserved(self):
        self.api.rows["one"][1].append("没有标题的人工内容")
        self.api.rows["one"][0].extend([""] * 19)
        self.gateway.write_result("1", "结论")
        self.assertEqual(self.api.rows["one"][1][7], "没有标题的人工内容")
        self.assertEqual(self.api.rows["one"][0][8], "")

    def test_identical_row_ids_across_tables_have_separate_jobs_and_correct_writeback(self):
        workspace = SpeakerWorkspace(self.config, feishu=self.api)
        self.assertEqual(workspace.scan_pending(), 2)
        agents = list(workspace.agents.values())
        self.assertNotEqual(agents[0].settings.runtime_dir, agents[1].settings.runtime_dir)
        for agent in agents:
            agent.process_item(required(agent._next_item()))
        jobs = [required(agent.workflows.latest_job("1")) for agent in agents]
        self.assertNotEqual(jobs[0]["id"], jobs[1]["id"])
        self.assertEqual(len(list((self.root / "handoff/outbox").glob("*.json"))), 2)
        agents[0].submit_result(jobs[0]["id"], DownstreamResult(AnalysisResult("分析"), ReviewResult("评审")))
        self.assertEqual(self.api.rows["one"][1][2], "待生成方案")
        self.assertEqual(self.api.rows["two"][1][2], "待生成方案")
        restarted = SpeakerWorkspace(self.config, feishu=self.api)
        self.assertEqual(required(list(restarted.agents.values())[1].workflows.latest_job("1"))["id"], jobs[1]["id"])

    def test_routing_asks_for_ambiguous_target_and_selects_named_table(self):
        workspace = SpeakerWorkspace(self.config, feishu=self.api)
        workspace.handle_command("需求 增加导出功能", "om_ambiguous", "ou_sender")
        self.assertEqual(len(self.api.replies), 1)
        self.assertTrue(all(agent._next_item() is None for agent in workspace.agents.values()))
        workspace.handle_command("台账：产品乙\n需求 增加导出功能", "om_selected", "ou_sender")
        first, second = list(workspace.agents.values())
        self.assertIsNone(first._next_item())
        self.assertIsNotNone(second._next_item())
        self.assertEqual(self.api.writes, [])

    def test_one_invalid_sheet_does_not_stop_other_tables(self):
        self.api.rows["one"] = [["随便一个说明表头"]]
        workspace = SpeakerWorkspace(self.config, feishu=self.api)
        with self.assertLogs("agents.speaker_agent.orchestration.workspace", level="ERROR"):
            self.assertEqual(workspace.scan_pending(), 1)

    def test_refresh_adds_new_tables_without_losing_existing_state(self):
        workspace = SpeakerWorkspace(self.config, feishu=self.api)
        self.api.refs.append(replace(self.api.refs[0], table_id="three", title="新产品"))
        self.api.rows["three"] = copy.deepcopy(self.api.rows["one"])
        workspace.scan_pending()
        self.assertEqual(len(workspace.agents), 3)


class CatalogTests(unittest.TestCase):
    def test_wiki_resolves_spreadsheet_and_discovers_every_sheet_ignoring_url_selection(self):
        sdk = Mock()
        sdk.wiki.v2.space.get_node.return_value = GetNodeSpaceResponse({"code": 0, "data": {
            "node": {"obj_type": "sheet", "obj_token": "book", "title": "任务台账"}}})
        sdk.sheets.v3.spreadsheet_sheet.query.return_value = QuerySpreadsheetSheetResponse({"code": 0, "data": {
            "sheets": [{"sheet_id": "one", "title": "产品甲", "resource_type": "sheet"},
                       {"sheet_id": "two", "title": "产品乙", "resource_type": "sheet"}]}})
        refs = CatalogClient(sdk).discover(("https://example.feishu.cn/wiki/node?sheet=one",
                                           "https://example.feishu.cn/sheets/book?sheet=two"))
        self.assertEqual([ref.table_id for ref in refs], ["one", "two"])
        self.assertEqual(refs[1].url, "https://example.feishu.cn/wiki/node?sheet=two")
        sdk.sheets.v3.spreadsheet_sheet.query.assert_called_once()

    def test_bitable_discovery_follows_all_pages(self):
        sdk = Mock()
        sdk.bitable.v1.app_table.list.side_effect = [
            ListAppTableResponse({"code": 0, "data": {"has_more": True, "page_token": "next", "items": [{"table_id": "one", "name": "甲"}]}}),
            ListAppTableResponse({"code": 0, "data": {"has_more": False, "items": [{"table_id": "two", "name": "乙"}]}})]
        refs = CatalogClient(sdk).discover(("https://example.feishu.cn/base/base?table=one",))
        self.assertEqual(len(refs), 2)
        self.assertEqual(sdk.bitable.v1.app_table.list.call_args.args[0].page_token, "next")

    def test_invalid_links_and_unsupported_wiki_objects_fail_explicitly(self):
        for url in ("https://evil.example/wiki/node", "https://example.feishu.cn.evil.test/wiki/node", "http://example.feishu.cn/wiki/node"):
            with self.assertRaises(ValueError):
                parse_file_url(url)
        sdk = Mock()
        sdk.wiki.v2.space.get_node.return_value = GetNodeSpaceResponse({"code": 0, "data": {
            "node": {"obj_type": "docx", "obj_token": "document"}}})
        with self.assertRaisesRegex(FeishuApiError, "docx"):
            CatalogClient(sdk).discover(("https://example.feishu.cn/wiki/node",))

    def test_sheets_read_write_and_append_use_official_api_payloads(self):
        sdk = Mock()
        response = BaseResponse({"code": 0})
        response.raw = RawResponse()
        response.raw.content = json.dumps({"data": {"valueRange": {"values": [["第一行\n第二行"]]}}}).encode()
        sdk.request.return_value = response
        api = SheetsClient(sdk)
        self.assertEqual(api.read_range("book", "one!A1:G20"), [["第一行\n第二行"]])
        request = sdk.request.call_args.args[0]
        self.assertTrue(unquote(request.uri).endswith("/values/one!A1:G20"))
        api.write_ranges("book", [("one!C2", [["生成中"]])])
        self.assertEqual(sdk.request.call_args.args[0].body["valueRanges"][0]["values"], [["生成中"]])
        api.append_rows("book", "one!A1:G", [["new"]])
        self.assertEqual(sdk.request.call_args.args[0].body["insertDataOption"], "INSERT_ROWS")


if __name__ == "__main__":
    unittest.main()

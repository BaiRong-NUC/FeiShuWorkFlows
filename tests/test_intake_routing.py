from __future__ import annotations

import json
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from lark_oapi.core.model import BaseResponse, RawResponse

from agents.speaker_agent.domain.intake import IntakeDecision, IntakeError, parse_intake_decision
from agents.speaker_agent.orchestration.workspace import SpeakerWorkspace
from shared.feishu.errors import FeishuApiError
from shared.feishu.sheets import SheetsClient
from test_agent import settings
from test_ledger_sources import MemoryWorkspace


class ProjectRoutingTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.api = MemoryWorkspace()
        self.api.refs = [replace(ref, title=name) for ref, name in zip(self.api.refs, ("PDF Viewer", "Leaf PDF"))]
        self.workspace = SpeakerWorkspace(replace(settings(Path(temp.name)), chat_target="Leaf PDF"), feishu=self.api)

    def test_project_names_allow_spacing_case_separators_and_unique_small_typo(self):
        for name in ("PDFViewer", "pdfviewer", "PDF-Viewer", "PDF_Viewer", "PDF Viewer", "PDFViewwe", "ＰＤＦＶｉｅｗｅｒ"):
            with self.subTest(name=name):
                self.assertEqual(self.workspace.select_target(f"{name}哪里广告策略是什么？"), self.api.refs[0].key)
        self.assertEqual(self.workspace.select_target("台账：pdfviewer\n需求 修改冷却"), self.api.refs[0].key)

    def test_multiple_projects_duplicate_names_and_close_typos_require_clarification(self):
        self.assertEqual(self.workspace.select_target("PDFViewer 和 LeafPDF 都修改冷却"), "")
        self.api.refs.append(replace(self.api.refs[0], table_id="duplicate", title="PDFViewer"))
        self.workspace.refresh()
        self.assertEqual(self.workspace.select_target("PDFViewer 哪里广告策略是什么？"), "")
        self.assertEqual(self.workspace.select_target("PDFViewwe 哪里广告策略是什么？"), "")
        self.assertEqual(self.workspace.select_target("台账：不存在的项目\n需求 修改冷却"), "")

    def test_question_schema_rejects_conflicting_intents_and_empty_title(self):
        question = IntakeDecision(False, title="广告策略", requirements="当前策略是什么？", is_inquiry=True)
        self.assertTrue(parse_intake_decision(asdict(question)).needs_analysis)
        for changed in ({"is_requirement": True}, {"is_inquiry": "true"}, {"title": ""}):
            with self.subTest(changed=changed), self.assertRaises(IntakeError):
                parse_intake_decision({**asdict(question), **changed})
        self.assertFalse(parse_intake_decision(asdict(IntakeDecision(False))).needs_analysis)


class SheetsRecoveryTests(unittest.TestCase):
    @staticmethod
    def response(code: int) -> BaseResponse:
        response = BaseResponse({"code": code})
        response.raw = RawResponse()
        response.raw.content = json.dumps({"data": {"valueRange": {"values": [["原有需求"]]}}}).encode()
        return response

    def test_transient_read_failure_retries_and_returns_original_data(self):
        sdk = Mock()
        sdk.request.side_effect = [self.response(95203), self.response(95203), self.response(0)]
        with patch("shared.feishu.sheets.time.sleep") as sleep, self.assertLogs("shared.feishu.sheets", level="WARNING"):
            self.assertEqual(SheetsClient(sdk).read_range("book", "sheet"), [["原有需求"]])
        self.assertEqual(sdk.request.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_read_retry_is_bounded_and_permission_errors_are_not_retried(self):
        for code, calls in ((95203, 3), (99991672, 1)):
            sdk = Mock()
            sdk.request.return_value = self.response(code)
            with patch("shared.feishu.sheets.time.sleep"), self.assertRaises(FeishuApiError):
                SheetsClient(sdk).read_range("book", "sheet")
            self.assertEqual(sdk.request.call_count, calls)

    def test_append_and_update_are_never_blindly_retried(self):
        for append in (True, False):
            sdk = Mock()
            sdk.request.return_value = self.response(95203)
            with patch("shared.feishu.sheets.time.sleep") as sleep, self.assertRaises(FeishuApiError):
                if append:
                    SheetsClient(sdk).append_rows("book", "sheet!A1:C", [["新需求"]])
                else:
                    SheetsClient(sdk).write_ranges("book", [("sheet!A1", [["新状态"]])])
            sdk.request.assert_called_once()
            sleep.assert_not_called()

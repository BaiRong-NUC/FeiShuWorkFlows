from __future__ import annotations

from helpers import required, as_message_event, as_record_event

import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from agents.speaker_agent.config import Settings
from agents.speaker_agent.domain.contracts import AnalysisResult, DownstreamResult, ReviewResult
from agents.speaker_agent.adapters.feishu.events import on_message, on_record_changed
from agents.speaker_agent.domain.models import DemandRecord, IncomingDemand
from agents.speaker_agent.orchestration.service import DemandAgent
from agents.speaker_agent.persistence.state import ProcessedEventStore


class FakeGateway:
    def __init__(self, record: DemandRecord) -> None:
        self.record = record
        self.statuses: list[tuple[str, str, str]] = []
        self.documents: list[tuple[str, str]] = []
        self.permissions: list[tuple[str, str]] = []
        self.messages: list[tuple[str, str, str]] = []
        self.replies: list[tuple[str, str]] = []
        self.created: list[dict] = []
        self.results: list[tuple[str, str]] = []
        self.client_tokens: list[str] = []

    def get_record(self, record_id: str) -> DemandRecord:
        if record_id not in {self.record.record_id, "rec1", "rec-created"}:
            raise AssertionError(record_id)
        return self.record

    def update_status(self, record_id: str, status: str, *, proposal_url: str = "", error: str = "") -> None:
        self.statuses.append((record_id, status, proposal_url or error))
        self.record = replace(self.record, status=status)

    def list_records_by_status(self, status: str) -> list[DemandRecord]:
        return [self.record] if self.record.status == status else []

    def write_result(self, record_id: str, markdown: str, *, document_url: str = "") -> None:
        self.results.append((record_id, markdown))
        self.update_status(record_id, "已生成", proposal_url=document_url)

    def create_proposal_document(self, title: str, markdown: str) -> tuple[str, str]:
        self.documents.append((title, markdown))
        return "docx-token", "https://example.feishu.cn/docx/docx-token"

    def create_record(self, fields: dict, *, client_token: str = "") -> str:
        self.created.append(fields)
        self.client_tokens.append(client_token)
        self.record = DemandRecord(
            record_id="rec-created",
            fields=fields,
            title=str(fields.get("需求标题") or "未命名需求"),
            requester_id="ou_sender",
            status=str(fields.get("状态") or ""),
            record_url="https://example.feishu.cn/record/rec-created",
        )
        return self.record.record_id

    def grant_document_view(self, document_token: str, open_id: str) -> None:
        self.permissions.append((document_token, open_id))

    def send_text(self, receive_id_type: str, receive_id: str, text: str, *, uuid: str = "") -> None:
        self.messages.append((receive_id_type, receive_id, text))

    def reply_text(self, message_id: str, text: str, *, uuid: str = "") -> None:
        self.replies.append((message_id, text))


def settings(runtime_dir: Path) -> Settings:
    return Settings(
        app_id="cli_test",
        app_secret="secret",
        bitable_app_token="base-token",
        bitable_table_id="tbl-token",
        bitable_url="",
        notify_chat_id="oc_chat",
        doc_folder_token="",
        trigger_status="待生成方案",
        processing_status="生成中",
        completed_status="已生成",
        failed_status="生成失败",
        status_field="状态",
        title_field="需求标题",
        requester_field="提出人",
        proposal_field="方案链接",
        error_field="处理备注",
        runtime_dir=runtime_dir,
        poll_seconds=300,
        enable_polling=True,
        enable_chat_commands=True,
        log_level="INFO",
        bot_open_id="ou_bot",
        status_field_id="fld_status",
    )


class AgentTests(unittest.TestCase):
    def test_reply_uses_downstream_results(self) -> None:
        downstream = DownstreamResult(
            analysis=AnalysisResult("需要提供按日期导出的销售汇总。", risks=("导出范围尚未确认",)),
            review=ReviewResult(
                conclusion="可实施",
                recommended_plan="新增独立导出任务，并复用现有查询接口。",
                estimated_effort="2人日",
                dependencies=("销售查询接口",),
                next_actions=("确认导出字段",),
            ),
        )
        with TemporaryDirectory() as directory:
            gateway = FakeGateway(_record())
            agent = DemandAgent(
                settings(Path(directory)),
                gateway,
                downstream=_FixedDownstream(downstream),
                store=ProcessedEventStore(Path(directory) / "events.json"),
            )
            agent.enqueue_record("rec1", revision=7)
            result = required(agent.process_item(required(agent._next_item())))

        self.assertTrue(result.ok)
        self.assertEqual(gateway.statuses[-1][1], "已生成")
        reply = gateway.messages[0][2]
        self.assertIn("# 需求结论：导出报表", reply)
        self.assertIn("新增独立导出任务", reply)
        self.assertIn("2人日", reply)

    def test_pending_downstream_only_acknowledges_record(self) -> None:
        with TemporaryDirectory() as directory:
            gateway = FakeGateway(_record())
            agent = DemandAgent(
                settings(Path(directory)),
                gateway,
                store=ProcessedEventStore(Path(directory) / "events.json"),
            )
            agent.enqueue_record("rec1")
            result = required(agent.process_item(required(agent._next_item())))

        self.assertTrue(result.ok)
        self.assertEqual(result.document_url, "")
        self.assertEqual(gateway.statuses[-1][1], "生成中")
        self.assertIn("已接收", gateway.messages[0][2])
        self.assertEqual(gateway.documents, [])

    def test_chat_demand_creates_ledger_record(self) -> None:
        with TemporaryDirectory() as directory:
            gateway = FakeGateway(_record())
            agent = DemandAgent(
                settings(Path(directory)),
                gateway,
                store=ProcessedEventStore(Path(directory) / "events.json"),
            )
            demand = IncomingDemand(
                text="需求标题：导出报表\n业务目标：减少手工汇总\n功能要求：按日期筛选",
                sender_id="ou_sender",
                message_id="om_demand",
            )
            self.assertTrue(agent.accept_message(demand))
            result = required(agent.process_item(required(agent._next_item())))

        self.assertEqual(result.record_id, "rec-created")
        self.assertEqual(gateway.created[0]["需求标题"], "导出报表")
        self.assertIn("已接收《导出报表》", gateway.replies[-1][1])

    def test_record_event_only_queues_status_transition(self) -> None:
        with TemporaryDirectory() as directory:
            agent = DemandAgent(
                settings(Path(directory)),
                FakeGateway(_record()),
                store=ProcessedEventStore(Path(directory) / "events.json"),
            )
            changed = _record_event("rec2", before="草稿", after="待生成方案")
            unchanged = _record_event("rec1", before="待生成方案", after="待生成方案")
            on_record_changed(agent, as_record_event(changed))
            on_record_changed(agent, as_record_event(unchanged))

            queued = required(agent._next_item())
            self.assertEqual(queued.record_id, "rec2")
            agent.workflows.finish_work(queued.key)
            self.assertIsNone(agent._next_item())

    def test_chat_command_replies_and_queues(self) -> None:
        with TemporaryDirectory() as directory:
            gateway = FakeGateway(_record())
            agent = DemandAgent(
                settings(Path(directory)),
                gateway,
                store=ProcessedEventStore(Path(directory) / "events.json"),
            )
            content = json.dumps({"text": "@_user_1 生成方案 https://example.feishu.cn/base/base-token?record=rec1"})
            mention = SimpleNamespace(key="@_user_1", id=SimpleNamespace(open_id="ou_bot"))
            message = SimpleNamespace(
                message_id="om_message",
                message_type="text",
                chat_type="group",
                content=content,
                mentions=[mention],
            )
            on_message(agent, as_message_event(SimpleNamespace(event=SimpleNamespace(sender=None, message=message))))
            queued = required(agent._next_item())
            self.assertEqual(queued.record_id, "rec1")
            self.assertIn("已接收", gateway.replies[0][1])


def _record() -> DemandRecord:
    return DemandRecord(
        record_id="rec1",
        fields={
            "需求标题": "导出报表",
            "业务目标": "减少手工汇总",
            "功能要求": "支持按日期筛选",
            "验收标准": "导出结果与源数据一致",
        },
        title="导出报表",
        requester_id="ou_user",
        status="待生成方案",
        record_url="https://example.feishu.cn/record/rec1",
    )


def _record_event(record_id: str, *, before: str, after: str):
    field = lambda value: SimpleNamespace(field_id="fld_status", field_value=json.dumps(value), field_identity_value={})
    action = SimpleNamespace(
        record_id=record_id,
        action="record_edited",
        before_value=[field(before)],
        after_value=[field(after)],
    )
    event = SimpleNamespace(
        file_token="base-token",
        table_id="tbl-token",
        revision=3,
        action_list=[action],
    )
    return SimpleNamespace(event=event, header=SimpleNamespace(event_id=f"event-{record_id}"))


class _FixedDownstream:
    def __init__(self, result: DownstreamResult) -> None:
        self.result = result

    def evaluate(self, brief: object) -> DownstreamResult:
        return self.result


if __name__ == "__main__":
    unittest.main()

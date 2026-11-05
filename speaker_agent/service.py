from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass

from .config import Settings
from .contracts import DownstreamResult, RequirementBrief
from .downstream import DownstreamAgent, PendingDownstreamAgent
from .feishu import FeishuApiError, FeishuGateway
from .intake import RequirementNormalizer
from .models import DemandRecord, IncomingDemand, ProcessingResult
from .reply import ReplyFormatter
from .state import ProcessedEventStore


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkItem:
    key: str
    record_id: str = ""
    revision: int | None = None
    reply_to_message_id: str = ""
    source_message: IncomingDemand | None = None


class DemandAgent:
    """Top-level Speaker agent.

    It receives demands, creates one normalized record, and later publishes
    results returned by the lower-layer analysis and review agents.
    """

    def __init__(
        self,
        settings: Settings,
        gateway: FeishuGateway,
        *,
        downstream: DownstreamAgent | None = None,
        normalizer: RequirementNormalizer | None = None,
        formatter: ReplyFormatter | None = None,
        store: ProcessedEventStore | None = None,
    ) -> None:
        self.settings = settings
        self.gateway = gateway
        self.downstream = downstream or PendingDownstreamAgent()
        self.normalizer = normalizer or RequirementNormalizer(settings)
        self.formatter = formatter or ReplyFormatter()
        self.store = store or ProcessedEventStore(settings.runtime_dir / "processed-events.json")
        self._queue: deque[WorkItem] = deque()
        self._queued: set[str] = set()
        self._lock = threading.Lock()
        self._wakeup = threading.Event()
        self._stopped = threading.Event()

    def enqueue_record(
        self,
        record_id: str,
        *,
        revision: int | None = None,
        event_id: str = "",
        reply_to_message_id: str = "",
    ) -> bool:
        key = self._work_key(record_id, revision=revision, event_id=event_id)
        if self.store.seen(key):
            return False
        return self._enqueue(
            WorkItem(
                key=key,
                record_id=record_id,
                revision=revision,
                reply_to_message_id=reply_to_message_id,
            )
        )

    def accept_message(self, demand: IncomingDemand) -> bool:
        key = f"message:{demand.message_id}"
        if self.store.seen(key):
            return False
        return self._enqueue(
            WorkItem(key=key, reply_to_message_id=demand.message_id, source_message=demand)
        )

    def scan_pending(self) -> int:
        records = self.gateway.list_records_by_status(self.settings.trigger_status)
        return sum(self.enqueue_record(record.record_id) for record in records)

    def run(self) -> None:
        while not self._stopped.is_set():
            item = self._next_item()
            if item is None:
                self._wakeup.wait(timeout=1)
                self._wakeup.clear()
                continue
            try:
                self.process_item(item)
            except Exception:
                LOGGER.exception("unexpected failure while processing %s", item.record_id or item.key)

    def stop(self) -> None:
        self._stopped.set()
        self._wakeup.set()

    def process_item(self, item: WorkItem) -> ProcessingResult | None:
        if item.source_message is not None and not item.record_id:
            return self._process_message(item)
        return self._process_record(item)

    def handle_command(self, text: str, message_id: str, sender_id: str = "") -> None:
        command, argument = _split_command(text)
        if command in {"帮助", "/help", "help"}:
            self.gateway.reply_text(message_id, self.help_text())
            return
        if command in {"生成方案", "/proposal", "proposal"}:
            self._queue_existing_record(argument, message_id)
            return
        if command in {"需求", "/demand", "demand"} or _looks_like_requirement(text):
            content = argument if command in {"需求", "/demand", "demand"} else text
            accepted = self.accept_message(
                IncomingDemand(text=content, sender_id=sender_id, message_id=message_id)
            )
            reply = "已接收需求，正在整理需求单。" if accepted else "这条需求已在处理队列中。"
            self.gateway.reply_text(message_id, reply)

    def help_text(self) -> str:
        return (
            f"台账状态改为“{self.settings.trigger_status}”后会自动建档。\n"
            "直接描述需求，或发送：需求 需求标题\n"
            "处理已有记录可发送：生成方案 记录ID"
        )

    def _process_record(self, item: WorkItem) -> ProcessingResult | None:
        try:
            record = self.gateway.get_record(item.record_id)
        except FeishuApiError as exc:
            LOGGER.error("cannot read record %s: %s", item.record_id, exc)
            return None
        if record.status not in {self.settings.trigger_status, self.settings.processing_status}:
            self.store.mark(item.key)
            return None

        try:
            brief = self.normalizer.from_record(record)
            self.gateway.update_status(record.record_id, self.settings.processing_status)
            downstream = self.downstream.evaluate(brief)
            reply, document_url = self._publish(record, brief, downstream)
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            self._mark_failed(record, message)
            self._safe_notify(record, f"《{record.title}》处理失败：{message}", item.reply_to_message_id)
            self.store.mark(item.key)
            return ProcessingResult(record.record_id, ok=False, summary="", error=message)

        self._safe_notify(record, reply, item.reply_to_message_id)
        self.store.mark(item.key)
        return ProcessingResult(record.record_id, ok=True, summary=reply, document_url=document_url)

    def _process_message(self, item: WorkItem) -> ProcessingResult | None:
        demand = item.source_message
        if demand is None:
            return None
        try:
            draft = self.normalizer.from_message(demand)
            record_id = self.gateway.create_record(self.normalizer.ledger_fields(draft))
            record = self.gateway.get_record(record_id)
            brief = self.normalizer.from_record(record, source="飞书会话")
            downstream = self.downstream.evaluate(brief)
            reply, document_url = self._publish(record, brief, downstream)
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            self.gateway.reply_text(item.reply_to_message_id, f"需求建档失败：{message}")
            self.store.mark(item.key)
            return ProcessingResult("", ok=False, summary="", error=message)

        self.gateway.reply_text(item.reply_to_message_id, reply)
        if self.settings.notify_chat_id:
            self.gateway.send_text("chat_id", self.settings.notify_chat_id, reply)
        self.store.mark(item.key)
        return ProcessingResult(record.record_id, ok=True, summary=reply, document_url=document_url)

    def _publish(
        self,
        record: DemandRecord,
        brief: RequirementBrief,
        downstream: DownstreamResult | None,
    ) -> tuple[str, str]:
        if downstream is None:
            self.gateway.update_status(
                record.record_id,
                self.settings.processing_status,
                error="已建档，等待下层需求分析与技术评审",
            )
            return self.formatter.pending(brief), ""

        reply = self.formatter.render(brief, downstream)
        document_token, document_url = self.gateway.create_proposal_document(
            f"需求结论：{brief.title}",
            reply,
        )
        if record.requester_id:
            self.gateway.grant_document_view(document_token, record.requester_id)
        self.gateway.update_status(
            record.record_id,
            self.settings.completed_status,
            proposal_url=document_url,
        )
        return f"{reply}\n台账：{record.record_url}\n结论：{document_url}", document_url

    def _queue_existing_record(self, argument: str, message_id: str) -> None:
        record_id = _extract_record_id(argument)
        if not record_id:
            self.gateway.reply_text(message_id, "请发送：生成方案 记录ID或记录链接")
            return
        accepted = self.enqueue_record(record_id, reply_to_message_id=message_id)
        reply = "已接收，正在读取台账并整理需求单。" if accepted else "这条记录已在处理队列中或刚刚处理过。"
        self.gateway.reply_text(message_id, reply)

    def _enqueue(self, item: WorkItem) -> bool:
        with self._lock:
            if item.key in self._queued:
                return False
            self._queue.append(item)
            self._queued.add(item.key)
        self._wakeup.set()
        return True

    def _next_item(self) -> WorkItem | None:
        with self._lock:
            if not self._queue:
                return None
            item = self._queue.popleft()
            self._queued.discard(item.key)
            return item

    def _safe_notify(self, record: DemandRecord, text: str, reply_to_message_id: str) -> None:
        try:
            if reply_to_message_id:
                self.gateway.reply_text(reply_to_message_id, text)
            elif record.requester_id:
                self.gateway.send_text("open_id", record.requester_id, text)
            if self.settings.notify_chat_id:
                self.gateway.send_text("chat_id", self.settings.notify_chat_id, text)
        except FeishuApiError as exc:
            LOGGER.error("failed to send Feishu reply for %s: %s", record.record_id, exc)

    def _mark_failed(self, record: DemandRecord, error: str) -> None:
        try:
            self.gateway.update_status(record.record_id, self.settings.failed_status, error=error)
        except FeishuApiError as exc:
            LOGGER.error("failed to write error status for %s: %s", record.record_id, exc)

    @staticmethod
    def _work_key(record_id: str, *, revision: int | None, event_id: str) -> str:
        if event_id:
            return f"event:{event_id}"
        if revision is not None:
            return f"record:{record_id}:rev:{revision}"
        return f"record:{record_id}:pending"


def _split_command(text: str) -> tuple[str, str]:
    normalized = text.replace("\u3000", " ").strip()
    if not normalized:
        return "", ""
    command, separator, argument = normalized.partition(" ")
    return command.strip().lower(), argument.strip() if separator else ""


def _extract_record_id(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    marker = "record="
    if marker in text:
        return text.split(marker, 1)[1].split("&", 1)[0].strip()
    return text.split()[0].strip()


def _looks_like_requirement(text: str) -> bool:
    markers = ("需求", "背景", "目标", "验收", "希望", "需要")
    return any(marker in text for marker in markers) and len(text.strip()) >= 8

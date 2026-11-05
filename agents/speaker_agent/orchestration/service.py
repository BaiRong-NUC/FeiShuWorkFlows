from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, replace
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from agents.speaker_agent.config import Settings
from agents.speaker_agent.domain.contracts import DownstreamResult, RequirementBrief, result_from_dict
from agents.speaker_agent.downstream.bridge import DownstreamAgent, FileDownstreamAgent
from agents.speaker_agent.domain.ports import DemandGateway
from agents.speaker_agent.intake.normalizer import RequirementNormalizer
from agents.speaker_agent.domain.models import IncomingDemand, ProcessingResult
from agents.speaker_agent.presentation.reply import ReplyFormatter
from agents.speaker_agent.persistence.state import ProcessedEventStore, WorkflowStore

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkItem:
    key: str
    record_id: str = ""
    revision: int | None = None
    reply_to_message_id: str = ""
    source_message: IncomingDemand | None = None


class DemandAgent:
    """Speaker: intake, durable handoff, result formatting and delivery only."""

    def __init__(self, settings: Settings, gateway: DemandGateway, *,
                 downstream: DownstreamAgent | None = None,
                 normalizer: RequirementNormalizer | None = None,
                 formatter: ReplyFormatter | None = None,
                 store: ProcessedEventStore | None = None) -> None:
        self.settings = settings
        self.gateway = gateway
        self.downstream = downstream or FileDownstreamAgent(settings.runtime_dir / "handoff")
        self.normalizer = normalizer or RequirementNormalizer(settings)
        self.formatter = formatter or ReplyFormatter(settings.reply_template)
        self.store = store or ProcessedEventStore(settings.runtime_dir / "processed-events.json")
        self.workflows = WorkflowStore(settings.runtime_dir / "speaker.sqlite3")
        self._lock = threading.RLock()
        self._wakeup = threading.Event()
        self._stopped = threading.Event()

    def enqueue_record(self, record_id: str, *, revision: int | None = None,
                       event_id: str = "", reply_to_message_id: str = "") -> bool:
        key = self._work_key(record_id, revision=revision, event_id=event_id)
        # Polling is a current-state query, not an event that is finished forever.
        if not key.endswith(":pending") and self.store.seen(key):
            return False
        return self._enqueue(WorkItem(key, record_id, revision, reply_to_message_id))

    def accept_message(self, demand: IncomingDemand) -> bool:
        if not demand.message_id or not demand.text.strip():
            return False
        key = f"message:{demand.message_id}"
        if self.store.seen(key):
            return False
        return self._enqueue(WorkItem(key=key, reply_to_message_id=demand.message_id, source_message=demand))

    def scan_pending(self) -> int:
        return sum(self.enqueue_record(r.record_id)
                   for r in self.gateway.list_records_by_status(self.settings.trigger_status))

    def run(self) -> None:
        next_resume = 0.0
        while not self._stopped.is_set():
            if time.monotonic() >= next_resume:
                self.resume_pending()
                next_resume = time.monotonic() + 5
            item = self._next_item()
            if item is not None:
                self.process_item(item)
            else:
                self._wakeup.wait(timeout=1)
                self._wakeup.clear()

    def stop(self) -> None:
        self._stopped.set()
        self._wakeup.set()

    def process_item(self, item: WorkItem) -> ProcessingResult | None:
        with self._lock:
            try:
                job = self._intake(item)
                result = self._advance(job) if job else None
                if not item.key.endswith(":pending"):
                    self.store.mark(item.key)
                self.workflows.finish_work(item.key)
                return result
            except Exception as exc:
                # Preserve both inbox work and the created-record mapping for retry.
                self.workflows.defer_work(item.key)
                LOGGER.exception("Speaker intake/delivery failed: %s", item.key)
                return ProcessingResult(item.record_id, False, "", error=str(exc))

    def resume_pending(self) -> None:
        with self._lock:
            for job in self.workflows.unfinished_jobs():
                try:
                    self._advance(job)
                except Exception as exc:
                    job["last_error"] = str(exc)
                    self.workflows.save_job(job)
                    LOGGER.error("Speaker job %s awaits retry: %s", job["id"], exc)

    def submit_result(self, request_id: str, result: DownstreamResult) -> ProcessingResult:
        """Internal callback for a future agent transport, never exposed to chat input."""
        with self._lock:
            job = self.workflows.get_job(request_id)
            if job is None:
                raise ValueError("unknown request_id")
            if job["state"] == "superseded":
                raise ValueError("request has been superseded")
            validated = result_from_dict(asdict(result))
            if job["state"] != "done":
                self._merge(job, validated)
            return self._advance(job)

    def handle_command(self, text: str, message_id: str, sender_id: str = "", chat_id: str = "") -> None:
        command, argument = _split_command(text)
        if command in {"帮助", "/help", "help"}:
            self.gateway.reply_text(message_id, self.help_text())
        elif command in {"整理需求", "生成方案", "/proposal", "proposal"}:
            record_id = _extract_record_id(argument, self.settings)
            if not record_id:
                self.gateway.reply_text(message_id, "请发送：整理需求 当前台账的记录ID或记录链接")
                return
            accepted = self.enqueue_record(record_id, reply_to_message_id=message_id)
            self.gateway.reply_text(message_id, "已接收，正在读取台账。" if accepted else "该记录已在处理。")
        elif command in {"需求", "/demand", "demand"} or _looks_like_requirement(text):
            content = argument if command in {"需求", "/demand", "demand"} else text
            if not content.strip():
                self.gateway.reply_text(message_id, "请在“需求”后填写需求描述。")
                return
            self.accept_message(IncomingDemand(content, sender_id, message_id, chat_id))
        elif text.strip():
            self.gateway.reply_text(message_id, "如需建档，请发送：需求 具体描述；发送“帮助”查看用法。")

    def help_text(self) -> str:
        return (f"台账状态改为“{self.settings.trigger_status}”后自动整理。\n"
                "新需求：需求 具体描述（支持多行）\n"
                "已有记录：整理需求 记录ID或链接；仍需处于待处理状态。")

    def _intake(self, item: WorkItem) -> dict | None:
        draft = None
        if item.source_message:
            demand = item.source_message
            draft = self.normalizer.from_message(demand)
            token, record_id = self.workflows.creation(demand.message_id)
            if not record_id:
                record_id = self.gateway.create_record(self.normalizer.ledger_fields(draft), client_token=token)
                self.workflows.created(demand.message_id, record_id)
        else:
            record_id = item.record_id
        record = self.gateway.get_record(record_id)
        if record.status not in {self.settings.trigger_status, self.settings.processing_status}:
            return None
        # The ledger snapshot is authoritative even for a chat-created demand.
        brief = self.normalizer.from_record(record)
        if draft:
            brief = replace(brief, source=draft.source, source_message_id=draft.source_message_id,
                            source_chat_id=draft.source_chat_id)
        fingerprint = _fingerprint(brief)
        previous = self.workflows.latest_job(record_id)
        if previous and previous["fingerprint"] == fingerprint and previous["state"] not in {"done", "superseded"}:
            if item.reply_to_message_id and not previous["reply_to"]:
                previous["reply_to"] = item.reply_to_message_id
                self.workflows.save_job(previous)
            return previous
        if previous and record.status == self.settings.processing_status and previous["state"] == "done":
            return None
        if previous and previous["state"] != "done":
            previous["state"] = "superseded"
            self.workflows.save_job(previous)
        request_id = str(uuid4())
        brief = replace(brief, request_id=request_id)
        job = {"id": request_id, "brief": asdict(brief), "fingerprint": fingerprint,
               "reply_to": item.reply_to_message_id, "state": "waiting", "result": {},
               "acknowledged": False, "delivery": {}, "written": False,
               "document_token": "", "document_url": ""}
        self.workflows.save_job(job)
        return job

    def _advance(self, job: dict) -> ProcessingResult:
        brief = RequirementBrief(**job["brief"])
        if job["state"] == "done":
            return ProcessingResult(brief.record_id, True, job["markdown"], job["document_url"])
        self._validate_current(job)
        result = result_from_dict(job["result"])
        if not result.complete:
            returned = self.downstream.evaluate(brief)
            self._validate_current(job)
            if returned is not None:
                self._merge(job, result_from_dict(asdict(returned)))
                result = result_from_dict(job["result"])
        if not result.complete:
            reply = self.formatter.pending(brief)
            if not job["acknowledged"]:
                self.gateway.update_status(brief.record_id, self.settings.processing_status,
                                           error="已建档，等待下层需求分析与技术评审")
                self._notify(job, reply, phase="ack")
                job["acknowledged"] = True
                self.workflows.save_job(job)
            return ProcessingResult(brief.record_id, True, reply)
        # Render once so retries always deliver the same content and UUIDs.
        if "markdown" not in job:
            job["markdown"] = self.formatter.render(brief, result)
            job["state"] = "publishing"
            self.workflows.save_job(job)
        if self.settings.create_documents and not job["document_token"]:
            token, url = self.gateway.create_proposal_document(f"需求结论：{brief.title}", job["markdown"])
            job["document_token"], job["document_url"] = token, url
            self.workflows.save_job(job)
        if job["document_token"] and brief.requester_id and not job.get("permission_granted"):
            self.gateway.grant_document_view(job["document_token"], brief.requester_id)
            job["permission_granted"] = True
            self.workflows.save_job(job)
        if not job["written"]:
            self._validate_current(job)
            self.gateway.write_result(brief.record_id, job["markdown"], document_url=job["document_url"])
            job["written"] = True
            self.workflows.save_job(job)
        self._notify(job, job["markdown"], phase="result")
        job["state"] = "done"
        job.pop("last_error", None)
        self.workflows.save_job(job)
        return ProcessingResult(brief.record_id, True, job["markdown"], job["document_url"])

    def _validate_current(self, job: dict) -> None:
        record_id = job["brief"]["record_id"]
        latest = self.workflows.latest_job(record_id)
        record = self.gateway.get_record(record_id)
        allowed = {self.settings.trigger_status, self.settings.processing_status}
        if job["state"] == "publishing":
            allowed.add(self.settings.completed_status)
        if (latest is None or latest["id"] != job["id"]
                or _fingerprint(self.normalizer.from_record(record)) != job["fingerprint"]
                or record.status not in allowed):
            job["state"] = "superseded"
            self.workflows.save_job(job)
            raise ValueError("需求内容或状态已变化，旧结果不予回填；请将新需求设为待处理")

    def _merge(self, job: dict, result: DownstreamResult) -> None:
        if job["state"] == "publishing":
            return  # A committed final report is immutable for this request ID.
        current = job["result"]
        for key, value in asdict(result).items():
            if value is not None and (key != "notes" or value):
                current[key] = value
        self.workflows.save_job(job)

    def _notify(self, job: dict, text: str, *, phase: str) -> None:
        brief = job["brief"]
        routes = []
        if job["reply_to"]:
            routes.append(("reply", job["reply_to"]))
        elif brief["requester_id"]:
            routes.append(("open_id", brief["requester_id"]))
        if self.settings.notify_chat_id and self.settings.notify_chat_id != brief["source_chat_id"]:
            routes.append(("chat_id", self.settings.notify_chat_id))
        errors = []
        for kind, target in routes:
            key = f"{phase}:{kind}:{target}"
            delivery = job["delivery"].setdefault(key, {"uuid": str(uuid4()), "sent": False})
            if delivery["sent"]:
                continue
            self.workflows.save_job(job)
            try:
                if kind == "reply":
                    self.gateway.reply_text(target, text, uuid=delivery["uuid"])
                else:
                    self.gateway.send_text(kind, target, text, uuid=delivery["uuid"])
            except Exception as exc:
                errors.append(exc)
                continue
            delivery["sent"] = True
            self.workflows.save_job(job)
        if errors:
            raise errors[0]

    def _enqueue(self, item: WorkItem) -> bool:
        accepted = self.workflows.enqueue(asdict(item))
        self._wakeup.set()
        return accepted

    def _next_item(self) -> WorkItem | None:
        data = self.workflows.next_work()
        if data is None:
            return None
        if data["source_message"]:
            data["source_message"] = IncomingDemand(**data["source_message"])
        return WorkItem(**data)

    @staticmethod
    def _work_key(record_id: str, *, revision: int | None, event_id: str) -> str:
        if event_id:
            return f"event:{event_id}:{record_id}"
        if revision is not None:
            return f"record:{record_id}:rev:{revision}"
        return f"record:{record_id}:pending"


def _fingerprint(brief: RequirementBrief) -> str:
    fields = asdict(brief)
    for key in ("source", "source_message_id", "source_chat_id", "request_id", "record_url"):
        fields.pop(key, None)
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _split_command(text: str) -> tuple[str, str]:
    parts = text.strip().split(maxsplit=1)
    return (parts[0].lower(), parts[1].strip() if len(parts) > 1 else "") if parts else ("", "")


def _extract_record_id(value: str, settings: Settings) -> str:
    text = value.strip()
    if text.startswith("https://"):
        url = urlparse(text)
        query = parse_qs(url.query)
        expected_host = urlparse(settings.bitable_url).hostname
        if not url.hostname or not (url.hostname.endswith(".feishu.cn") or url.hostname == expected_host):
            return ""
        if "/base/" in url.path and url.path.split("/base/", 1)[1].split("/")[0] != settings.bitable_app_token:
            return ""
        if query.get("table", [settings.bitable_table_id])[0] != settings.bitable_table_id:
            return ""
        text = query.get("record", [""])[0]
    return text if re.fullmatch(r"rec[A-Za-z0-9]+", text) else ""


def _looks_like_requirement(text: str) -> bool:
    return any(marker in text for marker in ("需求", "背景", "目标", "验收", "希望", "需要")) and len(text.strip()) >= 8

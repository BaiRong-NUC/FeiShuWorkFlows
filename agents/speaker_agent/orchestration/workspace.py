"""One Speaker, multiple source tables and isolated durable workflows."""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import lark_oapi as lark

from agents.speaker_agent.adapters.feishu.events import on_record_changed
from agents.speaker_agent.adapters.feishu.gateway import FeishuGateway
from agents.speaker_agent.adapters.feishu.sheets_gateway import SheetGateway
from agents.speaker_agent.config import Settings
from agents.speaker_agent.domain.ports import DemandGateway
from agents.speaker_agent.downstream.bridge import FileDownstreamAgent
from agents.speaker_agent.orchestration.service import DemandAgent
from shared.feishu.catalog import TableReference
from shared.feishu.client import FeishuClient
from shared.feishu.contracts import WorkspaceServices

LOGGER = logging.getLogger(__name__)


class SpeakerWorkspace:
    def __init__(self, settings: Settings, *, feishu: WorkspaceServices | None = None) -> None:
        self.settings = settings
        self.feishu = feishu if feishu is not None else FeishuClient(settings.feishu)
        self.agents: dict[str, DemandAgent] = {}
        self.tables: dict[str, TableReference] = {}
        self._lock = threading.RLock()
        self._stopped = threading.Event()
        self.refresh()

    def refresh(self) -> None:
        discovered = self.feishu.catalog.discover(self.settings.ledger_urls)
        if not discovered:
            raise RuntimeError("已配置的文件中没有找到可读取的工作表或数据表")
        with self._lock:
            active: dict[str, TableReference] = {}
            for ref in discovered:
                if ref.key not in self.agents:
                    namespace = hashlib.sha256(ref.key.encode()).hexdigest()[:20]
                    root = self.settings.runtime_dir / "sources" / namespace
                    config = replace(self.settings, runtime_dir=root, source_file_token=ref.file_token,
                                     source_table_id=ref.table_id, source_file_url=ref.file_url,
                                     source_kind=ref.kind, source_table=ref.title,
                                     status_field_id="")
                    gateway: DemandGateway = (SheetGateway(config, ref, feishu=self.feishu) if ref.kind == "sheet"
                                              else FeishuGateway(config, feishu=self.feishu))
                    self.agents[ref.key] = DemandAgent(config, gateway,
                                                     downstream=FileDownstreamAgent(self.settings.runtime_dir / "handoff"))
                    LOGGER.info("已发现 %s：%s / %s", "工作表" if ref.kind == "sheet" else "数据表", ref.file_title or ref.file_token, ref.title)
                else:
                    gateway = self.agents[ref.key].gateway
                    if isinstance(gateway, SheetGateway):
                        gateway.ref = ref
                active[ref.key] = ref
            self.tables = active

    def _active_agents(self) -> list[DemandAgent]:
        with self._lock:
            return [self.agents[key] for key in self.tables]

    def scan_pending(self) -> int:
        # Refresh each polling cycle so newly added worksheets are picked up without restart.
        self.refresh()
        accepted = 0
        for agent in self._active_agents():
            if self._stopped.is_set():
                break
            try:
                accepted += agent.scan_pending()
            except Exception:
                LOGGER.exception("跳过无法读取或不符合需求台账结构的表：%s", agent.settings.source_table)
        return accepted

    def run(self) -> None:
        next_resume = 0.0
        while not self._stopped.is_set():
            resume = time.monotonic() >= next_resume
            if resume:
                next_resume = time.monotonic() + 5
            for agent in self._active_agents():
                if self._stopped.is_set():
                    break
                if resume:
                    agent.resume_pending()
                item = agent._next_item()
                if item is not None:
                    agent.process_item(item)
            self._stopped.wait(1)

    def stop(self) -> None:
        self._stopped.set()
        with self._lock:
            for agent in self.agents.values():
                agent.stop()

    def on_record_event(self, data: lark.drive.v1.P2DriveFileBitableRecordChangedV1) -> None:
        event = data.event
        if event is None:
            return
        key = f"bitable/{event.file_token}/{event.table_id}"
        with self._lock:
            agent = self.agents.get(key) if key in self.tables else None
        if agent is not None:
            on_record_changed(agent, data)

    def table_names(self) -> str:
        with self._lock:
            return "、".join(ref.title for ref in self.tables.values())

    def agent_for(self, key: str) -> DemandAgent | None:
        with self._lock:
            return self.agents.get(key) if key in self.tables else None

    def select_target(self, text: str, *, use_default: bool = True) -> str:
        with self._lock:
            refs = list(self.tables.values())
        explicit = re.match(r"^台账\s*[:：]\s*([^\n]+)\n?(.*)$", text.strip(), re.S)
        target = explicit[1].strip() if explicit else ""
        if explicit:
            candidates = [ref for ref in refs if target in {ref.title, ref.key}]
        else:
            candidates = self._link_targets(text, refs)
            if not candidates:
                candidates = [ref for ref in refs if ref.title.casefold() in text.casefold()]
            if not candidates and use_default and self.settings.chat_target:
                candidates = [ref for ref in refs if self.settings.chat_target in {ref.title, ref.key}]
            if not candidates and use_default and len(refs) == 1:
                candidates = refs
        return candidates[0].key if len(candidates) == 1 else ""

    def handle_command(self, text: str, message_id: str, sender_id: str = "", chat_id: str = "") -> None:
        key = self.select_target(text)
        agent = self.agent_for(key)
        if agent is None:
            names = self.table_names()
            self.feishu.messages.reply_text(
                message_id, f"请在原需求前添加一行“台账：工作表名称”后重新发送。可选：{names}。同名表请使用 文件类型/文件token/表ID。",
                uuid=hashlib.sha256(("route:" + message_id).encode()).hexdigest()[:32])
            return
        explicit = re.match(r"^台账\s*[:：]\s*([^\n]+)\n?(.*)$", text.strip(), re.S)
        content = explicit[2].strip() if explicit else text
        agent.handle_command(content, message_id, sender_id, chat_id)

    @staticmethod
    def _link_targets(text: str, refs: list[TableReference]) -> list[TableReference]:
        matches: dict[str, TableReference] = {}
        for value in re.findall(r"https://[^\s<>]+", text):
            parsed = urlsplit(value.rstrip(").,，。"))
            query = parse_qs(parsed.query)
            for ref in refs:
                original = urlsplit(ref.file_url)
                token = parsed.path.rstrip("/").rsplit("/", 1)[-1]
                if parsed.hostname == original.hostname and (parsed.path == original.path or token == ref.file_token):
                    selected = query.get("sheet" if ref.kind == "sheet" else "table", [""])[0]
                    if selected == ref.table_id:
                        matches[ref.key] = ref
        return list(matches.values())

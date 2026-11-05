from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import asdict
from pathlib import Path

from agents.speaker_agent.domain.intake import DemandInterpreter, IntakeDecision, parse_intake_decision
from agents.speaker_agent.domain.models import IncomingDemand
from agents.speaker_agent.intake.interpreter import ModelDemandInterpreter
from agents.speaker_agent.orchestration.workspace import SpeakerWorkspace
from agents.speaker_agent.persistence.personal import PersonalInbox
from shared.llm.contracts import ModelError
from shared.llm.deepseek import DeepSeekClient

LOGGER = logging.getLogger(__name__)


class PersonalIntake:
    def __init__(self, workspace: SpeakerWorkspace, inbox: PersonalInbox, *,
                 interpreter: DemandInterpreter | None = None) -> None:
        self.workspace, self.inbox = workspace, inbox
        self.away_reply = self.load_away_reply()
        self.interpreter = interpreter
        if interpreter is None and workspace.settings.intake_mode == "deepseek":
            self.interpreter = ModelDemandInterpreter(DeepSeekClient(workspace.settings.deepseek),
                                                     max_chars=workspace.settings.intake_max_chars)

    @staticmethod
    def load_away_reply() -> str:
        text = (Path(__file__).resolve().parents[1] / "templates" / "away.md").read_text(encoding="utf-8-sig").strip()
        if not text:
            raise ValueError("Speaker 普通聊天回复模板不能为空")
        return text

    def drain(self) -> None:
        for batch in self.inbox.pending(time.time()):
            if self.workspace._stopped.is_set():
                return
            try:
                self.process(batch)
            except Exception:
                self.inbox.save(batch, ready=time.time() + 30)
                LOGGER.exception("个人需求暂未交接，保留消息待重试：%s", batch["source"]["message_id"])

    def process(self, batch: dict) -> None:
        demand = IncomingDemand(**batch["source"])
        uuid = hashlib.sha256(("personal:" + demand.message_id).encode()).hexdigest()[:32]
        if batch["latest_text"].strip() == "取消":
            batch["reply"] = "已取消当前待补充的需求。"
            batch["context"] = ""
        elif not batch["decision"] and not batch["reply"]:
            if len(demand.text) > self.workspace.settings.intake_max_chars or batch["attempts"] >= 3:
                batch["reply"] = "需求暂未整理成功，请缩短内容或稍后重新发送。尚未新建台账。"
            else:
                batch["attempts"] += 1
                self.inbox.save(batch)
                try:
                    LOGGER.info("开始识别个人消息：消息=%s，字符数=%s", demand.message_id, len(demand.text))
                    decision = (self.interpreter.interpret(demand.text) if self.interpreter else
                                IntakeDecision(True, title=demand.text.splitlines()[0][:100], requirements=demand.text))
                    batch["decision"] = asdict(parse_intake_decision(asdict(decision)))
                    LOGGER.info("个人消息识别完成：消息=%s，是否需求=%s", demand.message_id, decision.is_requirement)
                except ModelError as exc:
                    if exc.retryable and batch["attempts"] < 3:
                        raise
                    batch["reply"] = "需求理解暂不可用，请稍后重新发送。尚未新建台账。"
        self.inbox.save(batch)
        if batch["decision"] and not batch["reply"]:
            decision = parse_intake_decision(batch["decision"])
            if not decision.is_requirement:
                batch["reply"] = self.away_reply
                batch["context"] = ""
                self.inbox.save(batch)
            elif not batch["target"]:
                target = self.workspace.select_target(batch["latest_text"], use_default=False) or self.workspace.select_target(demand.text)
                if target:
                    batch["target"] = target
                else:
                    batch["context"] = demand.text
                    batch["reply"] = "这项需求属于哪个台账？请直接回复名称：" + self.workspace.table_names() + "。"
                self.inbox.save(batch)
            if decision.is_requirement and batch["target"]:
                agent = self.workspace.agent_for(batch["target"])
                if agent is None:
                    raise RuntimeError("需求目标表暂不可用，保留原交接目标")
                if agent.workflows.get_intake(demand.message_id) is None:
                    agent.workflows.save_intake(demand.message_id, {"source": asdict(demand), "attempts": 1,
                                                                 "reply_uuid": uuid, "decision": batch["decision"]})
                agent.accept_message(demand)
                LOGGER.info("个人需求已交接：消息=%s，台账=%s", demand.message_id, agent.settings.source_table)
        if batch["reply"]:
            self.workspace.feishu.messages.reply_text(demand.message_id, batch["reply"], uuid=uuid)
            LOGGER.info("个人消息已回复：消息=%s", demand.message_id)
        self.inbox.finish(batch, time.time())

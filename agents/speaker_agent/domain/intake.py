from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from shared.llm.contracts import ModelError


class IntakeError(ModelError):
    """Speaker input or model-generated requirement does not meet the contract."""


TEXT_FIELDS = (
    "title", "background", "goal", "requirements", "constraints",
    "acceptance", "priority", "due_date", "links",
)


@dataclass(frozen=True)
class IntakeDecision:
    is_requirement: bool
    title: str = ""
    background: str = ""
    goal: str = ""
    requirements: str = ""
    constraints: str = ""
    acceptance: str = ""
    priority: str = ""
    due_date: str = ""
    links: str = ""
    clarification_questions: tuple[str, ...] = ()
    is_inquiry: bool = False

    @property
    def needs_analysis(self) -> bool:
        return self.is_requirement or self.is_inquiry


class DemandInterpreter(Protocol):
    def interpret(self, text: str) -> IntakeDecision: ...


def parse_intake_decision(value: object) -> IntakeDecision:
    # 已持久化的需求单可能早于咨询类型上线，恢复时默认为非咨询。
    if isinstance(value, dict):
        value = {"is_inquiry": False, **value}
    expected = {"is_requirement", "is_inquiry", *TEXT_FIELDS, "clarification_questions"}
    if not isinstance(value, dict) or set(value) != expected:
        raise IntakeError("模型需求字段不符合约定")
    if type(value["is_requirement"]) is not bool:
        raise IntakeError("模型的需求标记必须是布尔值")
    if type(value["is_inquiry"]) is not bool or (value["is_requirement"] and value["is_inquiry"]):
        raise IntakeError("需求与咨询标记必须是互斥的布尔值")
    fields: dict[str, str] = {}
    for name in TEXT_FIELDS:
        text = value[name]
        if not isinstance(text, str) or len(text) > 8000:
            raise IntakeError("模型需求字段类型或长度不符合约定")
        fields[name] = text.strip()
    questions = value["clarification_questions"]
    if not isinstance(questions, (list, tuple)) or len(questions) > 10 or any(
        not isinstance(q, str) or not q.strip() or len(q) > 500 for q in questions
    ):
        raise IntakeError("模型待确认问题不符合约定")
    if (value["is_requirement"] or value["is_inquiry"]) and not fields["title"]:
        raise IntakeError("模型未返回需求标题")
    return IntakeDecision(value["is_requirement"], **fields, clarification_questions=tuple(questions),
                          is_inquiry=value["is_inquiry"])

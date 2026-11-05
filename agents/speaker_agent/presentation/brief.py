from dataclasses import asdict
from pathlib import Path
from string import Template

from agents.speaker_agent.domain.contracts import RequirementBrief
from agents.speaker_agent.domain.intake import TEXT_FIELDS


class BriefFormatter:
    def __init__(self) -> None:
        self.template = Template((Path(__file__).resolve().parents[1] / "templates" / "requirement.md").read_text(encoding="utf-8-sig"))
        self.template.substitute({name: "" for name in (*TEXT_FIELDS, "missing_fields", "clarification_questions")})

    def render(self, brief: RequirementBrief) -> str:
        data = asdict(brief)
        fields = {name: data[name] or "待补充" for name in TEXT_FIELDS}
        fields["missing_fields"] = "、".join(brief.missing_fields) or "关键字段已填写，内容仍待确认"
        fields["clarification_questions"] = "\n".join(f"- {q}" for q in brief.clarification_questions) or "- 暂无，需求分析与技术评审由下层继续处理。"
        return self.template.substitute(fields).strip() + "\n"

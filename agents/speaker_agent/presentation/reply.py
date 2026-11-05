from __future__ import annotations

from pathlib import Path
from string import Template

from agents.speaker_agent.domain.contracts import AnalysisResult, DownstreamResult, RequirementBrief, ReviewResult


class ReplyFormatter:
    """Render downstream results in the fixed requester-facing Markdown format."""

    def __init__(self, template_path: Path | None = None) -> None:
        path = template_path or Path(__file__).resolve().parents[1] / "templates" / "reply.md"
        self.template = Template(path.read_text(encoding="utf-8-sig"))
        # Fail at startup, before accepting work or writing to Feishu.
        self.template.substitute({key: "" for key in (
            "title", "analysis_summary", "recommended_plan", "effort_and_dependencies",
            "risks_and_questions", "next_actions", "record_url", "request_id",
        )})

    def render(self, brief: RequirementBrief, downstream: DownstreamResult) -> str:
        analysis = downstream.analysis
        review = downstream.review
        return self.template.substitute(
            title=_sentence(brief.title),
            analysis_summary=_sentence(analysis.summary if analysis else "需求分析待返回"),
            recommended_plan=_sentence(review.recommended_plan if review else "技术方案待返回"),
            effort_and_dependencies="\n".join(_facts(review)),
            risks_and_questions="\n".join(_risks(brief, analysis, review)),
            next_actions="\n".join(_next_actions(analysis, review)),
            record_url=brief.record_url or "未提供", request_id=brief.request_id,
        ).strip() + "\n"

    def pending(self, brief: RequirementBrief) -> str:
        lines = [
            f"已接收《{brief.title}》并完成需求建档。",
            "下层需求分析和技术评审完成后，会按统一格式回复结论。",
        ]
        if brief.missing_fields:
            lines.append("当前缺少：" + "、".join(brief.missing_fields) + "。")
        if brief.clarification_questions:
            lines.append("待确认：" + "；".join(brief.clarification_questions[:3]))
        if brief.record_url:
            lines.append(f"台账：{brief.record_url}")
        return "\n".join(lines)


def _facts(review: ReviewResult | None) -> list[str]:
    if review is None:
        return ["- 工作量：待技术评审", "- 依赖：待技术评审"]
    effort = review.estimated_effort or "待评估"
    dependencies = "、".join(review.dependencies) or "未提供，待确认"
    return [
        f"- 评审结论：{review.conclusion}",
        f"- 工作量：{effort}",
        f"- 依赖：{dependencies}",
    ]


def _risks(
    brief: RequirementBrief,
    analysis: AnalysisResult | None,
    review: ReviewResult | None,
) -> list[str]:
    items = [f"待补充：{name}" for name in brief.missing_fields]
    items.extend(brief.clarification_questions)
    if analysis is not None:
        items.extend(analysis.risks)
        items.extend(analysis.open_questions)
    if review is not None:
        items.extend(review.risks)
    if not items:
        return ["- 下层未提供风险项，待确认。"]
    return [f"- {item}" for item in _unique(items)]


def _next_actions(analysis: AnalysisResult | None, review: ReviewResult | None) -> list[str]:
    actions: list[str] = []
    if review is not None:
        actions.extend(review.next_actions)
    if analysis is not None:
        actions.extend(analysis.open_questions)
    if not actions:
        actions.append("请需求人确认上述结论。" if analysis and review else "等待下层分析与评审结果。")
    return [f"{index}. {action}" for index, action in enumerate(_unique(actions), start=1)]


def _sentence(value: str) -> str:
    text = " ".join(value.split())
    return text or "暂无内容。"


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        cleaned = " ".join(value.split())
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result

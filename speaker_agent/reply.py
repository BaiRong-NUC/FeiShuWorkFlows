from __future__ import annotations

from .contracts import AnalysisResult, DownstreamResult, RequirementBrief, ReviewResult


class ReplyFormatter:
    """Render downstream results in the fixed requester-facing Markdown format."""

    def render(self, brief: RequirementBrief, downstream: DownstreamResult) -> str:
        analysis = downstream.analysis
        review = downstream.review
        lines = [
            f"# 需求结论：{brief.title}",
            "",
            "## 需求结论",
            "",
            _sentence(analysis.summary if analysis else "下层需求分析尚未返回结论。"),
            "",
            "## 建议方案",
            "",
            _sentence(review.recommended_plan if review else "下层技术评审尚未返回方案。"),
            "",
            "## 工作量与依赖",
            "",
        ]
        lines.extend(_facts(review))
        lines.extend(["", "## 风险与待确认", ""])
        lines.extend(_risks(brief, analysis, review))
        lines.extend(["", "## 下一步", ""])
        lines.extend(_next_actions(analysis, review))
        return "\n".join(lines).strip() + "\n"

    def pending(self, brief: RequirementBrief) -> str:
        lines = [
            f"已接收《{brief.title}》并完成需求建档。",
            "下层需求分析和技术评审完成后，会按统一格式回复结论。",
        ]
        if brief.missing_fields:
            lines.append("当前缺少：" + "、".join(brief.missing_fields) + "。")
        if brief.record_url:
            lines.append(f"台账：{brief.record_url}")
        return "\n".join(lines)


def _facts(review: ReviewResult | None) -> list[str]:
    if review is None:
        return ["- 工作量：待技术评审", "- 依赖：待技术评审"]
    effort = review.estimated_effort or "待评估"
    dependencies = "、".join(review.dependencies) or "无额外依赖"
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
    items = list(brief.missing_fields)
    if analysis is not None:
        items.extend(analysis.risks)
        items.extend(analysis.open_questions)
    if review is not None:
        items.extend(review.risks)
    if not items:
        return ["- 暂无额外风险。"]
    return [f"- {item}" for item in _unique(items)]


def _next_actions(analysis: AnalysisResult | None, review: ReviewResult | None) -> list[str]:
    actions: list[str] = []
    if review is not None:
        actions.extend(review.next_actions)
    if analysis is not None:
        actions.extend(analysis.open_questions)
    if not actions:
        actions.append("等待下层分析与评审结果。")
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

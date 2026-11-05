from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RequirementBrief:
    """Normalized demand handed from the top agent to downstream agents."""

    record_id: str
    title: str
    requester_id: str
    source: str
    background: str
    goal: str
    requirements: str
    constraints: str
    acceptance: str
    priority: str
    due_date: str
    links: str
    record_url: str
    missing_fields: tuple[str, ...] = ()
    raw_text: str = ""
    source_message_id: str = ""
    source_chat_id: str = ""
    request_id: str = ""


@dataclass(frozen=True)
class AnalysisResult:
    summary: str
    scope: tuple[str, ...] = ()
    user_value: str = ""
    risks: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewResult:
    conclusion: str
    recommended_plan: str = ""
    estimated_effort: str = ""
    dependencies: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class DownstreamResult:
    analysis: AnalysisResult | None = None
    review: ReviewResult | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def complete(self) -> bool:
        return bool(self.analysis and self.analysis.summary.strip()
                    and self.review and self.review.conclusion.strip())


def result_from_dict(data: object) -> DownstreamResult:
    """Validate the lower-agent wire contract without coercing malformed data."""
    if not isinstance(data, dict) or set(data) - {"analysis", "review", "notes"}:
        raise ValueError("invalid downstream result fields")

    def strings(value):
        if not isinstance(value, (list, tuple)) or any(not isinstance(x, str) for x in value):
            raise ValueError("expected a list of strings")
        return tuple(value)

    def section(name, cls, required, lists):
        raw = data.get(name)
        if raw is None:
            return None
        if not isinstance(raw, dict) or set(raw) - set(cls.__dataclass_fields__):
            raise ValueError(f"invalid {name} fields")
        if not isinstance(raw.get(required), str) or not raw[required].strip():
            raise ValueError(f"{name}.{required} is required")
        values = {}
        for key, value in raw.items():
            if key in lists:
                values[key] = strings(value)
            elif isinstance(value, str):
                values[key] = value
            else:
                raise ValueError(f"{name}.{key} must be text")
        return cls(**values)

    return DownstreamResult(
        analysis=section("analysis", AnalysisResult, "summary", {"scope", "risks", "open_questions"}),
        review=section("review", ReviewResult, "conclusion", {"dependencies", "risks", "next_actions"}),
        notes=strings(data.get("notes", ())),
    )

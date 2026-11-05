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

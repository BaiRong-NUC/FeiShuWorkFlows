from __future__ import annotations

from .contracts import DownstreamResult, RequirementBrief


class DownstreamAgent:
    """Boundary for the analysis and technical-review agent.

    The top-level Speaker only supplies a normalized requirement. The lower
    layer owns requirement analysis, technical review, and coordination with
    the program, test, and data agents.
    """

    def evaluate(self, brief: RequirementBrief) -> DownstreamResult | None:
        raise NotImplementedError


class PendingDownstreamAgent(DownstreamAgent):
    """Placeholder used until the lower-layer agent is implemented."""

    def evaluate(self, brief: RequirementBrief) -> None:
        return None

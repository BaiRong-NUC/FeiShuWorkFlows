from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from agents.speaker_agent.domain.contracts import DownstreamResult, RequirementBrief, result_from_dict

LOGGER = logging.getLogger(__name__)


class DownstreamAgent(Protocol):
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


class FileDownstreamAgent(DownstreamAgent):
    """Local handoff adapter; analysis/review agents are independently implemented.

    Consumers atomically write inbox/<request_id>.json. Each poll accepts either
    or both sections; Speaker persists and merges them before final delivery.
    """

    def __init__(self, root: Path) -> None:
        self.outbox = root / "outbox"
        self.inbox = root / "inbox"
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.inbox.mkdir(parents=True, exist_ok=True)

    def evaluate(self, brief: RequirementBrief) -> DownstreamResult | None:
        if not brief.request_id:
            raise ValueError("request_id is required for downstream handoff")
        path = self.outbox / f"{brief.request_id}.json"
        if not path.exists():
            payload = {"schema_version": 1, "request_id": brief.request_id, "brief": asdict(brief)}
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, path)
            LOGGER.info("已写入下层交接：请求=%s，类型=%s", brief.request_id, brief.task_type)
        merged = {}
        for suffix in (".json", ".analysis.json", ".review.json"):
            result_path = self.inbox / f"{brief.request_id}{suffix}"
            if not result_path.exists():
                continue
            payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict) or payload.get("schema_version") != 1 or payload.get("request_id") != brief.request_id:
                raise ValueError("downstream result schema_version/request_id mismatch")
            result = result_from_dict(payload.get("result"))
            if suffix == ".analysis.json" and result.review is not None:
                raise ValueError("analysis file cannot supply technical review")
            if suffix == ".review.json" and result.analysis is not None:
                raise ValueError("review file cannot supply requirement analysis")
            merged.update({key: value for key, value in asdict(result).items() if value is not None and value != ()})
        return result_from_dict(merged) if merged else None

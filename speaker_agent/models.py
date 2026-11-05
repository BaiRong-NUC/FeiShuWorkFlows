from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class DemandRecord:
    record_id: str
    fields: dict[str, Any]
    title: str
    requester_id: str
    status: str
    record_url: str = ""


@dataclass(frozen=True)
class Proposal:
    title: str
    markdown: str
    summary: str
    document_url: str = ""
    document_token: str = ""


@dataclass(frozen=True)
class ProcessingResult:
    record_id: str
    ok: bool
    summary: str
    document_url: str = ""
    error: str = ""
    processed_at: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True)
class IncomingDemand:
    text: str
    sender_id: str
    message_id: str
    chat_id: str = ""

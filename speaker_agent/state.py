from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path


class ProcessedEventStore:
    """Persist processed event and record revisions across restarts."""

    def __init__(self, path: Path, *, retention: timedelta = timedelta(days=30)) -> None:
        self.path = path
        self.retention = retention
        self._lock = threading.Lock()
        self._events = self._load()

    def seen(self, key: str) -> bool:
        with self._lock:
            item = self._events.get(key)
            if item is None:
                return False
            try:
                seen_at = datetime.fromisoformat(item)
            except ValueError:
                return True
            return datetime.now() - seen_at <= self.retention

    def mark(self, key: str) -> None:
        with self._lock:
            self._events[key] = datetime.now().isoformat(timespec="seconds")
            self._cleanup()
            self._save()

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): str(value) for key, value in data.items()}

    def _cleanup(self) -> None:
        cutoff = datetime.now() - self.retention
        expired: list[str] = []
        for key, value in self._events.items():
            try:
                seen_at = datetime.fromisoformat(value)
            except ValueError:
                continue
            if seen_at < cutoff:
                expired.append(key)
        for key in expired:
            del self._events[key]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self._events, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

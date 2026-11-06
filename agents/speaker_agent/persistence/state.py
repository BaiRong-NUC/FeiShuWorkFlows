from __future__ import annotations

import json
import os
import threading
import sqlite3
import time
from uuid import uuid4
from contextlib import contextmanager
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


class WorkflowStore:
    """Durable inbox and requirement snapshots for one Speaker process."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS inbox (
                    key TEXT PRIMARY KEY, payload TEXT NOT NULL, ready REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, record_id TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS creations (
                    message_id TEXT PRIMARY KEY, token TEXT NOT NULL, record_id TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS intakes (
                    message_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, item: dict) -> bool:
        with self.connect() as db:
            return db.execute("INSERT OR IGNORE INTO inbox(key,payload) VALUES (?,?)",
                              (item["key"], json.dumps(item, ensure_ascii=False))).rowcount == 1

    def next_work(self) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM inbox WHERE ready<=? ORDER BY rowid LIMIT 1",
                             (time.time(),)).fetchone()
            return json.loads(row[0]) if row else None

    def finish_work(self, key: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM inbox WHERE key=?", (key,))

    def defer_work(self, key: str, *, delay: int = 30) -> None:
        with self.connect() as db:
            db.execute("UPDATE inbox SET ready=? WHERE key=?", (time.time() + delay, key))

    def save_job(self, job: dict) -> None:
        with self.connect() as db:
            db.execute("""INSERT INTO jobs(id,record_id,payload) VALUES (?,?,?)
                       ON CONFLICT(id) DO UPDATE SET payload=excluded.payload""",
                       (job["id"], job["brief"]["record_id"], json.dumps(job, ensure_ascii=False)))

    def get_job(self, request_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM jobs WHERE id=?", (request_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def latest_job(self, record_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM jobs WHERE record_id=? ORDER BY rowid DESC LIMIT 1",
                             (record_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def unfinished_jobs(self) -> list[dict]:
        with self.connect() as db:
            jobs = [json.loads(row[0]) for row in db.execute("SELECT payload FROM jobs ORDER BY rowid")]
        return [job for job in jobs if job["state"] not in {"done", "superseded", "cancelled"}]

    def cancel_record(self, record_id: str, reason: str) -> None:
        """Retain history, but stop jobs and queued work for a removed source row."""
        with self.connect() as db:
            for job_id, payload in db.execute("SELECT id,payload FROM jobs WHERE record_id=?", (record_id,)).fetchall():
                job = json.loads(payload)
                if job["state"] not in {"done", "superseded", "cancelled"}:
                    job["state"] = "cancelled"
                    job["last_error"] = reason
                    job.pop("retry_at", None)
                    db.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(job, ensure_ascii=False), job_id))
            message_ids = {row[0] for row in db.execute("SELECT message_id FROM creations WHERE record_id=?", (record_id,))}
            for key, payload in db.execute("SELECT key,payload FROM inbox").fetchall():
                item = json.loads(payload)
                source = item.get("source_message") or {}
                if item.get("record_id") == record_id or source.get("message_id") in message_ids:
                    db.execute("DELETE FROM inbox WHERE key=?", (key,))

    def creation(self, message_id: str) -> tuple[str, str]:
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO creations(message_id,token) VALUES (?,?)",
                       (message_id, str(uuid4())))
            return db.execute("SELECT token,record_id FROM creations WHERE message_id=?", (message_id,)).fetchone()

    def created(self, message_id: str, record_id: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE creations SET record_id=? WHERE message_id=?", (record_id, message_id))

    def get_intake(self, message_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM intakes WHERE message_id=?", (message_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def save_intake(self, message_id: str, payload: dict) -> None:
        with self.connect() as db:
            db.execute("""INSERT INTO intakes(message_id,payload) VALUES (?,?)
                       ON CONFLICT(message_id) DO UPDATE SET payload=excluded.payload""",
                       (message_id, json.dumps(payload, ensure_ascii=False)))

    def intake_for_record(self, record_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("""SELECT intakes.payload FROM intakes JOIN creations
                               ON intakes.message_id=creations.message_id WHERE creations.record_id=?""",
                             (record_id,)).fetchone()
            return json.loads(row[0]) if row else None

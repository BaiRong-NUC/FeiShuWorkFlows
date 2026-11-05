from __future__ import annotations

import json
import hashlib
import time
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from agents.speaker_agent.domain.models import IncomingDemand
from shared.feishu.personal import PersonalChat, PersonalMessage


class PersonalInbox:
    """先原子保存消息和读取位置，再执行大模型或台账操作。"""
    def __init__(self, path: Path, started_ms: int, *, own_id: str = "", include_self: bool = False) -> None:
        self.path = path
        self.own_id, self.include_self = own_id, include_self
        # 自测只接收本次启动后手动发出的本人消息，不补处理旧自动回复。
        self.self_started_ms = started_ms
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS cursors (chat TEXT PRIMARY KEY, end INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, chat TEXT NOT NULL, sender TEXT NOT NULL,
                    text TEXT NOT NULL, created INTEGER NOT NULL, batch TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, chat TEXT NOT NULL, payload TEXT NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0, ready REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS contexts (
                    chat TEXT PRIMARY KEY, text TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS sent_messages (id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS outgoing_attempts (
                    target_type TEXT NOT NULL, target_id TEXT NOT NULL,
                    digest TEXT NOT NULL, started_ms INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS outgoing_digest ON outgoing_attempts(digest,started_ms);
            """)
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('started_ms',?)", (started_ms,))
            self.started_ms = int(db.execute("SELECT value FROM metadata WHERE key='started_ms'").fetchone()[0])

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def cursor(self, chat: str) -> int:
        with self.connect() as db:
            row = db.execute("SELECT end FROM cursors WHERE chat=?", (chat,)).fetchone()
            return max(self.started_ms // 1000, int(row[0]) - 30 if row else 0)

    def before_send(self, target_type: str, target_id: str, text: str) -> None:
        with self.connect() as db:
            if target_type == "message_id":
                row = db.execute("SELECT chat FROM messages WHERE id=?", (target_id,)).fetchone()
                target_type, target_id = ("chat_id", row[0]) if row else ("unknown", "")
            db.execute("INSERT INTO outgoing_attempts VALUES (?,?,?,?)",
                       (target_type, target_id, self._digest(text), int(time.time() * 1000)))

    def after_send(self, message_id: str) -> None:
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO sent_messages VALUES (?)", (message_id,))

    @staticmethod
    def _digest(text: str) -> str:
        return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()

    def _is_automatic(self, db: sqlite3.Connection, chat: PersonalChat, item: PersonalMessage) -> bool:
        if db.execute("SELECT 1 FROM sent_messages WHERE id=?", (item.message_id,)).fetchone():
            return True
        # 发送成功但响应丢失/尚未保存 ID 时，用发送前持久化的会话、正文摘要和时间窗口兜底。
        attempts = db.execute("SELECT target_type,target_id FROM outgoing_attempts WHERE digest=? AND started_ms BETWEEN ? AND ?",
                              (self._digest(item.text), item.created_ms - 180000, item.created_ms + 30000))
        return any(kind == "unknown" or (kind == "chat_id" and target == chat.chat_id) or
                   (kind == "open_id" and target == chat.peer_id) for kind, target in attempts)

    def stage(self, chat: PersonalChat, messages: list[PersonalMessage], end: int) -> int:
        accepted = 0
        with self.connect() as db:
            for item in messages:
                if item.chat_id != chat.chat_id or item.created_ms < self.started_ms:
                    continue
                if self.own_id and item.sender_id == self.own_id:
                    if (not self.include_self or item.created_ms < self.self_started_ms or
                            self._is_automatic(db, chat, item)):
                        continue
                elif item.sender_id != chat.peer_id:
                    continue
                accepted += db.execute("INSERT OR IGNORE INTO messages(id,chat,sender,text,created) VALUES (?,?,?,?,?)",
                                       (item.message_id, item.chat_id, item.sender_id, item.text, item.created_ms)).rowcount
            db.execute("INSERT INTO cursors VALUES (?,?) ON CONFLICT(chat) DO UPDATE SET end=MAX(end,excluded.end)",
                       (chat.chat_id, end))
        return accepted

    def pending(self, now: float) -> list[dict]:
        with self.connect() as db:
            chats = db.execute("SELECT chat FROM messages WHERE batch='' GROUP BY chat ORDER BY MIN(created)").fetchall()
            for (chat,) in chats:
                if db.execute("SELECT 1 FROM batches WHERE chat=? AND done=0", (chat,)).fetchone():
                    continue
                rows = db.execute("SELECT id,sender,text,created FROM messages WHERE chat=? AND batch='' ORDER BY created,id LIMIT 100", (chat,)).fetchall()
                if not rows or rows[-1][3] > (now - 3) * 1000:
                    continue
                context = db.execute("SELECT text FROM contexts WHERE chat=? AND expires>?", (chat, now)).fetchone()
                text = "\n".join(([context[0]] if context else []) + [row[2] for row in rows])
                demand = IncomingDemand(text, rows[-1][1], rows[-1][0], chat)
                batch = {"source": asdict(demand), "latest_text": rows[-1][2], "decision": None,
                         "attempts": 0, "target": "", "reply": "", "context": ""}
                db.execute("INSERT INTO batches(id,chat,payload) VALUES (?,?,?)",
                           (demand.message_id, chat, json.dumps(batch, ensure_ascii=False)))
                db.executemany("UPDATE messages SET batch=? WHERE id=?", [(demand.message_id, row[0]) for row in rows])
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM batches WHERE done=0 AND ready<=? ORDER BY rowid", (now,))]

    def save(self, batch: dict, *, ready: float = 0) -> None:
        with self.connect() as db:
            db.execute("UPDATE batches SET payload=?,ready=? WHERE id=?",
                       (json.dumps(batch, ensure_ascii=False), ready, batch["source"]["message_id"]))

    def finish(self, batch: dict, now: float) -> None:
        with self.connect() as db:
            db.execute("UPDATE batches SET done=1 WHERE id=?", (batch["source"]["message_id"],))
            db.execute("DELETE FROM contexts WHERE chat=?", (batch["source"]["chat_id"],))
            if batch["context"]:
                db.execute("INSERT INTO contexts VALUES (?,?,?)",
                           (batch["source"]["chat_id"], batch["context"], now + 1800))

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from agents.speaker_agent.domain.models import IncomingDemand
from shared.feishu.personal import PersonalChat, PersonalMessage


class PersonalInbox:
    """先原子保存消息和读取位置，再执行大模型或台账操作。"""
    def __init__(self, path: Path, started_ms: int) -> None:
        self.path = path
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

    def stage(self, chat: PersonalChat, messages: list[PersonalMessage], end: int) -> None:
        with self.connect() as db:
            for item in messages:
                if item.chat_id != chat.chat_id or item.sender_id != chat.peer_id or item.created_ms < self.started_ms:
                    continue
                db.execute("INSERT OR IGNORE INTO messages(id,chat,sender,text,created) VALUES (?,?,?,?,?)",
                           (item.message_id, item.chat_id, item.sender_id, item.text, item.created_ms))
            db.execute("INSERT INTO cursors VALUES (?,?) ON CONFLICT(chat) DO UPDATE SET end=MAX(end,excluded.end)",
                       (chat.chat_id, end))

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

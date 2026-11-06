"""Keep sheet creation tokens local while the visible ID remains a sequence number."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class SheetCreationStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        db = self._connect()
        try:
            with db:
                db.execute("""CREATE TABLE IF NOT EXISTS sheet_creations (
                    source TEXT NOT NULL, token TEXT NOT NULL, number INTEGER NOT NULL,
                    fields TEXT NOT NULL, state TEXT NOT NULL,
                    PRIMARY KEY(source,token), UNIQUE(source,number))""")
        finally:
            db.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def get(self, source: str, token: str) -> dict[str, Any] | None:
        db = self._connect()
        try:
            row = db.execute("SELECT number,fields,state FROM sheet_creations WHERE source=? AND token=?",
                             (source, token)).fetchone()
            return {"number": row[0], "fields": json.loads(row[1]), "state": row[2]} if row else None
        finally:
            db.close()

    def reserve(self, source: str, token: str, minimum: int, fields: dict[str, Any]) -> dict[str, Any]:
        db = self._connect()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT number,fields,state FROM sheet_creations WHERE source=? AND token=?",
                                 (source, token)).fetchone()
                if row:
                    if row[2] != "prepared":
                        return {"number": row[0], "fields": json.loads(row[1]), "state": row[2]}
                    # Before any write starts we may follow concurrent manual additions.
                    number = row[0]
                    if number < minimum:
                        last = db.execute("SELECT MAX(number) FROM sheet_creations WHERE source=?", (source,)).fetchone()[0]
                        number = max(minimum, (last or 0) + 1)
                    values = {**fields, "ID": number}
                    db.execute("UPDATE sheet_creations SET number=?,fields=? WHERE source=? AND token=?",
                               (number, json.dumps(values, ensure_ascii=False), source, token))
                    return {"number": number, "fields": values, "state": "prepared"}
                last = db.execute("SELECT MAX(number) FROM sheet_creations WHERE source=?", (source,)).fetchone()[0]
                number = max(minimum, (last or 0) + 1)
                values = {**fields, "ID": number}
                db.execute("INSERT INTO sheet_creations VALUES (?,?,?,?,?)",
                           (source, token, number, json.dumps(values, ensure_ascii=False), "prepared"))
                return {"number": number, "fields": values, "state": "prepared"}
        finally:
            db.close()

    def start(self, source: str, token: str) -> bool:
        db = self._connect()
        try:
            with db:
                return db.execute("UPDATE sheet_creations SET state='sending' WHERE source=? AND token=? AND state='prepared'",
                                  (source, token)).rowcount == 1
        finally:
            db.close()

    def confirm(self, source: str, token: str) -> None:
        db = self._connect()
        try:
            with db:
                db.execute("UPDATE sheet_creations SET state='confirmed' WHERE source=? AND token=?", (source, token))
        finally:
            db.close()

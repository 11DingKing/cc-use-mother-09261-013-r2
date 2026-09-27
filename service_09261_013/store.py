"""SQLite 持久化: 案件快照 + 追加式事件日志 + 幂等键, 单事务提交。"""
import json
import sqlite3
from dataclasses import asdict

from .workflow import Case, Event, KeyRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases(
    id TEXT PRIMARY KEY,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
    seq INTEGER PRIMARY KEY,
    case_id TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency(
    key TEXT PRIMARY KEY,
    body TEXT NOT NULL
);
"""


class SQLiteStore:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.db.commit()

    @staticmethod
    def _dump(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def commit_command(self, case, event, key_record=None):
        """一次命令产生的案件快照、审计事件与幂等记录写入同一事务。"""
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO cases(id, body) VALUES(?, ?)",
                (case.id, self._dump(asdict(case))))
            self.db.execute(
                "INSERT INTO events(seq, case_id, body) VALUES(?, ?, ?)",
                (event.seq, event.case_id, self._dump(asdict(event))))
            if key_record is not None:
                self.db.execute(
                    "INSERT OR IGNORE INTO idempotency(key, body) VALUES(?, ?)",
                    (key_record.key, self._dump(asdict(key_record))))

    def load_cases(self):
        rows = self.db.execute("SELECT body FROM cases").fetchall()
        return [Case(**json.loads(body)) for (body,) in rows]

    def load_events(self):
        rows = self.db.execute(
            "SELECT body FROM events ORDER BY seq").fetchall()
        return [Event(**json.loads(body)) for (body,) in rows]

    def load_keys(self):
        rows = self.db.execute("SELECT body FROM idempotency").fetchall()
        return [KeyRecord(**json.loads(body)) for (body,) in rows]

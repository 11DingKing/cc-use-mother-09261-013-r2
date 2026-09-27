"""SQLite 状态仓储：案件投影 + 追加式审计事件 + 幂等键 + 申报关联。

所有写路径都在 transaction()（BEGIN IMMEDIATE）内完成，SQLite 会串行化并发写事务；
events 表上的 UNIQUE(case_id, seq) 是版本冲突的最后防线，保证同一版本只落一条事件。
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases(
  case_id TEXT PRIMARY KEY,
  applicant TEXT NOT NULL,
  supplier TEXT NOT NULL,
  title TEXT NOT NULL,
  state TEXT NOT NULL,
  version INTEGER NOT NULL,
  round INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  action TEXT NOT NULL,
  actor TEXT NOT NULL,
  from_state TEXT,
  to_state TEXT NOT NULL,
  round INTEGER NOT NULL,
  reason TEXT,
  detail TEXT NOT NULL DEFAULT '{}',
  idem_key TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(case_id, seq)
);
CREATE TABLE IF NOT EXISTS idempotency(
  key TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  action TEXT NOT NULL,
  response TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relationships(
  person TEXT NOT NULL,
  supplier TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(person, supplier)
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


class SQLiteStore:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        """串行化写事务：进入即取写锁，提交前其他写者等待。"""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    # ---------- 案件投影 ----------
    def get_case(self, case_id):
        row = self.db.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        return dict(row) if row else None

    def list_cases(self):
        rows = self.db.execute("SELECT * FROM cases ORDER BY case_id").fetchall()
        return [dict(r) for r in rows]

    def insert_case(self, case_id, applicant, supplier, title):
        at = _now()
        self.db.execute(
            "INSERT INTO cases(case_id,applicant,supplier,title,state,version,round,created_at,updated_at)"
            " VALUES(?,?,?,?,'draft',1,0,?,?)",
            (case_id, applicant, supplier, title, at, at),
        )
        return self.get_case(case_id)

    def update_case(self, case_id, expected_version, *, state, version, round_):
        """乐观并发守卫：仅当当前版本等于期望值时才更新，返回是否命中。"""
        cur = self.db.execute(
            "UPDATE cases SET state=?, version=?, round=?, updated_at=?"
            " WHERE case_id=? AND version=?",
            (state, version, round_, _now(), case_id, expected_version),
        )
        return cur.rowcount == 1

    # ---------- 审计事件（只追加，不修改） ----------
    def append_event(self, *, case_id, seq, action, actor, from_state, to_state,
                     round_, reason, detail, key):
        at = _now()
        self.db.execute(
            "INSERT INTO events(case_id,seq,action,actor,from_state,to_state,round,reason,detail,idem_key,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (case_id, seq, action, actor, from_state, to_state, round_, reason,
             json.dumps(detail or {}, ensure_ascii=False), key, at),
        )
        return {"seq": seq, "case_id": case_id, "action": action, "actor": actor,
                "from_state": from_state, "to_state": to_state, "round": round_,
                "reason": reason, "detail": detail or {}, "at": at}

    def list_events(self, case_id):
        rows = self.db.execute(
            "SELECT * FROM events WHERE case_id=? ORDER BY seq", (case_id,)).fetchall()
        return [self._event_view(dict(r)) for r in rows]

    @staticmethod
    def _event_view(row):
        return {"seq": row["seq"], "case_id": row["case_id"], "action": row["action"],
                "actor": row["actor"], "from_state": row["from_state"],
                "to_state": row["to_state"], "round": row["round"],
                "reason": row["reason"], "detail": json.loads(row["detail"]),
                "at": row["created_at"]}

    # ---------- 幂等键 ----------
    def find_idempotency(self, key):
        row = self.db.execute("SELECT response FROM idempotency WHERE key=?", (key,)).fetchone()
        return json.loads(row["response"]) if row else None

    def record_idempotency(self, key, case_id, action, response):
        self.db.execute(
            "INSERT INTO idempotency(key,case_id,action,response,created_at) VALUES(?,?,?,?,?)",
            (key, case_id, action, json.dumps(response, ensure_ascii=False), _now()),
        )

    # ---------- 申报关联（回避依据） ----------
    def declare_relationship(self, person, supplier):
        cur = self.db.execute(
            "INSERT OR IGNORE INTO relationships(person,supplier,created_at) VALUES(?,?,?)",
            (person, supplier, _now()),
        )
        return cur.rowcount == 1

    def has_relationship(self, person, supplier):
        row = self.db.execute(
            "SELECT 1 FROM relationships WHERE person=? AND supplier=?",
            (person, supplier)).fetchone()
        return row is not None

    def list_relationships(self):
        rows = self.db.execute(
            "SELECT person,supplier,created_at FROM relationships ORDER BY person,supplier").fetchall()
        return [dict(r) for r in rows]

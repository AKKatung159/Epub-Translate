"""SQLite store: jobs, segments and glossary survive restarts, so a long book resumes where it stopped."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, name TEXT, created REAL, status TEXT, message TEXT DEFAULT '',
  options TEXT DEFAULT '{}', stats TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS segments (
  job_id TEXT, id INTEGER, doc TEXT, path TEXT, kind TEXT, marked TEXT, plain TEXT,
  out TEXT, status TEXT DEFAULT 'pending', note TEXT DEFAULT '', edited INTEGER DEFAULT 0,
  PRIMARY KEY (job_id, id)
);
CREATE INDEX IF NOT EXISTS seg_status ON segments(job_id, status);
CREATE TABLE IF NOT EXISTS glossary (
  job_id TEXT, en TEXT, type TEXT, th TEXT, keep_english INTEGER DEFAULT 0, count INTEGER DEFAULT 0,
  example TEXT DEFAULT '', source TEXT DEFAULT '', PRIMARY KEY (job_id, en)
);
CREATE TABLE IF NOT EXISTS docs (
  job_id TEXT, doc TEXT, ord INTEGER, title TEXT, PRIMARY KEY (job_id, doc)
);
"""

# status flow: parsed -> glossary -> review -> queued -> translating -> done
#              (paused / waiting / cancelled / error at any point)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()

    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def x(self, sql: str, args=()) -> None:
        with self.lock:
            self.db.execute(sql, args)

    def many(self, sql: str, rows) -> None:
        with self.lock:
            self.db.execute("BEGIN")
            try:
                self.db.executemany(sql, rows)
            except Exception:
                self.db.execute("ROLLBACK")      # never leave a transaction open: later writes would all fail
                raise
            self.db.execute("COMMIT")

    # jobs ---------------------------------------------------------------------------------------------
    def new_job(self, name: str, options: dict) -> str:
        jid = uuid.uuid4().hex[:12]
        self.x("INSERT INTO jobs(id,name,created,status,options) VALUES(?,?,?,?,?)",
               (jid, name, time.time(), "parsed", json.dumps(options)))
        return jid

    def job(self, jid: str) -> dict | None:
        r = self.q("SELECT * FROM jobs WHERE id=?", (jid,))
        if not r:
            return None
        d = dict(r[0])
        d["options"] = json.loads(d["options"] or "{}")
        d["stats"] = json.loads(d["stats"] or "{}")
        return d

    def jobs(self) -> list[dict]:
        return [self.job(r["id"]) for r in self.q("SELECT id FROM jobs ORDER BY created DESC")]

    def set_status(self, jid: str, status: str, message: str = "") -> None:
        self.x("UPDATE jobs SET status=?, message=? WHERE id=?", (status, message, jid))

    def set_message(self, jid: str, message: str) -> None:
        self.x("UPDATE jobs SET message=? WHERE id=?", (message, jid))

    def update_stats(self, jid: str, **kw) -> None:
        with self.lock:
            st = self.job(jid)["stats"]
            st.update(kw)
            self.x("UPDATE jobs SET stats=? WHERE id=?", (json.dumps(st), jid))

    def update_options(self, jid: str, **kw) -> None:
        with self.lock:
            op = self.job(jid)["options"]
            op.update(kw)
            self.x("UPDATE jobs SET options=? WHERE id=?", (json.dumps(op), jid))

    def delete_job(self, jid: str) -> None:
        for t in ("segments", "glossary", "docs"):
            self.x(f"DELETE FROM {t} WHERE job_id=?", (jid,))
        self.x("DELETE FROM jobs WHERE id=?", (jid,))

    def counts(self, jid: str) -> dict:
        rows = self.q("SELECT status, COUNT(*) n, SUM(LENGTH(plain)) c FROM segments WHERE job_id=? GROUP BY status", (jid,))
        out = {r["status"]: {"n": r["n"], "chars": r["c"] or 0} for r in rows}
        return out


store = Store(settings.data_dir / "app.db")


def job_dir(jid: str) -> Path:
    d = settings.data_dir / "jobs" / jid
    d.mkdir(parents=True, exist_ok=True)
    return d

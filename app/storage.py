"""SQLite persistence for sessions, search runs, sources, reports, follow-ups and settings."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    question TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '{}',
    framing TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_run_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    strategy TEXT NOT NULL DEFAULT '{}',
    stages TEXT NOT NULL DEFAULT '[]',
    provider_log TEXT NOT NULL DEFAULT '[]',
    counts TEXT NOT NULL DEFAULT '{}',
    limitations TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    run_id INTEGER,
    status TEXT NOT NULL,
    reason TEXT,
    duplicate_of TEXT,
    is_new INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL,
    abstract TEXT,
    doi TEXT,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_session ON sources(session_id);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    run_id INTEGER,
    created_at TEXT NOT NULL,
    confidence TEXT,
    synthesis TEXT,
    markdown TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS followups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    cited TEXT NOT NULL DEFAULT '[]',
    not_covered TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Storage:
    def __init__(self, path: Path | str | None = None):
        self.path = str(path or config.db_path())
        self._lock = threading.RLock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------ settings
    def get_settings(self) -> dict[str, Any]:
        out = dict(config.DEFAULT_SETTINGS)
        with self._conn() as c:
            for row in c.execute("SELECT key, value FROM settings"):
                out[row["key"]] = json.loads(row["value"])
        return out

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        with self._conn() as c:
            for k, v in values.items():
                if k in config.DEFAULT_SETTINGS and v is not None:
                    c.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                              "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, json.dumps(v)))
        return self.get_settings()

    # ------------------------------------------------------------ sessions
    def create_session(self, question: str, title: str | None, params: dict) -> dict:
        sid = uuid.uuid4().hex[:12]
        ts = now_iso()
        title = title or (question[:80] + ("…" if len(question) > 80 else ""))
        with self._conn() as c:
            c.execute("INSERT INTO sessions(id, title, question, params, status, created_at, updated_at) "
                      "VALUES(?,?,?,?,?,?,?)", (sid, title, question, json.dumps(params), "draft", ts, ts))
        return self.get_session(sid)

    def get_session(self, sid: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
            if not row:
                return None
            s = dict(row)
            s["params"] = json.loads(s["params"] or "{}")
            s["framing"] = json.loads(s["framing"]) if s["framing"] else None
            counts = c.execute("SELECT status, COUNT(*) n FROM sources WHERE session_id = ? GROUP BY status",
                               (sid,)).fetchall()
            s["source_counts"] = {r["status"]: r["n"] for r in counts}
            rep = c.execute("SELECT id, confidence, created_at FROM reports WHERE session_id = ? "
                            "ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
            s["latest_report"] = dict(rep) if rep else None
        return s

    def list_sessions(self, limit: int = 100) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT s.id, s.title, s.question, s.status, s.created_at, s.updated_at, s.last_run_at, "
                "(SELECT COUNT(*) FROM sources x WHERE x.session_id = s.id AND x.status = 'included') AS included, "
                "(SELECT confidence FROM reports r WHERE r.session_id = s.id ORDER BY r.id DESC LIMIT 1) AS confidence, "
                "(SELECT COUNT(*) FROM reports r WHERE r.session_id = s.id) AS reports "
                "FROM sessions s ORDER BY s.updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def update_session(self, sid: str, **fields) -> None:
        if not fields:
            return
        for k in ("params", "framing"):
            if k in fields and not isinstance(fields[k], str) and fields[k] is not None:
                fields[k] = json.dumps(fields[k])
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE sessions SET {cols} WHERE id = ?", (*fields.values(), sid))

    def delete_session(self, sid: str) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM sessions WHERE id = ?", (sid,)).rowcount > 0

    # ------------------------------------------------------------ runs
    def create_run(self, sid: str, kind: str, strategy: dict, stages: list[dict]) -> int:
        with self._conn() as c:
            cur = c.execute("INSERT INTO runs(session_id, kind, status, started_at, strategy, stages) "
                            "VALUES(?,?,?,?,?,?)", (sid, kind, "running", now_iso(), json.dumps(strategy),
                                                    json.dumps(stages)))
            return int(cur.lastrowid)

    def update_run(self, run_id: int, **fields) -> None:
        for k in ("strategy", "stages", "provider_log", "counts", "limitations"):
            if k in fields:
                fields[k] = json.dumps(fields[k])
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE runs SET {cols} WHERE id = ?", (*fields.values(), run_id))

    def list_runs(self, sid: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM runs WHERE session_id = ? ORDER BY id", (sid,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("strategy", "stages", "provider_log", "counts", "limitations"):
                d[k] = json.loads(d[k] or ("{}" if k in ("strategy", "counts") else "[]"))
            out.append(d)
        return out

    # ------------------------------------------------------------ sources
    def next_label(self, sid: str) -> str:
        with self._conn() as c:
            n = c.execute("SELECT COALESCE(MAX(CAST(SUBSTR(label, 2) AS INTEGER)), 0) FROM sources WHERE session_id = ?",
                          (sid,)).fetchone()[0]
        return f"S{n + 1}"

    def add_source(self, sid: str, record: dict, status: str, run_id: int | None = None,
                   reason: str | None = None, duplicate_of: str | None = None, is_new: bool = False) -> dict:
        with self._conn() as c:
            n = c.execute("SELECT COALESCE(MAX(CAST(SUBSTR(label, 2) AS INTEGER)), 0) FROM sources WHERE session_id = ?",
                          (sid,)).fetchone()[0]
            src_id = uuid.uuid4().hex[:12]
            label = f"S{n + 1}"
            record = {**record, "id": src_id, "label": label}
            c.execute("INSERT INTO sources(id, session_id, label, run_id, status, reason, duplicate_of, is_new, "
                      "title, abstract, doi, data, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (src_id, sid, label, run_id, status, reason, duplicate_of, int(is_new),
                       record.get("title") or "(untitled)", record.get("abstract"), record.get("doi"),
                       json.dumps(record), now_iso()))
        return self.get_source(src_id)

    def update_source(self, src_id: str, data: dict | None = None, **fields) -> None:
        if data is not None:
            fields["data"] = json.dumps(data)
            fields.setdefault("title", data.get("title") or "(untitled)")
            fields.setdefault("abstract", data.get("abstract"))
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE sources SET {cols} WHERE id = ?", (*fields.values(), src_id))

    @staticmethod
    def _source_row(r: sqlite3.Row) -> dict:
        d = json.loads(r["data"])
        d.update(id=r["id"], label=r["label"], status=r["status"], reason=r["reason"],
                 duplicate_of=r["duplicate_of"], is_new=bool(r["is_new"]), run_id=r["run_id"],
                 added_at=r["created_at"], session_id=r["session_id"])
        return d

    def get_source(self, src_id: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM sources WHERE id = ?", (src_id,)).fetchone()
        return self._source_row(r) if r else None

    def list_sources(self, sid: str, status: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM sources WHERE session_id = ?", [sid]
        if status:
            q += " AND status = ?"
            args.append(status)
        with self._conn() as c:
            rows = c.execute(q + " ORDER BY CAST(SUBSTR(label, 2) AS INTEGER)", args).fetchall()
        return [self._source_row(r) for r in rows]

    def clear_new_flags(self, sid: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE sources SET is_new = 0 WHERE session_id = ?", (sid,))

    # ------------------------------------------------------------ reports
    def add_report(self, sid: str, run_id: int | None, markdown: str, synthesis: dict | None,
                   confidence: str | None) -> int:
        with self._conn() as c:
            cur = c.execute("INSERT INTO reports(session_id, run_id, created_at, confidence, synthesis, markdown) "
                            "VALUES(?,?,?,?,?,?)", (sid, run_id, now_iso(), confidence,
                                                    json.dumps(synthesis) if synthesis else None, markdown))
            return int(cur.lastrowid)

    def get_report(self, sid: str, report_id: int | None = None) -> dict | None:
        with self._conn() as c:
            if report_id:
                r = c.execute("SELECT * FROM reports WHERE session_id = ? AND id = ?", (sid, report_id)).fetchone()
            else:
                r = c.execute("SELECT * FROM reports WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                              (sid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["synthesis"] = json.loads(d["synthesis"]) if d["synthesis"] else None
        return d

    def list_reports(self, sid: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT id, run_id, created_at, confidence FROM reports WHERE session_id = ? "
                             "ORDER BY id DESC", (sid,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ follow-ups
    def add_followup(self, sid: str, question: str, answer: str, cited: list[str], not_covered: str | None) -> dict:
        with self._conn() as c:
            cur = c.execute("INSERT INTO followups(session_id, question, answer, cited, not_covered, created_at) "
                            "VALUES(?,?,?,?,?,?)", (sid, question, answer, json.dumps(cited), not_covered, now_iso()))
            fid = cur.lastrowid
        return next(f for f in self.list_followups(sid) if f["id"] == fid)

    def list_followups(self, sid: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM followups WHERE session_id = ? ORDER BY id", (sid,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["cited"] = json.loads(d["cited"])
            out.append(d)
        return out

    # ------------------------------------------------------------ search
    def search(self, text: str, limit: int = 50) -> dict[str, list[dict]]:
        like = f"%{text.strip()}%"
        with self._conn() as c:
            sessions = c.execute("SELECT id, title, question, updated_at, last_run_at FROM sessions "
                                 "WHERE title LIKE ? OR question LIKE ? ORDER BY updated_at DESC LIMIT ?",
                                 (like, like, limit)).fetchall()
            sources = c.execute("SELECT s.id, s.session_id, s.label, s.title, s.doi, s.status, x.title AS session_title, "
                                "s.created_at FROM sources s JOIN sessions x ON x.id = s.session_id "
                                "WHERE s.title LIKE ? OR s.abstract LIKE ? OR s.doi LIKE ? "
                                "ORDER BY s.created_at DESC LIMIT ?", (like, like, like, limit)).fetchall()
            reports = c.execute("SELECT r.id, r.session_id, r.created_at, r.confidence, x.title AS session_title "
                                "FROM reports r JOIN sessions x ON x.id = r.session_id WHERE r.markdown LIKE ? "
                                "ORDER BY r.id DESC LIMIT ?", (like, limit)).fetchall()
        return {"sessions": [dict(r) for r in sessions], "sources": [dict(r) for r in sources],
                "reports": [dict(r) for r in reports]}

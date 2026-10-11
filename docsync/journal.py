"""Journal of runs and publications (SQLite). Needed for idempotency, statuses and manual-edit detection."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

DDL = """
CREATE TABLE IF NOT EXISTS run (
  run_id        TEXT PRIMARY KEY,
  run_key       TEXT UNIQUE,
  project_path  TEXT NOT NULL,
  service_id    TEXT NOT NULL,
  merge_sha     TEXT NOT NULL,
  source_mr_iid INTEGER,
  source_mr_url TEXT,
  status        TEXT NOT NULL,
  skip_reason   TEXT,
  branch        TEXT,
  model_mr_iid  INTEGER,
  model_mr_url  TEXT,
  model_merge_sha TEXT,
  changeset     TEXT,
  apply_report  TEXT,
  preview       TEXT,
  error         TEXT,
  received_at   TEXT NOT NULL,
  proposed_at   TEXT,
  approved_at   TEXT,
  published_at  TEXT,
  updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publication (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id        TEXT,
  model_sha     TEXT NOT NULL,
  page_id       TEXT NOT NULL,
  block_id      TEXT NOT NULL,
  status        TEXT NOT NULL,
  hash_after    TEXT,
  version_after INTEGER,
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS publication_block_idx ON publication (page_id, block_id, id DESC);
CREATE TABLE IF NOT EXISTS finding (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id      TEXT,
  page_id     TEXT NOT NULL,
  quote       TEXT NOT NULL,
  reason      TEXT NOT NULL,
  suggestion  TEXT,
  confidence  REAL NOT NULL,
  resolution  TEXT NOT NULL,
  comment_id  TEXT,
  created_at  TEXT NOT NULL
);
"""

STATUSES = ("RECEIVED", "SKIPPED", "PROPOSED", "NEEDS_REVIEW", "APPROVED", "REJECTED", "PUBLISHED", "PARTIALLY_PUBLISHED", "FAILED")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Journal:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(DDL)
    def create_run(self, cs: dict, run_key: str) -> dict | None:
        """Creates a run; returns None on a repeated run_key (idempotency)."""
        run_id = str(uuid.uuid4())
        mr = cs.get("mr") or {}
        try:
            self.db.execute(
                "INSERT INTO run (run_id, run_key, project_path, service_id, merge_sha, source_mr_iid, source_mr_url, status, "
                "skip_reason, changeset, received_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, run_key, cs["project"]["path"], cs["service"], cs["mergeSha"], mr.get("iid"), mr.get("url"),
                 "SKIPPED" if cs.get("skipReason") else "RECEIVED", cs.get("skipReason"), json.dumps(cs, ensure_ascii=False), now(), now()))
            self.db.commit()
        except sqlite3.IntegrityError:
            return None
        return self.get_run(run_id)

    def update_run(self, run_id: str, **fields) -> dict:
        for k in ("changeset", "apply_report", "preview"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        fields["updated_at"] = now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE run SET {sets} WHERE run_id = ?", (*fields.values(), run_id))
        self.db.commit()
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM run WHERE run_id = ? OR run_key = ?", (run_id, run_id)).fetchone()
        return self._run(row) if row else None

    def run_by_branch(self, branch: str) -> dict | None:
        row = self.db.execute("SELECT * FROM run WHERE branch = ? ORDER BY received_at DESC", (branch,)).fetchone()
        return self._run(row) if row else None

    def list_runs(self, status: str | None = None, limit: int = 50) -> list[dict]:
        q, args = "SELECT * FROM run", []
        if status:
            q, args = q + " WHERE status = ?", [status]
        rows = self.db.execute(q + " ORDER BY received_at DESC, rowid DESC LIMIT ?", (*args, limit)).fetchall()
        return [self._run(r, brief=True) for r in rows]

    @staticmethod
    def _run(row: sqlite3.Row, brief: bool = False) -> dict:
        d = dict(row)
        for k in ("changeset", "apply_report", "preview"):
            if brief:
                d.pop(k, None)
            elif d.get(k):
                d[k] = json.loads(d[k])
        return d
    def last_hashes(self, page_id: str) -> dict[str, str]:
        # the last PUBLISHED or UNCHANGED record per block: both confirm what the block looked like
        # the last time DocSync saw it (UNCHANGED matters after a `force` publish or a normalization change)
        rows = self.db.execute(
            "SELECT block_id, hash_after FROM publication WHERE page_id = ? AND status IN ('PUBLISHED', 'UNCHANGED') "
            "AND id IN (SELECT max(id) FROM publication WHERE page_id = ? AND status IN ('PUBLISHED', 'UNCHANGED') GROUP BY block_id)",
            (page_id, page_id)).fetchall()
        return {r["block_id"]: r["hash_after"] for r in rows}

    def record_publication(self, run_id: str | None, model_sha: str, page_id: str, results: list[dict], version_after: int | None) -> None:
        for r in results:
            self.db.execute("INSERT INTO publication (run_id, model_sha, page_id, block_id, status, hash_after, version_after, created_at) "
                            "VALUES (?,?,?,?,?,?,?,?)",
                            (run_id, model_sha, page_id, r["blockId"], r["status"], r.get("hashAfter"), version_after, now()))
        self.db.commit()

    def publications(self, limit: int = 100) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM publication ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
    def record_finding(self, run_id: str | None, f: dict, resolution: str) -> int:
        cur = self.db.execute("INSERT INTO finding (run_id, page_id, quote, reason, suggestion, confidence, resolution, created_at) "
                              "VALUES (?,?,?,?,?,?,?,?)",
                              (run_id, f["pageId"], f["quote"], f["reason"], f.get("suggestion"), f["confidence"], resolution, now()))
        self.db.commit()
        return cur.lastrowid

    def set_finding_comment(self, finding_id: int, comment_id: str) -> None:
        self.db.execute("UPDATE finding SET comment_id = ? WHERE id = ?", (comment_id, finding_id))
        self.db.commit()

    def finding_exists(self, page_id: str, quote: str) -> bool:
        return self.db.execute("SELECT 1 FROM finding WHERE page_id = ? AND quote = ?", (page_id, quote)).fetchone() is not None

    def kpi(self) -> dict:
        rows = self.db.execute("SELECT status, count(*) AS n FROM run GROUP BY status").fetchall()
        lag = self.db.execute("SELECT avg((julianday(published_at) - julianday(received_at)) * 24) AS h FROM run WHERE published_at IS NOT NULL").fetchone()
        return {"runsByStatus": {r["status"]: r["n"] for r in rows}, "avgDocLagHours": lag["h"]}

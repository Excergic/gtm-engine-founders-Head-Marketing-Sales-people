"""Operational state only: never stores prospect payloads or a local CRM copy."""

from __future__ import annotations

import fcntl
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from .models import utcnow


class State:
    def __init__(self, path: Path, *, read_only=False):
        self.path = path
        self.read_only = read_only
        if read_only and path.exists():
            self.db = sqlite3.connect(
                f"file:{quote(str(path.resolve()))}?mode=ro", uri=True, timeout=30
            )
            self.db.row_factory = sqlite3.Row
            return
        if not read_only:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(":memory:" if read_only else path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS files (
              id TEXT PRIMARY KEY, source TEXT NOT NULL, status TEXT NOT NULL,
              first_seen TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS rows (
              file_id TEXT NOT NULL, row_number INTEGER NOT NULL, status TEXT NOT NULL,
              record_id TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 1,
              updated_at TEXT NOT NULL, PRIMARY KEY(file_id,row_number));
            CREATE TABLE IF NOT EXISTS audit (
              id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, operation TEXT NOT NULL,
              object TEXT NOT NULL, record_id TEXT, details TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS import_runs (
              id TEXT PRIMARY KEY, file_id TEXT, source_path TEXT NOT NULL,
              started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
              summary TEXT, error TEXT);
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(files)")}
        for column in ("path", "snapshot_path"):
            if column not in columns:
                self.db.execute(f"ALTER TABLE files ADD COLUMN {column} TEXT")
        self.db.commit()
        if not read_only:
            path.chmod(0o600)

    def close(self):
        self.db.close()

    def run_started(self, key, path):
        run_id = str(uuid4())
        with self.db:
            # Holding the writer lock proves an earlier unfinished run has stopped.
            self.db.execute(
                "UPDATE import_runs SET status='interrupted',finished_at=? WHERE status='running'",
                (utcnow().isoformat(),),
            )
            self.db.execute(
                "INSERT INTO import_runs(id,file_id,source_path,started_at,status) "
                "VALUES (?,?,?,?,?)",
                (run_id, key, str(path), utcnow().isoformat(), "running"),
            )
        return run_id

    def run_finished(self, run_id, status, summary=None, error=None):
        with self.db:
            self.db.execute(
                "UPDATE import_runs SET status=?,finished_at=?,summary=?,error=? WHERE id=?",
                (status, utcnow().isoformat(), json.dumps(summary or {}), error, run_id),
            )

    def history(self, limit=50):
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='import_runs'").fetchone():
            return []
        return [
            {**dict(row), "summary": json.loads(row["summary"] or "{}")}
            for row in self.db.execute(
                "SELECT * FROM import_runs ORDER BY started_at DESC LIMIT ?", (limit,)
            )
        ]

    def problems(self, statuses=("review",)):
        marks = ",".join("?" for _ in statuses)
        return [
            dict(row)
            for row in self.db.execute(
                f"SELECT rows.*,files.path AS source_path FROM rows JOIN files ON rows.file_id=files.id "
                f"WHERE rows.status IN ({marks}) ORDER BY rows.updated_at DESC",
                statuses,
            )
        ]

    def pending_files(self):
        columns = {r["name"] for r in self.db.execute("PRAGMA table_info(files)")}
        snapshot = "snapshot_path" if "snapshot_path" in columns else "NULL AS snapshot_path"
        recover_complete = (
            " OR (status='complete' AND (snapshot_path LIKE '%/processing/%' "
            "OR snapshot_path LIKE '%/failed/%'))"
            if "snapshot_path" in columns
            else ""
        )
        return [
            dict(row)
            for row in self.db.execute(
                f"SELECT id,path,{snapshot},status FROM files WHERE source='grok' "
                f"AND (status NOT IN ('complete','superseded'){recover_complete})"
            )
        ]

    def snapshot(self, key, path):
        with self.db:
            self.db.execute("UPDATE files SET snapshot_path=? WHERE id=?", (str(path), key))

    @contextmanager
    def lock(self):
        """Serialize import, CSV edits, and checkpoints across local processes."""
        with self.path.with_suffix(".lock").open("a") as file:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    "another GTM writer is running; retry after it finishes"
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(file, fcntl.LOCK_UN)

    def file(self, key, source, path=None):
        now = utcnow().isoformat()
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO files(id,source,status,first_seen,updated_at,path) VALUES (?,?,?,?,?,?)",
                (key, source, "processing", now, now, str(path) if path else None),
            )
            if path:
                self.db.execute("UPDATE files SET path=? WHERE id=?", (str(path), key))
        return dict(self.db.execute("SELECT * FROM files WHERE id=?", (key,)).fetchone())

    def row(self, key, number):
        row = self.db.execute(
            "SELECT * FROM rows WHERE file_id=? AND row_number=?", (key, number)
        ).fetchone()
        return dict(row) if row else None

    def result(self, key, number, status, record_id=None, error=None):
        with self.db:
            self.db.execute(
                """INSERT INTO rows VALUES (?,?,?,?,?,1,?)
                ON CONFLICT(file_id,row_number) DO UPDATE SET
                  status=excluded.status, record_id=excluded.record_id, error=excluded.error,
                  attempts=rows.attempts+1, updated_at=excluded.updated_at""",
                (key, number, status, record_id, error, utcnow().isoformat()),
            )

    def finish(self, key, status):
        with self.db:
            self.db.execute(
                "UPDATE files SET status=?,updated_at=? WHERE id=?",
                (status, utcnow().isoformat(), key),
            )

    def audit(self, operation, obj, record_id, **details):
        with self.db:
            self.db.execute(
                "INSERT INTO audit(timestamp,operation,object,record_id,details) "
                "VALUES (?,?,?,?,?)",
                (utcnow().isoformat(), operation, obj, record_id, json.dumps(details)),
            )

    def health(self):
        return {
            "sqlite": self.db.execute("PRAGMA quick_check").fetchone()[0],
            "files": [
                dict(r)
                for r in self.db.execute(
                    "SELECT source,status,count(*) AS count FROM files GROUP BY source,status"
                )
            ],
            "rows": [
                dict(r)
                for r in self.db.execute(
                    "SELECT status,count(*) AS count FROM rows GROUP BY status"
                )
            ],
            "recent_files": [
                dict(r)
                for r in self.db.execute(
                    "SELECT id,source,path,status,updated_at FROM files ORDER BY updated_at DESC LIMIT 20"
                )
            ],
            "failures": [
                dict(r)
                for r in self.db.execute(
                    "SELECT file_id,row_number,status,error FROM rows "
                    "WHERE status IN ('failed','review','invalid') ORDER BY updated_at DESC LIMIT 20"
                )
            ],
        }

"""Raw zone loader: every Grok/LinkedIn input file in ``data/incoming/`` is archived
unchanged in ``data/raw/YYYY-MM-DD/`` and stored row by row in ``raw.files`` /
``raw.grok_rows`` (the 13 Grok columns as text). The warehouse is the only record;
input files are never edited.

Idempotent: a file already loaded (same SHA-256) is skipped.
"""

from __future__ import annotations

import csv
import hashlib
import shutil
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from .ingestion import parse_csv
from .models import Source

# A file modified this recently is treated as still being written.
SETTLE_SECONDS = 60

GROK_COLUMNS = [
    "name", "title", "company", "company_website", "country", "city", "linkedin_url",
    "x_handle", "source_url", "signal", "signal_date", "why_fit", "outreach_angle",
]  # fmt: skip


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def blank(value):
    value = (value or "").strip()
    return value or None


def _num(value):
    value = blank(value)
    return float(value) if value is not None else None


def _row_key(values: dict) -> tuple:
    return tuple((values.get(c) or "").strip() for c in GROK_COLUMNS)


class Loader:
    def __init__(self, conn: psycopg.Connection, data_dir: Path, rules: dict, today=None):
        # Autocommit so each file and each person is its own committed transaction: a
        # failure keeps earlier work and a rerun resumes where it stopped.
        if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            raise RuntimeError("commit or roll back the open transaction before loading")
        conn.autocommit = True
        self.conn = conn
        self.data_dir = data_dir
        self.rules = rules
        self.today = today or datetime.now(UTC).date().isoformat()
        self.counts: Counter = Counter()
        self.problems: list[str] = []
        self.raw_by_content: dict[tuple, int] = {}
        self.run_id = None

    # -- run bookkeeping -----------------------------------------------------------

    def start(self, job: str = "migration"):
        self.run_id = self.conn.execute(
            "insert into ops.pipeline_runs (job, details) values (%s, %s) returning run_id",
            (job, Jsonb({"loader": "warehouse_load", "raw_date": self.today})),
        ).fetchone()[0]
        self.conn.commit()

    def finish(self, status: str, error: str | None = None):
        self.conn.execute(
            "update ops.pipeline_runs set finished_at = now(), status = %s, rows_in = %s,"
            " rows_ok = %s, rows_failed = %s, error_summary = %s, details = details || %s"
            " where run_id = %s",
            (
                status,
                self.counts["people_in"],
                self.counts["people_registered"] + self.counts["people_already_present"],
                self.counts["people_failed"],
                error,
                Jsonb({"counts": dict(self.counts), "problems": self.problems[:50]}),
                self.run_id,
            ),
        )
        self.conn.commit()

    # -- raw zone ------------------------------------------------------------------

    def load_raw(self):
        aliases = {}
        for canonical, names in self.rules["mappings"]["grok"].items():
            grok_name = names[0]
            for name in [grok_name, *names, canonical]:
                aliases[name.strip().casefold()] = grok_name
        inbox = self.data_dir / "incoming"
        now = time.time()
        for path in sorted(inbox.rglob("*.csv")):
            if now - path.stat().st_mtime < SETTLE_SECONDS:
                # Probably still being written; the next run picks it up.
                self.counts["raw_files_skipped_still_writing"] += 1
                continue
            source = "linkedin" if "linkedin" in path.relative_to(inbox).parts else "grok"
            parent = path.parent.name
            segment = parent if parent in {"founders", "VPs"} else None
            try:
                self._load_file(path, source, segment, aliases, inbox)
            except (OSError, ValueError, csv.Error, psycopg.Error) as exc:
                # One bad file never stops the others; it is retried on the next run.
                self.counts["raw_files_failed"] += 1
                self.problems.append(f"{path.name}: {type(exc).__name__}: {str(exc)[:200]}")

    def _load_file(self, path, source, segment, aliases, inbox):
        digest = sha256(path)
        existing = self.conn.execute(
            "select file_id from raw.files where sha256 = %s and source = %s"
            " and segment is not distinct from %s",
            (digest, source, segment),
        ).fetchone()
        if existing:
            self.counts["raw_files_already_present"] += 1
            self._index_existing(existing[0])
            return
        target = self.data_dir / "raw" / self.today / path.relative_to(inbox)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(path, target)
        if sha256(target) != digest:
            raise RuntimeError(f"raw copy differs from original: {path.name}")

        with self.conn.transaction():
            file_id = self.conn.execute(
                "insert into raw.files (source, segment, original_path, sha256, size_bytes,"
                " storage_path, run_id) values (%s, %s, %s, %s, %s, %s, %s) returning file_id",
                (
                    source,
                    segment,
                    str(path.relative_to(self.data_dir.parent)),
                    digest,
                    path.stat().st_size,
                    str(target.relative_to(self.data_dir.parent)),
                    self.run_id,
                ),
            ).fetchone()[0]
            if source == "grok":
                self._load_grok_rows(path, file_id, segment, aliases)
            else:
                self._load_linkedin_rows(path, file_id)
        self.counts[f"raw_files_{source}"] += 1

    def _validation(self, path, source):
        results = {}
        for parsed in parse_csv(path, source, self.rules["mappings"][source]):
            if parsed.prospect is None:
                results[parsed.number] = ("invalid", [parsed.error])
                continue
            p = parsed.prospect
            reasons = []
            if source == Source.grok:
                missing = [f for f in ("signal", "signal_date", "source_url") if not getattr(p, f)]
                if missing:
                    reasons.append("missing " + ", ".join(missing))
                if p.signal_date_precision in {"month", "year"}:
                    reasons.append(f"signal_date has {p.signal_date_precision} precision only")
            if not p.email and not p.linkedin_url:
                reasons.append("missing email and LinkedIn URL")
            results[parsed.number] = ("review" if reasons else "valid", reasons)
        return results

    def _rows(self, path):
        with path.open(encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file, strict=True)
            for number, raw in enumerate(reader, 2):
                if not any(v for v in raw.values()):
                    continue
                yield number, raw

    def _load_grok_rows(self, path, file_id, segment, aliases):
        validation = self._validation(path, Source.grok)
        for number, raw in self._rows(path):
            values, extras = {}, {}
            for header, value in raw.items():
                if header is None:
                    extras["_overflow"] = value
                    continue
                name = aliases.get(header.strip().casefold())
                if name and name not in values:
                    values[name] = value
                else:
                    extras[header] = value
            status, errors = validation.get(number, ("invalid", ["row could not be parsed"]))
            raw_row_id = self.conn.execute(
                "insert into raw.grok_rows (file_id, row_number, segment, "
                + ", ".join(GROK_COLUMNS)
                + ", extra_columns, validation_status, validation_errors) values (%s, %s, %s, "
                + ", ".join(["%s"] * len(GROK_COLUMNS))
                + ", %s, %s, %s) returning raw_row_id",
                (
                    file_id,
                    number,
                    segment,
                    *[values.get(c) for c in GROK_COLUMNS],
                    Jsonb(extras),
                    status,
                    Jsonb(errors),
                ),
            ).fetchone()[0]
            self.raw_by_content.setdefault(_row_key(values), raw_row_id)
            self.counts[f"raw_rows_{status}"] += 1

    def _load_linkedin_rows(self, path, file_id):
        validation = self._validation(path, Source.linkedin)
        for number, raw in self._rows(path):
            status, errors = validation.get(number, ("invalid", ["row could not be parsed"]))
            self.conn.execute(
                "insert into raw.linkedin_rows (file_id, row_number, data, validation_status,"
                " validation_errors) values (%s, %s, %s, %s, %s)",
                (file_id, number, Jsonb(raw), status, Jsonb(errors)),
            )
            self.counts[f"raw_rows_{status}"] += 1

    def _index_existing(self, file_id):
        for row in self.conn.execute(
            "select raw_row_id, " + ", ".join(GROK_COLUMNS) + " from raw.grok_rows"
            " where file_id = %s",
            (file_id,),
        ):
            self.raw_by_content.setdefault(
                _row_key(dict(zip(GROK_COLUMNS, row[1:], strict=True))), row[0]
            )


LAYER_COUNTS = """
select 'raw.files' as table_name, count(*) from raw.files
union all select 'raw.grok_rows', count(*) from raw.grok_rows
union all select 'raw.grok_rows valid', count(*) from raw.grok_rows where validation_status = 'valid'
union all select 'raw.grok_rows review', count(*) from raw.grok_rows where validation_status = 'review'
union all select 'raw.grok_rows invalid', count(*) from raw.grok_rows where validation_status = 'invalid'
union all select 'core.prospect_registry', count(*) from core.prospect_registry
union all select 'staging.prospects', count(*) from staging.prospects
union all select 'silver.prospects', count(*) from silver.prospects
union all select 'golden.prospects', count(*) from golden.prospects
union all select 'core.companies', count(*) from core.companies
union all select 'core.signals', count(*) from core.signals
union all select 'core.score_snapshots', count(*) from core.score_snapshots
union all select 'core.account_research', count(*) from core.account_research
union all select 'ops.state_transitions', count(*) from ops.state_transitions
union all select 'outbound.messages', count(*) from outbound.messages
"""


def layer_counts(conn: psycopg.Connection) -> dict:
    return {name: n for name, n in conn.execute(LAYER_COUNTS)}

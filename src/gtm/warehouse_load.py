"""One-time load of existing local data into the warehouse.

- Every original Grok/LinkedIn CSV in the inbox goes to ``data/raw/YYYY-MM-DD/`` unchanged
  and into ``raw.files`` / ``raw.grok_rows`` (13 Grok columns as text).
- Every person in the master CSV is registered, keeping their ID, in the layer their data
  qualifies for, with their signals, company, score snapshot and research.

Idempotent: files already loaded (same SHA-256) and people already registered are skipped,
so a rerun after a partial failure only adds what is missing.
"""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from .ingestion import parse_csv
from .models import Source, normalize_x_handle

GROK_COLUMNS = [
    "name", "title", "company", "company_website", "country", "city", "linkedin_url",
    "x_handle", "source_url", "signal", "signal_date", "why_fit", "outreach_angle",
]  # fmt: skip

# Master CSV column -> Grok column name used in the warehouse.
MASTER_TO_GROK = {
    "full_name": "name",
    "job_title": "title",
    "company_name": "company",
    "website": "company_website",
    **{c: c for c in GROK_COLUMNS if c not in {"name", "title", "company", "company_website"}},
}

CONNECTION_STATUS = {
    "discovered": "none",
    "connection_sent": "requested",
    "connection_pending": "requested",
    "connected": "connected",
    "conversation_started": "connected",
    "replied": "connected",
    "meeting_booked": "connected",
    "no_response": "requested",
    "closed": "none",
}


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

    def start(self):
        self.run_id = self.conn.execute(
            "insert into ops.pipeline_runs (job, details) values ('migration', %s) returning run_id",
            (Jsonb({"loader": "warehouse_load", "raw_date": self.today}),),
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
        for path in sorted(inbox.rglob("*.csv")):
            source = "linkedin" if "linkedin" in path.relative_to(inbox).parts else "grok"
            parent = path.parent.name
            segment = parent if parent in {"founders", "VPs"} else None
            self._load_file(path, source, segment, aliases, inbox)

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

    # -- people ---------------------------------------------------------------------

    def load_people(self, master: Path):
        with master.open(encoding="utf-8-sig", newline="") as file:
            people = list(csv.DictReader(file))
        research = self._research_by_domain()
        for row in people:
            self.counts["people_in"] += 1
            pid = row["id"]
            if self.conn.execute(
                "select 1 from core.prospect_registry where prospect_id = %s", (pid,)
            ).fetchone():
                self.counts["people_already_present"] += 1
                continue
            try:
                with self.conn.transaction():
                    self._load_person(row, research)
                self.counts["people_registered"] += 1
            except ValueError as exc:
                self.counts["people_failed"] += 1
                self.problems.append(f"{pid}: invalid value ({str(exc)[:80]})")
            except psycopg.Error as exc:
                self.counts["people_failed"] += 1
                # Keep the database reason, never the person's values.
                diag = exc.diag
                self.problems.append(
                    f"{pid}: {type(exc).__name__} {diag.constraint_name or ''} "
                    f"{(diag.message_primary or '')[:120]}"
                )

    def _company(self, row) -> str | None:
        domain = blank(row.get("domain"))
        if not domain:
            return None
        found = self.conn.execute(
            "select company_id from core.companies where domain = %s", (domain.lower(),)
        ).fetchone()
        if found:
            return found[0]
        self.counts["companies"] += 1
        return self.conn.execute(
            "insert into core.companies (name, domain, website, original_source)"
            " values (%s, %s, %s, 'grok') returning company_id",
            (blank(row.get("company_name")) or domain, domain.lower(), blank(row.get("website"))),
        ).fetchone()[0]

    def _load_person(self, row, research):
        pid = row["id"]
        company_id = self._company(row)
        prospect = {grok: blank(row.get(master)) for master, grok in MASTER_TO_GROK.items()}
        prospect = {k: v for k, v in prospect.items() if v is not None}
        if "x_handle" in prospect:  # older master rows predate handle normalization
            prospect["x_handle"] = normalize_x_handle(prospect["x_handle"])
        segments = [s for s in (row.get("segments") or "").split("|") if s]
        segments = [s.strip() for part in segments for s in part.split(",") if s.strip()]
        discovered = blank(row.get("discovered_at"))
        prospect.update(
            prospect_id=pid,
            company_id=str(company_id) if company_id else None,
            segment=sorted(set(segments)),
            email=(blank(row.get("email")) or "").lower() or None,
            email_status="unknown",
            domain=(blank(row.get("domain")) or "").lower() or None,
            connection_status=CONNECTION_STATUS.get(row.get("connection_status"), "none"),
            tier=blank(row.get("tier")),
            score_model_version=blank(row.get("score_model_version")),
            consulting_stage=blank(row.get("consulting_stage")),
            job_stage=blank(row.get("job_stage")),
            do_not_contact=(row.get("do_not_contact") or "").lower() == "true",
            notes=blank(row.get("notes")),
            original_source=blank(row.get("original_source")),
            created_at=discovered,
        )
        prospect = {k: v for k, v in prospect.items() if v is not None}

        history = json.loads(row.get("source_data") or "[]")
        raw_ids = [self.raw_by_content.get(_row_key(entry.get("data", {}))) for entry in history]
        first_raw = next((r for r in raw_ids if r), None)
        self.counts["people_linked_to_raw_row" if first_raw else "people_without_raw_row"] += 1

        self.conn.execute(
            "select ops.register_prospect(%s, %s, 'system', 'migrated from prospects.csv', %s)",
            (Jsonb(prospect), self.run_id, first_raw),
        )

        # Signals: the current one, plus any different ones in the source history.
        seen = set()
        candidates = [(prospect, first_raw, discovered)] + [
            (entry.get("data", {}), raw_id, entry.get("discovered_at"))
            for entry, raw_id in zip(history, raw_ids, strict=True)
        ]
        for data, raw_id, seen_at in candidates:
            signal = blank(data.get("signal"))
            key = (signal, blank(data.get("signal_date")), blank(data.get("source_url")))
            if not signal or key in seen:
                continue
            seen.add(key)
            self.conn.execute(
                "insert into core.signals (prospect_id, company_id, raw_row_id, signal,"
                " signal_date, source_url, why_fit, outreach_angle, source, first_seen_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, 'grok', coalesce(%s, now()))",
                (
                    pid,
                    company_id,
                    raw_id,
                    signal,
                    blank(data.get("signal_date")),
                    blank(data.get("source_url")),
                    blank(data.get("why_fit")),
                    blank(data.get("outreach_angle")),
                    seen_at,
                ),
            )
            self.counts["signals"] += 1

        if blank(row.get("tier")) or row.get("score_status") not in (None, "", "needs_research"):
            self.conn.execute(
                "insert into core.score_snapshots (prospect_id, snapshot_date, account_fit,"
                " lead_fit, intent, engagement, total, tier, model_version, reasons)"
                " values (%s, coalesce(%s::timestamptz::date, current_date), %s, %s, %s, %s,"
                " %s, %s, %s, %s)",
                (
                    pid,
                    blank(row.get("research_updated_at")),
                    _num(row.get("account_fit_score")),
                    _num(row.get("lead_fit_score")),
                    _num(row.get("hiring_intent_score")),
                    _num(row.get("engagement_score")),
                    _num(row.get("full_time_priority")),
                    blank(row.get("tier")),
                    blank(row.get("score_model_version")) or "unknown",
                    Jsonb(
                        {
                            "score_status": row.get("score_status"),
                            "reasons": blank(row.get("score_reasons")),
                            "evidence_coverage": row.get("evidence_coverage"),
                        }
                    ),
                ),
            )
            self.counts["score_snapshots"] += 1

        domain = (blank(row.get("domain")) or "").lower()
        if blank(row.get("research_updated_at")) and domain in research and company_id:
            if not self.conn.execute(
                "select 1 from core.account_research where company_id = %s", (company_id,)
            ).fetchone():
                self._account_research(company_id, research[domain])
            self.conn.execute(
                "select ops.transition(%s, 'researched', %s, 'system', %s)",
                (pid, self.run_id, "account research receipt migrated"),
            )
            self.counts["people_researched"] += 1

    def _research_by_domain(self) -> dict:
        receipts = {}
        for path in sorted((self.data_dir / "research" / "accounts").glob("*.json")):
            receipt = json.loads(path.read_text())
            receipts[(receipt.get("domain") or path.stem).lower()] = receipt
        return receipts

    def _account_research(self, company_id, receipt):
        sources = sorted(
            {e["url"] for e in receipt.get("evidence", []) if isinstance(e, dict) and e.get("url")}
        )
        self.conn.execute(
            "insert into core.account_research (company_id, researched_at, findings, sources,"
            " status, profile_hash) values (%s, %s, %s, %s, %s, %s)",
            (
                company_id,
                receipt.get("researched_at"),
                Jsonb(receipt),
                sources,
                receipt.get("status") or "unknown",
                receipt.get("profile_hash"),
            ),
        )
        self.counts["account_research"] += 1


def load_existing(conn: psycopg.Connection, data_dir: Path, master: Path, rules: dict) -> dict:
    loader = Loader(conn, data_dir, rules)
    loader.start()
    try:
        loader.load_raw()
        loader.load_people(master)
    except Exception as exc:
        loader.finish("failed", f"{type(exc).__name__}: {str(exc)[:200]}")
        raise
    loader.finish("partial" if loader.counts["people_failed"] else "succeeded")
    return {
        "run_id": str(loader.run_id),
        "counts": dict(loader.counts),
        "problems": loader.problems,
    }


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

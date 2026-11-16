"""Grok import into the warehouse: inbox -> raw -> people, with a recorded outcome per row.

Identity rules (unchanged from the CSV engine):
- exact normalized LinkedIn URL or email -> same person: add the signal, fill blanks only
  (manual edits win), add the segment;
- same name + company but no exact key -> held for human review, never auto-merged;
- otherwise -> a new person, placed in the layer their data qualifies for.
Every raw row ends with exactly one outcome in ``ops.raw_row_outcomes``.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from .ingestion import parse_csv
from .models import Prospect, Source, normalize_domain
from .warehouse_load import GROK_COLUMNS, Loader, _row_key

SIGNAL_COLUMNS = ("signal", "signal_date", "source_url", "why_fit", "outreach_angle")


def grok_values(p: Prospect) -> dict:
    """Validated Prospect -> warehouse column names (the Grok contract)."""
    values = {
        "name": p.full_name,
        "title": p.job_title,
        "company": p.company_name,
        "company_website": p.website,
        "country": p.country,
        "city": p.city,
        "linkedin_url": p.linkedin_url,
        "x_handle": p.x_handle,
        "source_url": p.source_url,
        "signal": p.signal,
        "signal_date": p.signal_date,
        "why_fit": p.why_fit,
        "outreach_angle": p.outreach_angle,
        "email": p.email,
    }
    return {k: v for k, v in values.items() if v is not None}


def _key(text) -> str:
    return " ".join((text or "").casefold().split())


class WarehouseImporter:
    def __init__(self, conn: psycopg.Connection, data_dir: Path, rules: dict):
        self.loader = Loader(conn, data_dir, rules)  # sets autocommit
        self.conn = conn
        self.data_dir = data_dir
        self.rules = rules
        self.counts: Counter = Counter()
        self.problems: list[str] = []

    @property
    def run_id(self):
        return self.loader.run_id

    # -- run -------------------------------------------------------------------------

    def run(self) -> dict:
        self.loader.start("import")
        try:
            self.loader.load_raw()
            self.process_pending()
        except Exception as exc:
            self._finish("failed", f"{type(exc).__name__}: {str(exc)[:200]}")
            raise
        self._finish("partial" if self.counts["failed"] else "succeeded")
        raw = {k: v for k, v in self.loader.counts.items() if k.startswith("raw_")}
        return {
            "run_id": str(self.run_id),
            "raw": raw,
            "outcomes": dict(self.counts),
            "problems": self.problems,
        }

    def _finish(self, status, error=None):
        total = sum(v for k, v in self.counts.items() if k != "failed")
        self.conn.execute(
            "update ops.pipeline_runs set finished_at = now(), status = %s, rows_in = %s,"
            " rows_ok = %s, rows_failed = %s, error_summary = %s, details = details || %s"
            " where run_id = %s",
            (
                status,
                total + self.counts["failed"],
                total,
                self.counts["failed"],
                error,
                Jsonb({"raw": dict(self.loader.counts), "outcomes": dict(self.counts)}),
                self.run_id,
            ),
        )

    # -- processing ----------------------------------------------------------------------

    def pending_rows(self):
        return self.conn.execute(
            "select g.raw_row_id, g.row_number, g.segment, g.validation_status,"
            " g.validation_errors, f.storage_path, "
            + ", ".join(f"g.{c}" for c in GROK_COLUMNS)
            + " from raw.grok_rows g join raw.files f using (file_id)"
            " where not exists (select 1 from ops.raw_row_outcomes o"
            "                   where o.raw_row_id = g.raw_row_id)"
            " order by g.raw_row_id"
        ).fetchall()

    def process_pending(self):
        parsed_files: dict[str, dict] = {}
        for row in self.pending_rows():
            raw_row_id, number, segment, status, errors, storage = row[:6]
            content = _row_key(dict(zip(GROK_COLUMNS, row[6:], strict=True)))
            if storage not in parsed_files:
                path = self.data_dir.parent / storage
                parsed_files[storage] = {
                    r.number: r.prospect
                    for r in parse_csv(path, Source.grok, self.rules["mappings"]["grok"])
                }
            prospect = parsed_files[storage].get(number)
            try:
                with self.conn.transaction():
                    outcome = self._process(raw_row_id, segment, status, errors, prospect, content)
                self.counts[outcome] += 1
            except psycopg.Error as exc:
                self.counts["failed"] += 1
                diag = exc.diag
                self.problems.append(
                    f"raw row {raw_row_id}: {type(exc).__name__} {diag.constraint_name or ''}"
                    f" {(diag.message_primary or '')[:120]}"
                )

    def _outcome(self, raw_row_id, outcome, *, prospect=None, candidate=None, reason=None):
        self.conn.execute(
            "insert into ops.raw_row_outcomes (raw_row_id, outcome, prospect_id,"
            " candidate_prospect_id, reason, run_id) values (%s, %s, %s, %s, %s, %s)",
            (raw_row_id, outcome, prospect, candidate, reason, self.run_id),
        )
        return outcome

    def _process(self, raw_row_id, segment, status, errors, prospect, content):
        if status == "invalid" or prospect is None:
            return self._outcome(raw_row_id, "invalid", reason="; ".join(errors or []))
        values = grok_values(prospect)
        missing = [c for c in ("title", "company") if not values.get(c)]
        if missing:
            return self._outcome(
                raw_row_id, "invalid", reason="missing " + ", ".join(missing) + " (required)"
            )

        same = self._same_content_person(raw_row_id, content)
        if same:
            return self._outcome(
                raw_row_id, "duplicate", prospect=same, reason="identical row already imported"
            )

        exact = self._exact_match(values)
        if exact:
            self.merge_into(exact, values, segment, raw_row_id)
            return self._outcome(
                raw_row_id, "merged", prospect=exact, reason="same LinkedIn URL or email"
            )

        candidate = self._name_company_match(values)
        if candidate:
            return self._outcome(
                raw_row_id,
                "held_for_review",
                candidate=candidate,
                reason="same name and company as an existing person; confirm before merging",
            )

        pid = self.register(values, segment, raw_row_id)
        return self._outcome(raw_row_id, "registered", prospect=pid)

    # -- matching -------------------------------------------------------------------------

    def _same_content_person(self, raw_row_id, content):
        rows = self.conn.execute(
            "select o.prospect_id, "
            + ", ".join(f"g.{c}" for c in GROK_COLUMNS)
            + " from raw.grok_rows g join ops.raw_row_outcomes o using (raw_row_id)"
            " where o.prospect_id is not null and g.raw_row_id <> %s"
            " and g.name is not distinct from %s and g.company is not distinct from %s",
            (raw_row_id, content[0] or None, content[2] or None),
        ).fetchall()
        for pid, *cols in rows:
            if _row_key(dict(zip(GROK_COLUMNS, cols, strict=True))) == content:
                return pid
        return None

    def _exact_match(self, values):
        for column in ("linkedin_url", "email"):
            if values.get(column):
                found = self.conn.execute(
                    f"select prospect_id from core.prospect_registry where {column} = %s",
                    (values[column],),
                ).fetchone()
                if found:
                    return found[0]
        return None

    def _name_company_match(self, values):
        found = self.conn.execute(
            "select prospect_id from core.all_prospects"
            " where lower(regexp_replace(trim(name), '\\s+', ' ', 'g')) = %s"
            " and lower(regexp_replace(trim(company), '\\s+', ' ', 'g')) = %s limit 1",
            (_key(values["name"]), _key(values["company"])),
        ).fetchone()
        return found[0] if found else None

    # -- writes ------------------------------------------------------------------------------

    def _company(self, values):
        website = values.get("company_website")
        if not website:
            return None
        try:
            domain = normalize_domain(website)
        except ValueError:
            return None
        found = self.conn.execute(
            "select company_id from core.companies where domain = %s", (domain,)
        ).fetchone()
        if found:
            return found[0]
        return self.conn.execute(
            "insert into core.companies (name, domain, website, original_source)"
            " values (%s, %s, %s, 'grok') returning company_id",
            (values.get("company") or domain, domain, website),
        ).fetchone()[0]

    def _add_signal(self, pid, company_id, values, raw_row_id) -> bool:
        if not values.get("signal"):
            return False
        exists = self.conn.execute(
            "select 1 from core.signals where prospect_id = %s and signal = %s"
            " and signal_date is not distinct from %s and source_url is not distinct from %s",
            (pid, values["signal"], values.get("signal_date"), values.get("source_url")),
        ).fetchone()
        if exists:
            return False
        self.conn.execute(
            "insert into core.signals (prospect_id, company_id, raw_row_id, signal, signal_date,"
            " source_url, why_fit, outreach_angle, source) values (%s, %s, %s, %s, %s, %s, %s,"
            " %s, 'grok')",
            (pid, company_id, raw_row_id, *[values.get(c) for c in SIGNAL_COLUMNS]),
        )
        return True

    def register(self, values, segment, raw_row_id):
        company_id = self._company(values)
        domain = None
        if values.get("company_website"):
            try:
                domain = normalize_domain(values["company_website"])
            except ValueError:
                domain = None
        row = {
            **values,
            "segment": [segment] if segment else [],
            "company_id": str(company_id) if company_id else None,
            "domain": domain,
            "original_source": "grok",
        }
        row = {k: v for k, v in row.items() if v is not None}
        pid = self.conn.execute(
            "select ops.register_prospect(%s, %s, 'system', 'grok import', %s)",
            (Jsonb(row), self.run_id, raw_row_id),
        ).fetchone()[0]
        self._add_signal(pid, company_id, values, raw_row_id)
        return pid

    def merge_into(self, pid, values, segment, raw_row_id, *, actor="system", reason="grok import"):
        current = self.conn.execute(
            "select to_jsonb(p) from core.all_prospects p where prospect_id = %s", (pid,)
        ).fetchone()[0]
        company_id = current.get("company_id") or self._company(values)
        changes = {
            k: v
            for k, v in values.items()
            if k not in SIGNAL_COLUMNS and current.get(k) is None and v is not None
        }
        if not current.get("company_id") and company_id:
            changes["company_id"] = str(company_id)
        if segment and segment not in (current.get("segment") or []):
            changes["segment"] = sorted({*(current.get("segment") or []), segment})
        if self._add_signal(pid, company_id, values, raw_row_id):
            newer = (values.get("signal_date") or "") > (current.get("signal_date") or "")
            if newer or not current.get("signal"):
                changes.update({c: values.get(c) for c in SIGNAL_COLUMNS})
        if changes:
            self.conn.execute(
                "select ops.update_prospect(%s, %s, %s, %s, %s)",
                (pid, Jsonb(changes), self.run_id, actor, reason),
            )


def review_queue(conn: psycopg.Connection) -> list[dict]:
    rows = conn.execute(
        "select o.raw_row_id, o.reason, g.name, g.title, g.company, g.signal, g.signal_date,"
        " p.prospect_id, p.layer, p.name, p.title, p.company, p.signal, p.signal_date"
        " from ops.raw_row_outcomes o join raw.grok_rows g using (raw_row_id)"
        " join core.all_prospects p on p.prospect_id = o.candidate_prospect_id"
        " where o.outcome = 'held_for_review' and o.resolution is null order by o.raw_row_id"
    ).fetchall()
    keys = ("name", "title", "company", "signal", "signal_date")
    return [
        {
            "raw_row_id": r[0],
            "reason": r[1],
            "incoming": dict(zip(keys, r[2:7], strict=True)),
            "candidate": {"prospect_id": str(r[7]), "layer": r[8]}
            | dict(zip(keys, r[9:14], strict=True)),
        }
        for r in rows
    ]


def resolve(
    conn: psycopg.Connection, data_dir: Path, rules: dict, raw_row_id: int, action: str, by: str
) -> dict:
    """Resolve one held row: merge into the candidate, register as new, or reject."""
    if action not in {"merge", "new", "reject"}:
        raise ValueError("action must be merge, new or reject")
    importer = WarehouseImporter(conn, data_dir, rules)
    held = conn.execute(
        "select o.candidate_prospect_id, g.segment, f.storage_path, g.row_number"
        " from ops.raw_row_outcomes o join raw.grok_rows g using (raw_row_id)"
        " join raw.files f using (file_id)"
        " where o.raw_row_id = %s and o.outcome = 'held_for_review' and o.resolution is null",
        (raw_row_id,),
    ).fetchone()
    if not held:
        raise ValueError(f"raw row {raw_row_id} is not waiting for review")
    candidate, segment, storage, number = held
    parsed = {
        r.number: r.prospect
        for r in parse_csv(data_dir.parent / storage, Source.grok, rules["mappings"]["grok"])
    }
    values = grok_values(parsed[number])
    pid = None
    with conn.transaction():
        if action == "merge":
            importer.merge_into(
                candidate, values, segment, raw_row_id, actor="human", reason="review: merged"
            )
            pid = candidate
        elif action == "new":
            pid = importer.register(values, segment, raw_row_id)
        conn.execute(
            "update ops.raw_row_outcomes set resolution = %s, resolved_by = %s,"
            " resolved_at = now(), prospect_id = %s where raw_row_id = %s",
            ({"merge": "merged", "new": "new", "reject": "rejected"}[action], by, pid, raw_row_id),
        )
    return {
        "raw_row_id": raw_row_id,
        "resolution": action,
        "prospect_id": str(pid) if pid else None,
    }

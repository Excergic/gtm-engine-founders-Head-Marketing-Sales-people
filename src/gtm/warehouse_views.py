"""Editable CSV views of the warehouse (data/views/), with validated write-back.

Export writes prospects.csv, companies.csv and deals.csv. Only the EDITABLE columns of
prospects.csv may be changed; edits are applied in one transaction through
ops.update_prospect (so layer rules, uniqueness and promotion still apply) and refused
as a whole if any row is invalid, changes a read-only column, or was changed in the
warehouse after the export.
"""

from __future__ import annotations

import csv
import os
import tempfile
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from .models import normalize_linkedin, normalize_x_handle
from .warehouse_load import GROK_COLUMNS

PROSPECT_COLUMNS = [
    "prospect_id", "layer", *GROK_COLUMNS, "segment", "email", "email_status", "domain",
    "seniority", "function", "lifecycle_stage", "lead_status", "connection_status",
    "reply_status", "tier", "lead_score", "consulting_stage", "job_stage", "do_not_contact",
    "owner", "notes", "updated_at",
]  # fmt: skip
EDITABLE = {
    *GROK_COLUMNS, "email", "email_status", "seniority", "function", "consulting_stage",
    "job_stage", "do_not_contact", "owner", "notes",
}  # fmt: skip
SIGNAL_COLUMNS = {"signal", "signal_date", "source_url", "why_fit", "outreach_angle"}
EDITABLE -= SIGNAL_COLUMNS  # signals are append-only history; add new ones through import

COMPANY_COLUMNS = [
    "company_id", "name", "domain", "website", "industry", "employee_range", "funding_stage",
    "icp_fit", "account_tier", "lifecycle_stage", "owner", "updated_at",
]  # fmt: skip
DEAL_COLUMNS = [
    "deal_id", "company_id", "primary_prospect_id", "pipeline", "stage", "amount", "currency",
    "expected_close_date", "next_step", "updated_at",
]  # fmt: skip


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "|".join(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _write(path: Path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(columns)
        writer.writerows([_text(v) for v in row] for row in rows)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)


def export_views(conn: psycopg.Connection, directory: Path) -> dict:
    queries = {
        "prospects.csv": (
            PROSPECT_COLUMNS,
            "select " + ", ".join(PROSPECT_COLUMNS) + " from core.all_prospects"
            " order by layer desc, name",
        ),
        "companies.csv": (
            COMPANY_COLUMNS,
            "select " + ", ".join(COMPANY_COLUMNS) + " from core.companies order by name",
        ),
        "deals.csv": (
            DEAL_COLUMNS,
            "select " + ", ".join(DEAL_COLUMNS) + " from outbound.deals order by updated_at",
        ),
    }
    counts = {}
    with conn.transaction():  # one consistent snapshot
        conn.execute("set transaction isolation level repeatable read, read only")
        for name, (columns, sql) in queries.items():
            rows = conn.execute(sql).fetchall()
            _write(directory / name, columns, rows)
            counts[name] = len(rows)
    return counts


def _parse(column, value):
    value = value.strip()
    if value == "":
        return None
    if column == "do_not_contact":
        if value.lower() not in {"true", "false"}:
            raise ValueError("do_not_contact must be true or false")
        return value.lower() == "true"
    if column == "linkedin_url":
        return normalize_linkedin(value)
    if column == "x_handle":
        return normalize_x_handle(value)
    if column == "email":
        return value.lower()
    return value


def apply_view_edits(conn: psycopg.Connection, directory: Path, actor: str = "human") -> dict:
    path = directory / "prospects.csv"
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames != PROSPECT_COLUMNS:
            raise ValueError("prospects.csv columns were changed; export the view again")
        edited = list(reader)

    current = {
        str(r[0]): r
        for r in conn.execute(
            "select " + ", ".join(PROSPECT_COLUMNS) + " from core.all_prospects"
        ).fetchall()
    }
    plan, errors = [], []
    for number, row in enumerate(edited, 2):
        pid = row["prospect_id"]
        if pid not in current:
            errors.append(f"line {number}: unknown prospect_id (add people through import)")
            continue
        now = dict(zip(PROSPECT_COLUMNS, current[pid], strict=True))
        if row["updated_at"] != _text(now["updated_at"]):
            changed_cols = [c for c in EDITABLE if row[c] != _text(now[c])]
            if changed_cols:
                errors.append(f"line {number}: changed in the warehouse after export")
            continue
        changes = {}
        for column in PROSPECT_COLUMNS:
            if row[column] == _text(now[column]):
                continue
            if column not in EDITABLE:
                errors.append(f"line {number}: {column} is read-only")
                continue
            try:
                changes[column] = _parse(column, row[column])
            except ValueError as exc:
                errors.append(f"line {number}: {exc}")
        if changes:
            plan.append((pid, changes))
    if errors:
        return {"applied": 0, "errors": errors}

    run = None
    with conn.transaction():
        run = conn.execute(
            "insert into ops.pipeline_runs (job, details) values ('import', %s) returning run_id",
            (Jsonb({"source": "csv view edits", "rows": len(plan)}),),
        ).fetchone()[0]
        moved = {}
        for pid, changes in plan:
            layer = conn.execute(
                "select ops.update_prospect(%s, %s, %s, %s, 'edited in prospects.csv view')",
                (pid, Jsonb(changes), run, actor),
            ).fetchone()[0]
            moved[pid] = layer
        conn.execute(
            "update ops.pipeline_runs set finished_at = now(), status = 'succeeded',"
            " rows_in = %s, rows_ok = %s where run_id = %s",
            (len(plan), len(plan), run),
        )
    return {"applied": len(plan), "run_id": str(run), "layers": moved, "errors": []}

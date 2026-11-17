"""Enrichment: find the missing LinkedIn URL and verified work email, never invent them.

Flow (Step 3, waterfall LinkedIn URL -> work email -> verify):
1. ``select_batch`` picks people missing either field (silver first: one field away from
   golden), freshest signal first. ``gtm enrich batch`` writes them to a request file.
2. An ``Enricher`` returns ``Finding`` records. Today that is ``FileEnricher``: results a
   provider run (Clay, then Vibe Prospecting, then manual) wrote to a JSON file. API
   adapters for unattended runs implement the same interface later.
3. ``apply_findings`` validates every finding, stores the provider's raw response, writes
   provenance to ``core.enrichment``, and fills blanks only through ``ops.update_prospect``,
   which promotes people to silver/golden. Manual values are never overwritten.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import Prospect, Source, normalize_linkedin

EMAIL_STATUS = {
    # provider wording -> warehouse email_status
    "verified": "verified",
    "valid": "verified",
    "deliverable": "verified",
    "safe": "verified",
    "risky": "risky",
    "catch-all": "risky",
    "catch_all": "risky",
    "accept_all": "risky",
    "unknown": "unknown",
    "invalid": "invalid",
    "undeliverable": "invalid",
}
PROVIDERS = ("clay", "vibe_prospecting", "manual")


class Finding(BaseModel):
    """One value a provider found for one person. Missing values are simply absent."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    prospect_id: str
    provider: Literal["clay", "vibe_prospecting", "manual"]
    linkedin_url: str | None = None
    email: str | None = None
    email_status: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    request: dict = Field(default_factory=dict)
    response: dict = Field(default_factory=dict)

    @field_validator("linkedin_url")
    @classmethod
    def _linkedin(cls, value):
        return normalize_linkedin(value) if value else None

    @field_validator("email")
    @classmethod
    def _email(cls, value):
        # Reuse the Prospect model's email rules.
        return Prospect(full_name="xx", source=Source.grok, email=value).email if value else None

    @field_validator("email_status")
    @classmethod
    def _status(cls, value):
        if value is None:
            return None
        mapped = EMAIL_STATUS.get(value.strip().lower())
        if mapped is None:
            raise ValueError(f"unknown email status {value!r}")
        return mapped


class Enricher(Protocol):
    name: str

    def enrich(self, batch: list[dict]) -> list[Finding]: ...


class FileEnricher:
    """Findings written to a JSON file by a provider run (``{"findings": [...]}``)."""

    name = "file"

    def __init__(self, path: Path):
        self.path = path

    def enrich(self, batch: list[dict]) -> list[Finding]:
        wanted = {p["prospect_id"] for p in batch} if batch else None
        data = json.loads(self.path.read_text())
        findings = [Finding.model_validate(f) for f in data.get("findings", [])]
        return [f for f in findings if wanted is None or f.prospect_id in wanted]


def select_batch(conn: psycopg.Connection, limit: int, layer: str | None = None) -> list[dict]:
    """People missing a LinkedIn URL or verified email, silver first, freshest signal first."""
    if layer not in (None, "staging", "silver"):
        raise ValueError("layer must be staging or silver")
    rows = conn.execute(
        "select prospect_id, layer, name, title, company, company_website, domain,"
        " linkedin_url, email, email_status, signal_date, segment"
        " from core.all_prospects"
        " where layer <> 'golden' and not do_not_contact"
        " and (%(layer)s::text is null or layer = %(layer)s)"
        " order by (layer = 'silver') desc, signal_date desc nulls last, name"
        " limit %(limit)s",
        {"layer": layer, "limit": limit},
    ).fetchall()
    keys = ("prospect_id", "layer", "name", "title", "company", "company_website", "domain",
            "linkedin_url", "email", "email_status", "signal_date", "segment")  # fmt: skip
    batch = []
    for row in rows:
        person = dict(zip(keys, row, strict=True))
        person["prospect_id"] = str(person["prospect_id"])
        person["needs"] = [
            field
            for field, missing in (
                ("linkedin_url", not person["linkedin_url"]),
                ("email", person["email_status"] != "verified"),
            )
            if missing
        ]
        batch.append(person)
    return batch


def write_request(batch: list[dict], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    path = directory / f"{stamp}_request.json"
    path.write_text(json.dumps({"created_at": stamp, "people": batch}, indent=2, default=str))
    return path


def apply_findings(
    conn: psycopg.Connection, findings: list[Finding], *, dry_run: bool = False
) -> dict:
    """Validate and apply findings; each person in its own transaction. Fill blanks only."""
    conn.autocommit = True
    counts: Counter = Counter()
    problems: list[str] = []
    planned: list[dict] = []
    run = None
    if not dry_run:
        run = conn.execute(
            "insert into ops.pipeline_runs (job, details) values ('enrich', %s) returning run_id",
            (Jsonb({"findings": len(findings)}),),
        ).fetchone()[0]

    for f in findings:
        current = conn.execute(
            "select layer, linkedin_url, email, email_status, do_not_contact"
            " from core.all_prospects where prospect_id = %s",
            (f.prospect_id,),
        ).fetchone()
        if current is None:
            problems.append(f"{f.prospect_id}: unknown prospect")
            counts["unknown_prospect"] += 1
            continue
        layer, linkedin, email, email_status, dnc = current
        if dnc:
            counts["skipped_do_not_contact"] += 1
            continue

        changes: dict = {}
        if f.linkedin_url and not linkedin:
            changes["linkedin_url"] = f.linkedin_url
        elif f.linkedin_url and linkedin != f.linkedin_url:
            problems.append(f"{f.prospect_id}: {f.provider} LinkedIn differs from existing; kept")
            counts["conflicting_value_kept"] += 1
        if f.email and not email:
            changes["email"] = f.email
            changes["email_status"] = f.email_status or "unknown"
        elif f.email and f.email == email and f.email_status and email_status != "verified":
            changes["email_status"] = f.email_status  # a verification of the existing email
        elif f.email and email and f.email != email:
            problems.append(f"{f.prospect_id}: {f.provider} email differs from existing; kept")
            counts["conflicting_value_kept"] += 1

        for key in ("linkedin_url", "email"):
            if key in changes:
                taken = conn.execute(
                    f"select prospect_id from core.prospect_registry where {key} = %s"
                    " and prospect_id <> %s",
                    (changes[key], f.prospect_id),
                ).fetchone()
                if taken:
                    problems.append(
                        f"{f.prospect_id}: {key} already belongs to {taken[0]}; possible duplicate"
                    )
                    counts["possible_duplicate"] += 1
                    changes.pop(key)
                    if key == "email":
                        changes.pop("email_status", None)

        planned.append({"prospect_id": f.prospect_id, "provider": f.provider, "changes": changes})
        if dry_run:
            counts["would_change" if changes else "nothing_new"] += 1
            continue

        try:
            with conn.transaction():
                response_id = conn.execute(
                    "insert into raw.provider_responses (provider, prospect_id, request, response,"
                    " run_id) values (%s, %s, %s, %s, %s) returning response_id",
                    (f.provider, f.prospect_id, Jsonb(f.request), Jsonb(f.response), run),
                ).fetchone()[0]
                for field in ("linkedin_url", "email", "email_status"):
                    value = getattr(f, field)
                    if value:
                        conn.execute(
                            "insert into core.enrichment (prospect_id, field, value, provider,"
                            " confidence, response_id, run_id) values (%s, %s, %s, %s, %s, %s, %s)",
                            (f.prospect_id, field, value, f.provider, f.confidence, response_id,
                             run),
                        )  # fmt: skip
                new_layer = layer
                if changes:
                    new_layer = conn.execute(
                        "select ops.update_prospect(%s, %s, %s, 'system', %s)",
                        (f.prospect_id, Jsonb(changes), run, f"enrichment: {f.provider}"),
                    ).fetchone()[0]
                counts[f"now_{new_layer}" if changes else "nothing_new"] += 1
                if new_layer != layer:
                    counts[f"promoted_{layer}_to_{new_layer}"] += 1
        except psycopg.Error as exc:
            counts["failed"] += 1
            problems.append(f"{f.prospect_id}: {type(exc).__name__} {exc.diag.constraint_name}")

    if run:
        conn.execute(
            "update ops.pipeline_runs set finished_at = now(), status = %s, rows_in = %s,"
            " rows_ok = %s, rows_failed = %s, details = details || %s where run_id = %s",
            (
                "partial" if counts["failed"] else "succeeded",
                len(findings),
                len(findings) - counts["failed"],
                counts["failed"],
                Jsonb({"counts": dict(counts)}),
                run,
            ),
        )
    return {
        "dry_run": dry_run,
        "run_id": str(run) if run else None,
        "counts": dict(counts),
        "problems": problems,
        "planned": planned if dry_run else None,
    }


def match_rate(conn: psycopg.Connection, run_id: str) -> dict:
    """Per-provider found rates for one enrichment run."""
    rows = conn.execute(
        "select provider, field, count(distinct prospect_id) from core.enrichment"
        " where run_id = %s group by 1, 2 order by 1, 2",
        (run_id,),
    ).fetchall()
    return {f"{p}.{f}": n for p, f, n in rows}

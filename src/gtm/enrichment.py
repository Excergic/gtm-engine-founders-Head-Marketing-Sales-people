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
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

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


class Finding(BaseModel):
    """One value a provider found for one person. Missing values are simply absent."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    prospect_id: str
    provider: str = Field(pattern=r"^[a-z][a-z0-9_]{1,39}$")
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
    conn: psycopg.Connection,
    findings: list[Finding],
    *,
    dry_run: bool = False,
    run_id=None,
) -> dict:
    """Validate and apply findings; each person in its own transaction. Fill blanks only."""
    conn.autocommit = True
    counts: Counter = Counter()
    problems: list[str] = []
    planned: list[dict] = []
    run = run_id
    owns_run = run is None and not dry_run
    if owns_run:
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

    if owns_run:
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


PERSON_COLUMNS = ("prospect_id", "layer", "name", "title", "company", "company_website",
                  "domain", "linkedin_url", "email", "email_status", "signal_date", "segment")  # fmt: skip


def current_people(conn: psycopg.Connection, ids: list[str]) -> list[dict]:
    rows = conn.execute(
        "select " + ", ".join(PERSON_COLUMNS) + " from core.all_prospects"
        " where prospect_id = any(%s::uuid[]) and not do_not_contact",
        (ids,),
    ).fetchall()
    people = [dict(zip(PERSON_COLUMNS, r, strict=True)) for r in rows]
    for p in people:
        p["prospect_id"] = str(p["prospect_id"])
    return people


def needs(person: dict, stage: str) -> bool:
    if person["layer"] == "golden":
        return False
    if stage == "linkedin_url":
        return not person["linkedin_url"]
    if stage == "email":
        return not person["email"]
    return bool(person["email"]) and person["email_status"] not in ("verified", "invalid")


def record_attempts(conn, run_id, provider: str, stage: str, people: list[dict],
                    findings: list[Finding]) -> None:  # fmt: skip
    """One ops.enrichment_attempts row per person asked: found, miss or error."""
    if run_id is None:
        return
    found = {f.prospect_id for f in findings}
    rows = []
    for person in people:
        key = (provider, stage)
        if key in person.get("failed", {}):
            message = next((e for e in reversed(person.get("errors", []))
                            if e.startswith(f"{provider}.{stage}:")), "failed")  # fmt: skip
            rows.append((person["prospect_id"], stage, provider, "error", message[:1000],
                         person["failed"][key], run_id))  # fmt: skip
        else:
            outcome = "found" if person["prospect_id"] in found else "miss"
            rows.append((person["prospect_id"], stage, provider, outcome, None, None, run_id))
    with conn.cursor() as cur:
        cur.executemany(
            "insert into ops.enrichment_attempts (prospect_id, stage, provider, outcome, error,"
            " retryable, run_id) values (%s, %s, %s, %s, %s, %s, %s)",
            rows,
        )


def run_waterfall(
    conn: psycopg.Connection,
    ids: list[str],
    waterfall: dict[str, list[str]],
    enrichers: dict,
    *,
    dry_run: bool = False,
    only: set[str] | None = None,
    connect: Callable[[], psycopg.Connection] | None = None,
    journal_dir: Path | None = None,
) -> dict:
    """LinkedIn URL -> email -> verify. Each provider sees only people still missing the field.

    Provider calls can take minutes, long enough for a serverless database to drop an idle
    connection. With ``connect``, a dropped connection is replaced before each database step.
    With ``journal_dir``, every provider's findings are written to a file before they are
    applied, so paid results survive any database failure (replay: ``gtm enrich apply``).
    """

    from .enrichers import record_failure  # enrichers imports this module

    def live() -> psycopg.Connection:
        nonlocal conn
        try:
            conn.autocommit = True
            conn.execute("select 1")
        except (psycopg.OperationalError, psycopg.InterfaceError):
            if connect is None:
                raise
            conn = connect()
            conn.autocommit = True
        return conn

    live()
    run = None
    if not dry_run:
        run = conn.execute(
            "insert into ops.pipeline_runs (job, details) values ('enrich', %s) returning run_id",
            (Jsonb({"waterfall": waterfall, "people": len(ids), "prospect_ids": list(ids)}),),
        ).fetchone()[0]
    steps, errors = [], []
    for stage in ("linkedin_url", "email", "verify"):
        for name in waterfall.get(stage, []):
            if only and name not in only:
                continue
            people = [p for p in current_people(live(), ids) if needs(p, stage)]
            step = {"stage": stage, "provider": name, "asked": len(people)}
            if dry_run:
                # Planning only: never call a provider (calls can spend credits).
                step["would_ask"] = [p["name"] for p in people]
            elif people and stage in enrichers[name].stages:
                try:
                    findings = enrichers[name].find(stage, people)
                except Exception as exc:  # a broken provider must not stop the waterfall
                    findings = []
                    for person in people:
                        record_failure(person, name, stage, exc)
                failed = [p for p in people if (name, stage) in p.get("failed", {})]
                errors += [e for p in people for e in p.get("errors", [])]
                if journal_dir and findings:
                    journal_dir.mkdir(parents=True, exist_ok=True)
                    (journal_dir / f"{run}_{stage}_{name}.json").write_text(json.dumps(
                        {"findings": [f.model_dump(mode="json") for f in findings]},
                        indent=2, default=str))  # fmt: skip
                result = apply_findings(live(), findings, dry_run=dry_run, run_id=run)
                record_attempts(live(), run, name, stage, people, findings)
                step.update(found=len(findings), failed=len(failed), counts=result["counts"])
                errors += result["problems"]
            steps.append(step)
    layers = Counter(p["layer"] for p in current_people(live(), ids))
    if run:
        conn.execute(
            "update ops.pipeline_runs set finished_at = now(), status = %s, rows_in = %s,"
            " rows_ok = %s, details = details || %s where run_id = %s",
            ("partial" if errors else "succeeded", len(ids), len(ids),
             Jsonb({"steps": steps, "errors": errors[:50]}), run),
        )  # fmt: skip
    return {"run_id": str(run) if run else None, "dry_run": dry_run, "steps": steps,
            "layers_now": dict(layers), "errors": errors}  # fmt: skip


# ---------------------------------------------------------------------------
# Running many batches safely
# ---------------------------------------------------------------------------

STOP_WORDS = ("insufficient", "credit", "balance", "payment method")


def asked_recently(conn: psycopg.Connection, hours: int = 24) -> set[str]:
    """People any enrich run asked in the last ``hours`` (attempts, or older run records)."""
    rows = conn.execute(
        "select prospect_id::text from ops.enrichment_attempts"
        " where attempted_at > now() - make_interval(hours => %(h)s)"
        " union select jsonb_array_elements_text(details->'prospect_ids') from ops.pipeline_runs"
        " where job = 'enrich' and started_at > now() - make_interval(hours => %(h)s)"
        " and details ? 'prospect_ids'",
        {"h": hours},
    ).fetchall()
    return {r[0] for r in rows}


def retry_queue(conn: psycopg.Connection) -> list[str]:
    """Not-yet-golden people whose latest attempt at some stage was a retryable error."""
    rows = conn.execute(
        "select distinct l.prospect_id::text from ops.enrichment_latest l"
        " join core.all_prospects p using (prospect_id)"
        " where l.outcome = 'error' and l.retryable is not false"
        " and p.layer <> 'golden' and not p.do_not_contact"
        " order by 1"
    ).fetchall()
    return [r[0] for r in rows]


def run_batches(
    connect: Callable[[], psycopg.Connection],
    ids: list[str],
    waterfall: dict[str, list[str]],
    enrichers: dict,
    *,
    limit: int = 25,
    dry_run: bool = False,
    only: set[str] | None = None,
    runs_dir: Path | None = None,
    log: Callable[[str], None] = print,
    max_consecutive_failures: int = 3,
) -> dict:
    """Run the waterfall over ``ids`` in batches. A failed person, provider or batch is
    recorded and skipped; the run stops early only when a provider reports a credit
    problem or several batches in a row fail (something is down)."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    found: Counter = Counter()
    failed_people: Counter = Counter()
    errors: list[str] = []
    failed_batches: list[dict] = []
    batches, consecutive, stopped = 0, 0, None
    for start in range(0, len(ids), limit):
        chunk = ids[start : start + limit]
        batches += 1
        try:
            with connect() as conn:
                result = run_waterfall(
                    conn, chunk, waterfall, enrichers, dry_run=dry_run, only=only,
                    connect=connect,
                    journal_dir=runs_dir / "findings" if runs_dir else None,
                )  # fmt: skip
        except Exception as exc:  # the batch failed as a whole; keep going
            consecutive += 1
            message = f"{type(exc).__name__}: {exc}"[:500]
            failed_batches.append({"batch": batches, "people": len(chunk), "error": message})
            log(f"batch {batches}: FAILED ({message}); continuing")
            _mark_batch_failed(connect, chunk, message)
            if consecutive >= max_consecutive_failures:
                stopped = f"{consecutive} batches failed in a row"
                log(f"stopping: {stopped}")
                break
            continue
        consecutive = 0
        if runs_dir and not dry_run:
            runs_dir.mkdir(parents=True, exist_ok=True)
            path = runs_dir / f"{stamp}_batch{batches:03d}.json"
            path.write_text(json.dumps(result, indent=2, default=str))
        for step in result["steps"]:
            key = f"{step['stage']}.{step['provider']}"
            found[key] += step.get("found") or 0
            failed_people[key] += step.get("failed") or 0
        errors += result["errors"]
        log(f"batch {batches}: {start + len(chunk)}/{len(ids)} people,"
            f" layers {result['layers_now']}, errors {len(result['errors'])}")  # fmt: skip
        if any(word in str(e).lower() for e in result["errors"] for word in STOP_WORDS):
            stopped = "provider reports a credit problem"
            log(f"stopping: {stopped}")
            break
    return {"people": len(ids), "batches": batches, "found": dict(found),
            "failed": {k: v for k, v in failed_people.items() if v},
            "failed_batches": failed_batches, "stopped": stopped,
            "error_count": len(errors), "errors": errors[:50]}  # fmt: skip


def _mark_batch_failed(connect, chunk: list[str], message: str) -> None:
    """Close the run row a crashed batch left as 'running' (best effort)."""
    try:
        with connect() as conn:
            conn.execute(
                "update ops.pipeline_runs set status = 'failed', finished_at = now(),"
                " details = details || %s where job = 'enrich' and status = 'running'"
                " and details->'prospect_ids' = %s",
                (Jsonb({"error": message}), Jsonb(chunk)),
            )
            conn.commit()
    except Exception:
        pass

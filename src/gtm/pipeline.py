"""The daily pipeline: everything from new Grok files to today's ready list, in the warehouse.

    1 import         new CSVs in data/incoming -> raw -> staging / silver / golden
    2 enrich         people never asked before whose title is in the ICP (capped per run)
    3 retry_failed   lookups whose last attempt failed with a retryable error
    4 score          ICP score for everyone; golden people in or out of ready

Every step is isolated: a failed step is recorded and the next one still runs. Paid
enrichment is skipped when Deepline credits are below ``pipeline.min_deepline_credits``.
A pipeline run already marked running (started in the last 6 hours) makes a new run skip,
so a scheduled and a manual run never overlap; an older one is treated as crashed.
The run and each step's outcome are stored in ops.pipeline_runs (job 'pipeline').
Nothing here sends messages.
"""

from __future__ import annotations

import json
import subprocess
import traceback
from collections.abc import Callable
from datetime import date

import psycopg
from psycopg.types.json import Jsonb

STALE_AFTER = "6 hours"  # a 'running' pipeline older than this crashed


def deepline_credits() -> float | None:
    """Spendable Deepline credits, or None when the balance can't be read."""
    try:
        done = subprocess.run(["deepline", "billing", "balance", "--json"],
                              capture_output=True, text=True, timeout=60)  # fmt: skip
        return float(json.loads(done.stdout)["spendable_balance_credits"])
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return None


def new_icp_people(conn: psycopg.Connection, icp_rules: dict, limit: int) -> list[str]:
    """Not-golden people no provider has been asked about yet, ICP titles only,
    freshest signal first."""
    from .icp import segment

    rows = conn.execute(
        "select p.prospect_id::text, p.title from core.all_prospects p"
        " where p.layer <> 'golden' and not p.do_not_contact"
        " and not exists (select 1 from ops.enrichment_attempts a"
        "                 where a.prospect_id = p.prospect_id)"
        " and not exists (select 1 from ops.pipeline_runs r where r.job = 'enrich'"
        "                 and r.details->'prospect_ids' ? p.prospect_id::text)"
        " order by p.signal_date desc nulls last"
    ).fetchall()
    return [pid for pid, title in rows if segment(title, icp_rules)][:limit]


def run_daily(
    settings,
    connect: Callable[[], psycopg.Connection],
    *,
    dry_run: bool = False,
    credits: Callable[[], float | None] = deepline_credits,
    enrichers: dict | None = None,
    log: Callable[[str], None] = print,
) -> dict:
    from . import icp
    from .enrichers import build, waterfall_config
    from .enrichment import retry_queue, run_batches
    from .warehouse_import import WarehouseImporter

    rules = settings.rules()
    config = rules.get("pipeline", {})
    waterfall, providers = waterfall_config(rules)
    if enrichers is None:
        enrichers = {n: build(n, providers[n], settings.data_directory) for n in providers}

    with connect() as conn:
        conn.autocommit = True
        conn.execute(
            "update ops.pipeline_runs set status = 'failed', finished_at = now(),"
            " error_summary = 'did not finish (crashed or killed)'"
            " where job = 'pipeline' and status = 'running'"
            " and started_at < now() - %s::interval", (STALE_AFTER,))  # fmt: skip
        busy = conn.execute(
            "select run_id from ops.pipeline_runs where job = 'pipeline' and status = 'running'"
        ).fetchone()
        if busy:
            log(f"pipeline run {busy[0]} is still running; nothing to do")
            return {"status": "skipped", "reason": f"pipeline run {busy[0]} is still running"}
        run_id = None
        if not dry_run:
            run_id = conn.execute(
                "insert into ops.pipeline_runs (job, details) values ('pipeline', %s)"
                " returning run_id", (Jsonb({"date": date.today().isoformat()}),),
            ).fetchone()[0]  # fmt: skip

    steps: dict[str, dict] = {}
    status = "failed"  # until the steps finish
    try:

        def step(name: str, fn: Callable[[], dict]) -> None:
            log(f"[{name}] start")
            try:
                result = fn()
                steps[name] = {"status": result.pop("_status", "ok"), **result}
            except Exception as exc:  # one step never stops the others
                steps[name] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:500],
                               "trace": traceback.format_exc(limit=3)[-1500:]}  # fmt: skip
            log(f"[{name}] {steps[name]['status']}")

        def do_import():
            if dry_run:
                return {"_status": "skipped", "reason": "dry run"}
            with connect() as conn:
                result = WarehouseImporter(conn, settings.data_directory, rules).run()
            status = "partial" if result["problems"] else "ok"
            return {"_status": status, "raw": result["raw"], "outcomes": result["outcomes"],
                    "problems": result["problems"][:20]}  # fmt: skip

        def low_credits() -> dict | None:
            balance = credits()
            minimum = config.get("min_deepline_credits", 10)
            if balance is None:
                return {"_status": "skipped", "reason": "Deepline balance unavailable"}
            if balance < minimum:
                return {"_status": "skipped",
                        "reason": f"Deepline credits {balance} below {minimum}"}  # fmt: skip
            return None

        def do_enrich():
            if skip := low_credits():
                return skip
            with connect() as conn:
                ids = new_icp_people(conn, rules["icp"], config.get("enrich_max_people", 100))
            if not ids:
                return {"_status": "ok", "people": 0}
            result = run_batches(connect, ids, waterfall, enrichers, limit=25, dry_run=dry_run,
                                 runs_dir=settings.data_directory / "enrichment" / "runs",
                                 log=log)  # fmt: skip
            return _batch_summary(result)

        def do_retry():
            if skip := low_credits():
                return skip
            with connect() as conn:
                ids = retry_queue(conn)
            if not ids:
                return {"_status": "ok", "people": 0}
            result = run_batches(connect, ids, waterfall, enrichers, limit=25, dry_run=dry_run,
                                 runs_dir=settings.data_directory / "enrichment" / "runs",
                                 log=log)  # fmt: skip
            return _batch_summary(result)

        def do_score():
            with connect() as conn:
                result = icp.run(conn, rules, dry_run=dry_run)
            return {"tiers": result["tiers"], "golden": result["golden"],
                    "ready": result["ready"], "moves": result["lifecycle_moves"]}  # fmt: skip

        step("import", do_import)
        step("enrich", do_enrich)
        step("retry_failed", do_retry)
        step("score", do_score)

        failed = [n for n, s in steps.items() if s["status"] == "failed"]
        partial = [n for n, s in steps.items() if s["status"] == "partial"]
        status = "failed" if len(failed) == len(steps) else (
            "partial" if failed or partial else "succeeded")  # fmt: skip
        return {"run_id": str(run_id) if run_id else None, "dry_run": dry_run,
                "status": status, "steps": steps}  # fmt: skip
    except BaseException as exc:  # e.g. killed mid-run: still close the run record
        steps["pipeline"] = {"status": "failed", "error": repr(exc)[:500]}
        raise
    finally:
        if run_id:  # always close the run record, with a fresh connection
            with connect() as conn:
                conn.execute(
                    "update ops.pipeline_runs set finished_at = now(), status = %s,"
                    " error_summary = %s, details = details || %s where run_id = %s",
                    (status,
                     ", ".join(n for n, s in steps.items() if s["status"] == "failed") or None,
                     Jsonb({"steps": steps}), run_id),
                )  # fmt: skip
                conn.commit()


def _batch_summary(result: dict) -> dict:
    status = "partial" if result["failed_batches"] or result["stopped"] else "ok"
    return {"_status": status, "people": result["people"], "found": result["found"],
            "failed": result["failed"], "failed_batches": len(result["failed_batches"]),
            "stopped": result["stopped"], "error_count": result["error_count"]}  # fmt: skip

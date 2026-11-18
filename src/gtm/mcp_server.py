"""GTM warehouse MCP server: track golden prospects, their signals and who to reach out to.

Read-only by construction: every connection is opened with
``default_transaction_read_only=on``, so no tool can change data or send anything.

Run:  uv run gtm-mcp        (stdio; registered for Claude Code in .mcp.json)
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[2]


def connect() -> psycopg.Connection:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", interpolate=False)
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set (add it to .env)")
    return psycopg.connect(url, options="-c default_transaction_read_only=on",
                           row_factory=dict_row, connect_timeout=15)  # fmt: skip


def _model_version() -> str:
    from .config import Settings

    return Settings.load().rules()["icp"]["model_version"]


def _clean(rows: list[dict]) -> list[dict]:
    """JSON-friendly values (dates, UUIDs, decimals as strings or numbers)."""
    out = []
    for row in rows:
        item = {}
        for key, value in row.items():
            if value is None or isinstance(value, (str, int, float, bool, list, dict)):
                item[key] = value
            elif isinstance(value, Decimal):
                item[key] = int(value) if value == value.to_integral_value() else float(value)
            else:
                item[key] = str(value)
        out.append(item)
    return out


LATEST_SCORE = """
    left join lateral (
        select tier, total, reasons from core.score_snapshots s
        where s.prospect_id = p.prospect_id and s.model_version = %(version)s
        order by snapshot_date desc limit 1) s on true
"""


def pipeline_overview(conn) -> dict:
    layers = conn.execute(
        "select layer, count(*) as people from core.all_prospects group by layer order by layer"
    ).fetchall()
    stages = conn.execute(
        "select lifecycle_stage, count(*) as people from golden.prospects"
        " group by 1 order by 2 desc"
    ).fetchall()
    tiers = conn.execute(
        "select s.tier, count(*) as people from core.all_prospects p" + LATEST_SCORE +
        " group by 1 order by 1", {"version": _model_version()}).fetchall()  # fmt: skip
    retry = conn.execute(
        "select count(distinct prospect_id) as people from ops.enrichment_latest l"
        " join core.all_prospects p using (prospect_id)"
        " where l.outcome = 'error' and l.retryable is not false and p.layer <> 'golden'"
    ).fetchone()
    runs = conn.execute(
        "select job, status, started_at, finished_at, rows_in from ops.pipeline_runs"
        " order by started_at desc limit 8"
    ).fetchall()
    return {"layers": _clean(layers), "golden_by_stage": _clean(stages),
            "icp_tiers": _clean(tiers), "enrichment_retry_queue": retry["people"],
            "recent_runs": _clean(runs)}  # fmt: skip


def ready_to_contact(conn, limit: int = 50, tier: str | None = None) -> list[dict]:
    rows = conn.execute(
        "select p.prospect_id, p.name, p.title, p.company, p.email, p.linkedin_url,"
        " s.tier, s.total as score, p.signal, p.signal_date, p.outreach_angle, p.source_url,"
        " p.why_fit, p.connection_status, p.touches_count"
        " from golden.prospects p" + LATEST_SCORE +
        " where p.lifecycle_stage = 'ready' and (%(tier)s::text is null or s.tier = %(tier)s)"
        " order by s.tier, p.signal_date desc, s.total desc limit %(limit)s",
        {"version": _model_version(), "tier": tier, "limit": limit},
    ).fetchall()  # fmt: skip
    return _clean(rows)


def golden_prospects(conn, status: str = "all", search: str | None = None,
                     limit: int = 100) -> list[dict]:  # fmt: skip
    """status: all | ready | not_ready | stale (signal older than the ICP window)."""
    if status not in ("all", "ready", "not_ready", "stale"):
        raise ValueError("status must be all, ready, not_ready or stale")
    from .config import Settings

    max_age = Settings.load().rules()["icp"]["signal_max_age_days"]
    rows = conn.execute(
        "select p.prospect_id, p.name, p.title, p.company, p.lifecycle_stage, s.tier,"
        " s.total as score, p.signal, p.signal_date,"
        " case when p.signal_date ~ '^\\d{4}-\\d{2}-\\d{2}$'"
        "      then current_date - p.signal_date::date end as signal_age_days,"
        " p.email, p.linkedin_url, p.lead_status, p.do_not_contact"
        " from golden.prospects p" + LATEST_SCORE +
        " where (%(search)s::text is null or p.name ilike %(like)s or p.company ilike %(like)s)"
        " order by p.signal_date desc nulls last limit %(limit)s",
        {"version": _model_version(), "search": search, "like": f"%{search or ''}%",
         "limit": 1000},
    ).fetchall()  # fmt: skip
    rows = _clean(rows)
    if status == "ready":
        rows = [r for r in rows if r["lifecycle_stage"] == "ready"]
    elif status == "not_ready":
        rows = [r for r in rows if r["lifecycle_stage"] != "ready"]
    elif status == "stale":
        rows = [r for r in rows
                if r["signal_age_days"] is None or r["signal_age_days"] > max_age]  # fmt: skip
    return rows[:limit]


def prospect(conn, name_or_id: str) -> dict:
    """Everything about one person: record, signals, score, enrichment, history."""
    matches = conn.execute(
        "select * from core.all_prospects where prospect_id::text = %(q)s"
        " or name ilike %(like)s order by (prospect_id::text = %(q)s) desc, name limit 5",
        {"q": name_or_id, "like": f"%{name_or_id}%"},
    ).fetchall()
    if not matches:
        return {"error": f"no prospect matches {name_or_id!r}"}
    if len(matches) > 1 and str(matches[0]["prospect_id"]) != name_or_id:
        return {"matches": _clean([{k: m[k] for k in ("prospect_id", "name", "title",
                                                       "company", "layer")} for m in matches])}  # fmt: skip
    person = matches[0]
    pid = person["prospect_id"]
    signals = conn.execute(
        "select signal_date, signal, signal_type, source, source_url, first_seen_at"
        " from core.signals where prospect_id = %s order by first_seen_at desc", (pid,)).fetchall()  # fmt: skip
    score = conn.execute(
        "select snapshot_date, tier, total, lead_fit, intent, account_fit, engagement,"
        " model_version, reasons from core.score_snapshots where prospect_id = %s"
        " order by snapshot_date desc limit 3", (pid,)).fetchall()  # fmt: skip
    enrichment = conn.execute(
        "select provider, field, value, confidence, found_at from core.enrichment"
        " where prospect_id = %s order by found_at", (pid,)).fetchall()  # fmt: skip
    attempts = conn.execute(
        "select stage, provider, outcome, error, attempted_at from ops.enrichment_latest"
        " where prospect_id = %s order by stage", (pid,)).fetchall()  # fmt: skip
    history = conn.execute(
        "select at, from_layer, to_layer, from_state, to_state, reason from ops.state_transitions"
        " where prospect_id = %s order by transition_id", (pid,)).fetchall()  # fmt: skip
    keep = ("prospect_id", "layer", "lifecycle_stage", "name", "title", "company",
            "company_website", "country", "city", "email", "email_status", "linkedin_url",
            "x_handle", "signal", "signal_date", "source_url", "why_fit", "outreach_angle",
            "lead_status", "connection_status", "reply_status", "touches_count",
            "do_not_contact", "notes")  # fmt: skip
    return {"prospect": _clean([{k: person.get(k) for k in keep}])[0],
            "signals": _clean(signals), "scores": _clean(score),
            "enrichment": _clean(enrichment), "enrichment_attempts": _clean(attempts),
            "history": _clean(history)}  # fmt: skip


def signals_feed(conn, days: int = 14, layer: str = "golden", limit: int = 100) -> list[dict]:
    if layer not in ("golden", "silver", "staging", "all"):
        raise ValueError("layer must be golden, silver, staging or all")
    since = date.fromordinal(date.today().toordinal() - days).isoformat()
    rows = conn.execute(
        "select p.signal_date, p.name, p.title, p.company, p.layer, p.lifecycle_stage,"
        " p.signal, p.source_url, p.outreach_angle from core.all_prospects p"
        " where p.signal_date >= %(since)s and (%(layer)s = 'all' or p.layer = %(layer)s)"
        " order by p.signal_date desc, p.name limit %(limit)s",
        {"since": since, "layer": layer, "limit": limit},
    ).fetchall()
    return _clean(rows)


def enrichment_failures(conn, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "select p.name, p.company, p.layer, l.stage, l.provider, l.retryable, l.error,"
        " l.attempted_at from ops.enrichment_latest l join core.all_prospects p"
        " using (prospect_id) where l.outcome = 'error'"
        " order by l.attempted_at desc limit %s", (limit,)).fetchall()  # fmt: skip
    return _clean(rows)


def build_server():
    from mcp.server.fastmcp import FastMCP

    server = FastMCP(
        "gtm-warehouse",
        instructions=(
            "Read-only view of the GTM warehouse. Golden = verified email + LinkedIn URL. "
            "Ready = golden + fresh signal + ICP tier 1/2 + an angle with a source: the people "
            "to reach out to now. Nothing here sends messages or changes data."
        ),
    )

    def run(fn, *args, **kwargs):
        with connect() as conn:
            return fn(conn, *args, **kwargs)

    @server.tool()
    def overview() -> dict:
        """Pipeline health: people per layer, golden by lifecycle stage, ICP tiers,
        enrichment retry queue size and the latest pipeline runs."""
        return run(pipeline_overview)

    @server.tool()
    def ready_list(limit: int = 50, tier: str | None = None) -> list[dict]:
        """Who to reach out to now (lifecycle 'ready'), best tier and freshest signal first,
        with email, LinkedIn, signal, outreach angle and source. tier: tier_1 or tier_2."""
        return run(ready_to_contact, limit=limit, tier=tier)

    @server.tool()
    def golden_list(status: str = "all", search: str | None = None,
                    limit: int = 100) -> list[dict]:  # fmt: skip
        """Golden prospects with stage, tier and signal age. status: all, ready, not_ready,
        or stale (signal too old to contact). search matches name or company."""
        return run(golden_prospects, status=status, search=search, limit=limit)

    @server.tool()
    def prospect_detail(name_or_id: str) -> dict:
        """One person: full record, every signal, ICP score and reasons, which provider
        found what, enrichment attempts, and lifecycle history."""
        return run(prospect, name_or_id)

    @server.tool()
    def recent_signals(days: int = 14, layer: str = "golden", limit: int = 100) -> list[dict]:
        """Signals from the last N days, newest first. layer: golden, silver, staging, all."""
        return run(signals_feed, days=days, layer=layer, limit=limit)

    @server.tool()
    def failures(limit: int = 50) -> list[dict]:
        """Enrichment lookups whose latest attempt failed, with the error and whether a
        retry can help (retry with: gtm enrich run --retry-failed)."""
        return run(enrichment_failures, limit=limit)

    return server


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()

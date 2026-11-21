"""GTM engine CLI. The Postgres warehouse (DATABASE_URL in .env) is the only record:
Grok CSVs are inputs, loaded into raw and never edited. Nothing here sends messages."""

from __future__ import annotations

import functools
import json
from collections import Counter
from pathlib import Path
from typing import Annotated

import psycopg
import typer
from pydantic import ValidationError

from .config import Settings

app = typer.Typer(
    no_args_is_help=True,
    help="GTM engine: Grok CSVs -> warehouse -> enrichment -> ICP score -> ready.",
)
db_app = typer.Typer(help="Warehouse: migrations, imports and the duplicate review queue.")
app.add_typer(db_app, name="db")
enrich_app = typer.Typer(help="Find missing LinkedIn URLs and verified emails; fill blanks only.")
app.add_typer(enrich_app, name="enrich")
icp_app = typer.Typer(
    help="Score everyone against the ICP and move golden people in or out of ready."
)
app.add_typer(icp_app, name="icp")
schedule_app = typer.Typer(help="Run gtm pipeline every day on this Mac (launchd).")
app.add_typer(schedule_app, name="schedule")


def output(value):
    typer.echo(json.dumps(value, indent=2, default=str, ensure_ascii=False))


def guarded(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except typer.Exit:
            raise
        except ValidationError as exc:
            # Messages can include prospect values; output structural paths only.
            fields = [".".join(map(str, e["loc"])) for e in exc.errors(include_input=False)]
            typer.echo("Error: invalid values in fields: " + ", ".join(fields), err=True)
            raise typer.Exit(1) from exc
        except (ValueError, RuntimeError, OSError, psycopg.Error) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc

    return wrapped


def warehouse_connection(attempts: int = 1):
    from dotenv import load_dotenv

    from . import warehouse

    load_dotenv(Path.cwd() / ".env", interpolate=False)
    try:
        return warehouse.connect(attempts=attempts)
    except Exception as exc:  # psycopg errors can include the host; keep the message short
        raise RuntimeError(f"cannot connect to the warehouse: {type(exc).__name__}") from exc


@db_app.command("status")
@guarded
def db_status():
    """Show which migrations are applied, pending, or changed since applying."""
    from . import warehouse

    with warehouse_connection() as conn:
        output(warehouse.status(conn))


@db_app.command("migrate")
@guarded
def db_migrate():
    """Apply pending migrations in order, each in its own transaction."""
    from . import warehouse

    with warehouse_connection() as conn:
        output({"applied": warehouse.migrate(conn)})


@app.command("sql")
@guarded
def sql_command(query: str, limit: Annotated[int, typer.Option(min=1)] = 200):
    """Run one read-only SQL query against the warehouse and print the rows as JSON."""
    with warehouse_connection() as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(query)
            if cur.description is None:
                output({"rows": [], "row_count": cur.rowcount})
                return
            columns = [c.name for c in cur.description]
            rows = [dict(zip(columns, r, strict=True)) for r in cur.fetchmany(limit)]
            output({"rows": rows, "truncated": cur.fetchone() is not None})


@db_app.command("import")
@guarded
def db_import():
    """Load new inbox CSVs into raw and import their people; every row gets an outcome."""
    from .warehouse_import import WarehouseImporter

    settings = Settings.load()
    with warehouse_connection() as conn:
        output(WarehouseImporter(conn, settings.data_directory, settings.rules()).run())


@db_app.command("review")
@guarded
def db_review():
    """List rows held because they look like an existing person (same name and company)."""
    from .warehouse_import import review_queue

    with warehouse_connection() as conn:
        queue = review_queue(conn)
        output({"open": len(queue), "items": queue})


@db_app.command("resolve")
@guarded
def db_resolve(
    raw_row_id: int,
    action: Annotated[str, typer.Argument(help="merge, new or reject")],
    by: Annotated[str, typer.Option(help="who decided")] = "human",
):
    """Resolve one review item: merge into the candidate, register as new, or reject."""
    from .warehouse_import import resolve

    settings = Settings.load()
    with warehouse_connection() as conn:
        output(resolve(conn, settings.data_directory, settings.rules(), raw_row_id, action, by))


@enrich_app.command("batch")
@guarded
def enrich_batch(
    limit: Annotated[int, typer.Option(min=1, max=500)] = 15,
    layer: Annotated[str | None, typer.Option(help="staging or silver")] = None,
    dry_run: Annotated[bool, typer.Option(help="show the batch without writing a file")] = False,
):
    """Pick people missing a LinkedIn URL or verified email; write a request file."""
    from .enrichment import select_batch, write_request

    settings = Settings.load()
    with warehouse_connection() as conn:
        batch = select_batch(conn, limit, layer)
    summary = {
        "people": len(batch),
        "by_layer": dict(Counter(p["layer"] for p in batch)),
        "needs": dict(Counter(n for p in batch for n in p["needs"])),
    }
    if not dry_run:
        path = write_request(batch, settings.data_directory / "enrichment" / "requests")
        summary["request_file"] = str(path)
    output(summary)


@enrich_app.command("apply")
@guarded
def enrich_apply(
    findings_file: Path,
    dry_run: Annotated[bool, typer.Option(help="validate and show changes only")] = False,
):
    """Apply provider findings (JSON) to the warehouse: provenance + fill blanks + promote."""
    from .enrichment import FileEnricher, apply_findings, match_rate

    findings = FileEnricher(findings_file).enrich([])
    with warehouse_connection() as conn:
        result = apply_findings(conn, findings, dry_run=dry_run)
        if result["run_id"]:
            result["found"] = match_rate(conn, result["run_id"])
    output(result)


@enrich_app.command("run")
@guarded
def enrich_run(
    limit: Annotated[int, typer.Option(min=1, max=500)] = 15,
    layer: Annotated[str | None, typer.Option(help="staging or silver")] = None,
    request: Annotated[Path | None, typer.Option(help="use the people in a request file")] = None,
    only: Annotated[list[str] | None, typer.Option(help="run only these providers")] = None,
    dry_run: Annotated[
        bool, typer.Option(help="list who each provider would be asked about")
    ] = False,
    all_people: Annotated[
        bool,
        typer.Option("--all", help="every non-golden prospect once, in batches of --limit"),
    ] = False,
    resume: Annotated[
        bool,
        typer.Option(help="with --all: skip people an enrich run already asked in the last 24h"),
    ] = False,
    retry_failed: Annotated[
        bool,
        typer.Option(help="retry people whose last attempt failed with a retryable error"),
    ] = False,
):
    """Run the configured waterfall: LinkedIn URL -> work email -> verify.

    Batches are failure-safe: a failed person, provider or batch is recorded in
    ops.enrichment_attempts and skipped. Retry them later with --retry-failed.
    """
    import json as _json

    from .enrichers import build, waterfall_config
    from .enrichment import asked_recently, retry_queue, run_batches, run_waterfall, select_batch

    settings = Settings.load()
    waterfall, providers = waterfall_config(settings.rules())
    enrichers = {n: build(n, providers[n], settings.data_directory) for n in providers}
    selected = set(only) if only else None
    reconnect = lambda: warehouse_connection(attempts=6)  # noqa: E731
    with reconnect() as conn:
        if not (all_people or retry_failed):
            if request:
                ids = [p["prospect_id"] for p in _json.loads(request.read_text())["people"]]
            else:
                ids = [p["prospect_id"] for p in select_batch(conn, limit, layer)]
            output(run_waterfall(conn, ids, waterfall, enrichers, dry_run=dry_run,
                                 only=selected, connect=reconnect))  # fmt: skip
            return
        if retry_failed:
            ids = retry_queue(conn)
        else:
            # Snapshot the list once so people who stay unfound are not asked again.
            ids = [p["prospect_id"] for p in select_batch(conn, 1_000_000, layer)]
            if resume:
                asked = asked_recently(conn)
                ids = [i for i in ids if i not in asked]
        conn.commit()
    result = run_batches(
        reconnect, ids, waterfall, enrichers, limit=limit, dry_run=dry_run, only=selected,
        runs_dir=settings.data_directory / "enrichment" / "runs",
        log=lambda line: typer.echo(line, err=True),
    )  # fmt: skip
    output(result)


@icp_app.command("run")
@guarded
def icp_run(
    dry_run: Annotated[bool, typer.Option(help="show tiers and moves, write nothing")] = False,
):
    """Score every prospect (core.score_snapshots), then apply the ready gate to golden."""
    from . import icp

    settings = Settings.load()
    with warehouse_connection(attempts=3) as conn:
        output(icp.run(conn, settings.rules(), dry_run=dry_run))


@icp_app.command("ready")
@guarded
def icp_ready(limit: Annotated[int, typer.Option(min=1, max=1000)] = 100):
    """List who is ready to contact, best tier and freshest signal first."""
    settings = Settings.load()
    version = settings.rules()["icp"]["model_version"]
    with warehouse_connection(attempts=3) as conn:
        rows = conn.execute(
            "select p.name, p.title, p.company, s.tier, s.total, p.signal_date, p.signal,"
            " p.outreach_angle, p.source_url"
            " from golden.prospects p"
            " join lateral (select tier, total from core.score_snapshots s"
            "   where s.prospect_id = p.prospect_id and s.model_version = %s"
            "   order by snapshot_date desc limit 1) s on true"
            " where p.lifecycle_stage = 'ready'"
            " order by s.tier, p.signal_date desc, s.total desc limit %s",
            (version, limit),
        ).fetchall()
    keys = ("name", "title", "company", "tier", "total", "signal_date", "signal",
            "outreach_angle", "source_url")  # fmt: skip
    output([dict(zip(keys, (str(v) if v is not None else None for v in r), strict=True))
            for r in rows])  # fmt: skip


@app.command("pipeline")
@guarded
def pipeline_command(
    dry_run: Annotated[
        bool, typer.Option(help="plan only: no import, no paid calls, no writes")
    ] = False,
):
    """Daily run: import new Grok files, enrich new ICP people, retry failed lookups, score
    and update ready. A failed step is recorded and the next one still runs."""
    from datetime import datetime

    from .pipeline import run_daily

    settings = Settings.load()

    def log(line: str) -> None:
        typer.echo(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}", err=True)

    result = run_daily(settings, lambda: warehouse_connection(attempts=6), dry_run=dry_run,
                       log=log)  # fmt: skip
    output(result)
    if result["status"] == "failed":
        raise typer.Exit(1)


@schedule_app.command("install")
@guarded
def schedule_install(
    at: Annotated[str | None, typer.Option(help="HH:MM, default pipeline.run_at")] = None,
    dry_run: Annotated[bool, typer.Option(help="show the launchd job, install nothing")] = False,
):
    """Run gtm pipeline every day at a set time (launchd; runs on wake if the Mac slept)."""
    from .schedule import Schedule

    settings = Settings.load()
    run_at = at or settings.rules().get("pipeline", {}).get("run_at", "07:30")
    output(Schedule(Path.cwd()).install(run_at, dry_run=dry_run))


@schedule_app.command("status")
@guarded
def schedule_status():
    """Is the daily job installed, when does it run, and how did the last run exit."""
    from .schedule import Schedule

    output(Schedule(Path.cwd()).status())


@schedule_app.command("uninstall")
@guarded
def schedule_uninstall():
    """Remove the daily job."""
    from .schedule import Schedule

    output(Schedule(Path.cwd()).uninstall())


outbound_app = typer.Typer(
    help="Signal-based email: pick today's people, store drafts, record confirmed sends."
    " Nothing here sends."
)
app.add_typer(outbound_app, name="outbound")


@outbound_app.command("pick")
@guarded
def outbound_pick(
    per_segment: Annotated[int, typer.Option(min=1, max=200)] = 25,
    list_name: Annotated[
        str | None, typer.Option("--list", help="pick from a named list, e.g. old-employer-email")
    ] = None,
):
    """Best founders and VPs (one per company) with the signal and offer to use: from the
    ready list, or from a named list for its own campaign."""
    from . import outbound

    settings = Settings.load()
    version = settings.rules()["icp"]["model_version"]
    with warehouse_connection(attempts=3) as conn:
        output(outbound.pick(conn, version, per_segment, list_name))


@outbound_app.command("lists")
@guarded
def outbound_lists(name: Annotated[str | None, typer.Argument()] = None):
    """Outbound lists kept apart from ready (counts), or one list's active members."""
    from . import outbound

    with warehouse_connection(attempts=3) as conn:
        output(outbound.list_members(conn, name) if name else outbound.lists(conn))


@outbound_app.command("save")
@guarded
def outbound_save(path: Path):
    """Store drafts from a JSON list (prospect_id, signal_id, offer, company, subject,
    body). Drafts that break the house style are rejected and listed."""
    from . import outbound

    items = json.loads(path.read_text())
    with warehouse_connection(attempts=3) as conn:
        output(outbound.save_drafts(conn, items))


@outbound_app.command("list")
@guarded
def outbound_list(status: Annotated[str | None, typer.Option()] = None):
    """Messages and their status (draft, sent, ...)."""
    from . import outbound

    with warehouse_connection(attempts=3) as conn:
        output(outbound.drafts(conn, status))


@outbound_app.command("link")
@guarded
def outbound_link(message_id: str, thread_id: str):
    """Remember the Gmail thread a draft was placed in."""
    from . import outbound

    with warehouse_connection(attempts=3) as conn:
        outbound.link_draft(conn, message_id, thread_id)
    output({"message_id": message_id, "thread_id": thread_id})


@outbound_app.command("sent")
@guarded
def outbound_sent(message_id: str, external_id: Annotated[str | None, typer.Option()] = None):
    """Record a send confirmed in Gmail: person -> in_sequence, follow-up task in 4 days."""
    from . import outbound

    with warehouse_connection(attempts=3) as conn:
        output(outbound.mark_sent(conn, message_id, external_id=external_id))

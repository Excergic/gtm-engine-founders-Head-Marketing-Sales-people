"""Local CSV CRM with optional research-agent execution; no automated outreach."""

from __future__ import annotations

import csv
import functools
import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from .agent import (
    ResearchBatch,
    agent_schema,
    prompt_for,
    run_research,
    validate_batch,
    validation_issues,
)
from .config import Settings
from .inbox import GrokInbox
from .models import ConnectionStatus, Objective, Prospect, Source, normalize_domain, utcnow
from .research import ResearchStore, atomic_json, profile_hash, research_queue
from .scheduler import MacScheduler, validate_plist
from .service import GTMService, stable_key, tokens
from .state import State
from .store import CSVStore, truth
from .workflows import (
    apply_reports,
    enrichment_queue,
    outreach_summary,
    portfolio_metrics,
    ranked,
    score_summary,
)

app = typer.Typer(
    no_args_is_help=True, help="Simple local GTM tracker. Your master CSV is the record of truth."
)
imports = typer.Typer(help="Import Grok or authorized LinkedIn CSV files locally.")
app.add_typer(imports, name="import")
scheduler_app = typer.Typer(help="Optional macOS local CSV import every 15 minutes.")
app.add_typer(scheduler_app, name="scheduler")
research_app = typer.Typer(help="Evidence-backed account research; no automatic outreach.")
app.add_typer(research_app, name="research")


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
            if any(
                error["loc"] and error["loc"][0] == "accounts"
                for error in exc.errors(include_input=False, include_context=False)
            ):
                detail = "; ".join(
                    issue["path"] + ": " + issue["reason"] for issue in validation_issues(exc)
                )
                typer.echo("Error: research validation failed: " + detail, err=True)
                raise typer.Exit(1) from exc
            # Validation messages can include invalid prospect values; output structural paths only.
            fields = [".".join(map(str, e["loc"])) for e in exc.errors(include_input=False)]
            typer.echo("Error: invalid values in fields: " + ", ".join(fields), err=True)
            raise typer.Exit(1) from exc
        except (ValueError, RuntimeError, OSError, csv.Error, sqlite3.Error) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc

    return wrapped


@contextmanager
def runtime(*, read_only=False):
    settings = Settings.load()
    state = State(settings.database, read_only=read_only)
    try:
        yield (
            GTMService(
                CSVStore(settings.tracker),
                state,
                settings.rules(),
                research=ResearchStore(settings.data_directory / "research"),
            ),
            settings,
        )
    finally:
        state.close()


def summary_exit(result):
    output(result)
    if any(
        any(file.get(key, 0) for key in ("failed", "invalid", "review")) for file in result["files"]
    ):
        raise typer.Exit(2)


def do_import(source, file, all_files, dry_run, retry=False):
    if file and all_files:
        raise ValueError("Use a file path or --all, not both")
    with runtime(read_only=dry_run) as (service, settings):
        if source == Source.grok:
            summary_exit(
                GrokInbox(service, settings).run(
                    [file] if file else None, dry_run=dry_run, retry_only=retry
                )
            )
        else:
            paths = [file] if file else sorted(settings.linkedin_directory.glob("*.csv"))
            files = []
            seen = {}
            for path in paths:
                if dry_run:
                    files.append(service.preview_rows(path, source, seen=seen))
                else:
                    files.append(service.import_file(path, source))
            summary_exit({"dry_run": dry_run, "files": files})


@imports.command("grok")
@guarded
def import_grok(
    file: Annotated[
        Path | None, typer.Argument(help="CSV path; defaults to the configured inbox.")
    ] = None,
    all_files: Annotated[bool, typer.Option("--all", help="Process the Grok inbox.")] = False,
    dry_run: bool = False,
):
    """Import to the local master CSV. --dry-run previews creates/updates without writing."""
    do_import(Source.grok, file, all_files, dry_run)


@imports.command("linkedin")
@guarded
def import_linkedin(
    file: Annotated[Path | None, typer.Argument()] = None,
    all_files: Annotated[bool, typer.Option("--all")] = False,
    dry_run: bool = False,
):
    """Import manually prepared or authorized CSV data; no LinkedIn automation."""
    do_import(Source.linkedin, file, all_files, dry_run)


@imports.command("history")
@guarded
def import_history():
    with runtime(read_only=True) as (service, _):
        output(service.state.history())


@imports.command("retry-failed")
@guarded
def retry_failed():
    do_import(Source.grok, None, False, False, retry=True)


@imports.command("review")
@guarded
def review():
    with runtime(read_only=True) as (service, _):
        output(
            {
                "import_rows": service.state.problems(("review", "invalid", "failed")),
                "prospects": [public(r) for r in service.store.load() if truth(r["needs_review"])],
            }
        )


def public(row):
    return {
        k: v
        for k, v in row.items()
        if k not in {"source_data", "activity_history", "completed_actions"}
    }


@app.command("people")
@guarded
def people():
    """List prospect IDs and editable fields."""
    with runtime(read_only=True) as (service, _):
        output([public(row) for row in service.store.load()])


@app.command("add-prospect")
@guarded
def add_prospect(
    name: Annotated[str, typer.Option()],
    company: str = "",
    website: str = "",
    linkedin_url: str = "",
    email: str = "",
    source: Source = Source.linkedin,
    objective: Objective = Objective.consulting_client,
):
    """Capture a person manually; incomplete identities are flagged rather than rejected."""
    p = Prospect(
        full_name=name,
        company_name=company,
        website=website,
        linkedin_url=linkedin_url,
        email=email,
        source=source,
        objective=objective,
    )
    with runtime() as (service, _), service.state.lock():
        rows = service.store.load()
        observation = {
            "key": stable_key("manual", str(utcnow())),
            "source": str(source),
            "discovered_at": p.discovered_at.isoformat(),
            "data": {"manual": True},
        }
        row, action = service.merge(rows, p, observation)
        service.write_rows(rows)
        output({"action": action, "prospect": public(row)})


@app.command("update-status")
@guarded
def update_status(
    person_id: str,
    status: ConnectionStatus,
    followup: Annotated[str | None, typer.Option(help="ISO date, e.g. 2026-11-20.")] = None,
    notes: str | None = None,
):
    """Record an observed connection status. Find person IDs with gtm people."""
    with runtime() as (service, _), service.state.lock():
        output(public(service.update(person_id, status=status, followup=followup, notes=notes)))


@app.command("log-activity")
@guarded
def log_activity(
    person_id: str,
    notes: Annotated[str, typer.Option()],
    kind: str = "note",
    followup: str | None = None,
    do_not_contact: bool | None = None,
):
    """Append manual activity; optionally set a follow-up or do-not-contact flag."""
    with runtime() as (service, _), service.state.lock():
        output(
            public(
                service.update(
                    person_id,
                    notes=notes,
                    followup=followup,
                    activity=kind,
                    do_not_contact=do_not_contact,
                )
            )
        )


@app.command("opportunities")
@guarded
def opportunities(
    person_id: str | None = None, objective: Objective | None = None, stage: str | None = None
):
    """Show or set the consulting/job stage columns in the master CSV."""
    with runtime(read_only=stage is None) as (service, _):
        if stage:
            if not person_id or not objective:
                raise ValueError("Updating a stage requires --person-id and --objective")
            with service.state.lock():
                output(public(service.update(person_id, objective=objective, stage=stage)))
        else:
            output(
                [public(r) for r in service.store.load() if r["consulting_stage"] or r["job_stage"]]
            )


@app.command("followups")
@guarded
def followups():
    """Show due actions; never send a message."""
    with runtime(read_only=True) as (service, _):
        output(service.actions())


@app.command("complete-action")
@guarded
def complete_action(action_id: str):
    with runtime() as (service, _), service.state.lock():
        service.complete(action_id)
        output({"completed": action_id})


@app.command("daily")
@guarded
def daily():
    """Summarize actual local prospects, opportunity stages, and today's actions."""
    with runtime(read_only=True) as (service, settings):
        rows = service.store.load()
        actions = service.actions()
        closed = service.rules["pipelines"]["closed"]
        output(
            {
                "title": "Daily GTM Summary",
                "master_csv": str(settings.tracker.absolute()),
                "total_people": len(rows),
                "reported_company_domains": len({r["domain"] for r in rows if r["domain"]}),
                "by_source": {
                    source: sum(source in tokens(r["sources"]) for r in rows) for source in Source
                },
                "by_segment": {
                    segment: sum(segment in tokens(r.get("segments")) for r in rows)
                    for segment in ("founders", "VPs")
                },
                "prospects_needing_review": sum(truth(r["needs_review"]) for r in rows),
                "consulting_opportunities_active": sum(
                    bool(r["consulting_stage"]) and r["consulting_stage"] not in closed
                    for r in rows
                ),
                "job_opportunities_active": sum(
                    bool(r["job_stage"]) and r["job_stage"] not in closed for r in rows
                ),
                "outreach": outreach_summary(rows),
                "scoring": score_summary(ranked(service)),
                "priority_prospects": [
                    r for r in ranked(service) if r["tier"] in {"tier_1", "tier_2"}
                ][:10],
                "actions_due": len(actions),
                "top_10_actions": actions[:10],
            }
        )


@app.command("health")
@guarded
def health():
    """Validate local CSV and SQLite state; no credentials or internet required."""
    with runtime(read_only=True) as (service, settings):
        rows = service.store.load()
        output(
            {
                "mode": "local_csv",
                "master_csv": str(settings.tracker.absolute()),
                "master_exists": settings.tracker.exists(),
                "people": len(rows),
                **service.state.health(),
            }
        )


def configured_scheduler():
    settings = Settings.load()
    return MacScheduler(Path.cwd(), settings.data_directory / "state/logs")


@scheduler_app.command("install")
@guarded
def scheduler_install(dry_run: bool = False, output_path: Path | None = None):
    """Install only when explicitly invoked. --dry-run generates a preview only."""
    agent = configured_scheduler()
    if dry_run:
        content = validate_plist(agent.spec)
        if output_path:
            if output_path.resolve().is_relative_to(agent.path.parent.resolve()):
                raise ValueError("Write scheduler previews outside Library/LaunchAgents")
            if output_path.exists():
                raise ValueError("Preview output already exists; choose a new path")
            output_path.write_bytes(content)
        output({"dry_run": True, "configuration": agent.spec})
    else:
        if output_path:
            raise ValueError("--output-path is only for --dry-run")
        output(agent.install())


@scheduler_app.command("status")
@guarded
def scheduler_status():
    output(configured_scheduler().status())


@scheduler_app.command("uninstall")
@guarded
def scheduler_uninstall():
    output(configured_scheduler().uninstall())


@app.command("score")
@guarded
def score_command(
    write: bool = False, tier: str | None = None, limit: int = typer.Option(20, min=1)
):
    """Rank all prospects using cached research; --write adds score columns to the master."""
    with runtime(read_only=not write) as (service, _):
        if write:
            with service.state.lock():
                service.write_rows(service.store.load())
        results = ranked(service)
        selected = [r for r in results if tier is None or r["tier"] == tier]
        output({**score_summary(results), "written": write, "results": selected[:limit]})


@app.command("event")
@guarded
def event_command(
    person_id: str,
    event: str,
    notes: str | None = None,
    followup: str | None = None,
    event_id: str | None = None,
):
    """Record a manually observed connection_sent, connection_accepted, reply, etc."""
    with runtime() as (service, _), service.state.lock():
        output(
            public(
                service.record_event(
                    person_id, event, notes=notes, followup=followup, event_id=event_id
                )
            )
        )


@research_app.command("queue")
@guarded
def research_queue_command(limit: int = typer.Option(10, min=1)):
    with runtime(read_only=True) as (service, _):
        queue = research_queue(service.store.load(), service.research, service.rules["scoring"])
        output({"accounts_needing_research": len(queue), "accounts": queue[:limit]})


@research_app.command("show")
@guarded
def research_show(domain: str):
    with runtime(read_only=True) as (service, _):
        report = service.research.load(domain)
        output(report.model_dump(mode="json") if report else {"status": "needs_research"})


@research_app.command("prepare")
@guarded
def research_prepare(output_directory: Path, limit: int = typer.Option(5, min=1, max=10)):
    """Create a public-company research brief and JSON schema for any research agent."""
    if output_directory.exists():
        raise ValueError("Choose a new output directory; existing briefs are immutable")
    with runtime(read_only=True) as (service, _):
        queue = research_queue(service.store.load(), service.research, service.rules["scoring"])[
            :limit
        ]
        atomic_json(output_directory / "schema.json", agent_schema())
        atomic_json(output_directory / "accounts.json", queue)
        (output_directory / "prompt.txt").write_text(prompt_for(queue, service.rules["scoring"]))
        output({"accounts": len(queue), "directory": str(output_directory), "agent_started": False})


@research_app.command("apply")
@guarded
def research_apply(file: Path, dry_run: bool = False):
    """Validate structured research, retain receipts, and refresh scores. No contact-status changes."""
    batch = ResearchBatch.model_validate_json(file.read_text())
    if len({a.domain for a in batch.accounts}) != len(batch.accounts):
        raise ValueError("Research batch has duplicate domains")
    with runtime(read_only=dry_run) as (service, _):
        if dry_run:
            output(apply_reports(service, batch.accounts, dry_run=True))
        else:
            with service.state.lock():
                output(apply_reports(service, batch.accounts))


@research_app.command("run")
@guarded
def research_run(
    limit: int = typer.Option(3, min=1, max=10), dry_run: bool = False, domain: str | None = None
):
    """Optional Codex web research batch. Uses Codex login/allowance; never runs from import/scheduler."""
    with runtime(read_only=dry_run) as (service, settings):
        queue = research_queue(
            service.store.load(),
            service.research,
            service.rules["scoring"],
            include_current=domain is not None,
        )
        if domain:
            queue = [account for account in queue if account["domain"] == normalize_domain(domain)]
            if not queue:
                raise ValueError("Account is unknown or all its contacts are excluded")
        queue = queue[:limit]
        if dry_run or not queue:
            output(
                {
                    "dry_run": dry_run,
                    "accounts": len(queue),
                    "domains": [a["domain"] for a in queue],
                    "agent_started": False,
                }
            )
            return
        typer.echo(
            f"Researching {len(queue)} accounts with Codex; this may take several minutes.",
            err=True,
        )
        batch, run_dir = run_research(
            queue, service.rules["scoring"], settings.data_directory / "research"
        )
        with service.state.lock():
            # Goals might have been edited during the external run; validate against current policy.
            service.rules = settings.rules()
            result = apply_reports(service, batch.accounts)
            atomic_json(
                run_dir / "status.json",
                {"status": "applied", "results_applied": True, "accounts": len(batch.accounts)},
            )
        output(result)


@research_app.command("resume")
@guarded
def research_resume(run_id: str, dry_run: bool = False):
    """Validate/apply a saved run after a local fix. Never starts an agent."""
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ValueError("Use the 32-character run ID from data/research/runs/")
    with runtime(read_only=dry_run) as (service, settings):
        run_dir = settings.data_directory / "research" / "runs" / run_id
        request = json.loads((run_dir / "request.json").read_text())
        policy = service.rules["scoring"]
        if request.get("profile_hash") != profile_hash(policy):
            raise ValueError(
                "Saved run uses different goals; research again for the current profile"
            )
        requested = [{"domain": domain} for domain in request["domains"]]
        batch = validate_batch(json.loads((run_dir / "result.json").read_text()), requested, policy)
        if dry_run:
            output(apply_reports(service, batch.accounts, dry_run=True))
        else:
            with service.state.lock():
                result = apply_reports(service, batch.accounts)
                atomic_json(
                    run_dir / "status.json",
                    {
                        "status": "applied",
                        "results_applied": True,
                        "accounts": len(batch.accounts),
                        "resumed": True,
                    },
                )
            output(result)


@app.command("enrichment-queue")
@guarded
def enrichment_queue_command(person_id: str | None = None):
    """Suggest targeted enrichment only after qualification; no provider calls."""
    with runtime(read_only=True) as (service, _):
        candidates = enrichment_queue(service)
        output([r for r in candidates if person_id is None or r["id"] == person_id])


@app.command("portfolio-report")
@guarded
def portfolio_report(output_path: Path | None = None):
    """Export only aggregate measurements suitable for a public case study."""
    with runtime(read_only=True) as (service, _):
        report = portfolio_metrics(service)
        if output_path:
            if output_path.exists():
                raise ValueError("Choose a new portfolio report path")
            atomic_json(output_path, report)
        output(report)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    app()

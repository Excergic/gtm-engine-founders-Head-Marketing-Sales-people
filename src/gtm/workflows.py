"""Scoring/research orchestration with explicit writes and safe portfolio aggregates."""

from __future__ import annotations

import json
from collections import Counter

from .models import utcnow
from .research import profile_hash
from .scoring import SCORE_COLUMNS, score_rows
from .store import truth


def ranked(service):
    if not service.research:
        raise ValueError("Research store is not configured")
    results = score_rows(service.store.load(), service.research, service.rules["scoring"])

    def order(row):
        priorities = [v for v in (row["full_time_priority"], row["contract_priority"]) if v != ""]
        return (row["tier"] or "tier_4", -max(priorities, default=-1), row["id"])

    return sorted(results, key=order)


def score_summary(results):
    return {
        "people": len(results),
        "tiers": dict(Counter(r["tier"] or "unscored" for r in results)),
        "statuses": dict(Counter(r["score_status"] for r in results)),
        "model_state": "exploratory_end_to_end",
        "note": "Priority hypotheses; not hiring probabilities or validated conversion predictions.",
    }


def apply_reports(service, reports, *, dry_run=False):
    rows = service.store.load()
    domains = {r["domain"] for r in rows if r.get("domain")}
    policy = service.rules["scoring"]
    now = utcnow()
    # Validate the entire batch before any write; later IO failures remain retryable and idempotent.
    for report in reports:
        if report.domain not in domains:
            raise ValueError("Research domain is outside this tracker")
        if report.profile_hash != profile_hash(policy):
            raise ValueError("Profile changed; research must use current role goals")
        if report.researched_at > now or any(
            e.retrieved_at > now or (e.published_at and e.published_at > now)
            for e in report.evidence
        ):
            raise ValueError("Future research/evidence timestamp")
        previous = service.research.load(report.domain)
        if previous and previous.researched_at > report.researched_at:
            raise ValueError("Cannot replace newer research with older research")
    if dry_run:
        return {"dry_run": True, "accounts": len(reports), "domains": [r.domain for r in reports]}
    receipts = []
    for report in reports:
        receipts.append(str(service.research.save(report)))
    results = score_rows(rows, service.research, policy)
    for row, result in zip(rows, results, strict=True):
        row.update({key: result[key] for key in SCORE_COLUMNS})
    service.store.write(rows)
    service.state.audit("research_applied", "accounts", None, count=len(reports))
    return {
        "dry_run": False,
        "accounts": len(reports),
        "receipts": receipts,
        **score_summary(results),
    }


def enrichment_queue(service):
    scores = {r["id"]: r for r in ranked(service)}
    output = []
    for row in service.store.load():
        result = scores[row["id"]]
        if truth(row.get("do_not_contact")) or result["tier"] not in {"tier_1", "tier_2"}:
            continue
        missing = [field for field in ("email", "linkedin_url") if not row.get(field)]
        if missing:
            output.append(
                {
                    "id": row["id"],
                    "name": row["full_name"],
                    "company": row["company_name"],
                    "domain": row["domain"],
                    "tier": result["tier"],
                    "missing": missing,
                    "recommendation": "Enrich only the field needed for your next authorized outreach step",
                    "automatic_provider_call": False,
                }
            )
    return output


def portfolio_metrics(service):
    rows = service.store.load()
    results = ranked(service)
    history = [event for row in rows for event in json.loads(row["activity_history"] or "[]")]
    allowed_events = {
        "manual_update",
        "connection_sent",
        "connection_accepted",
        "outreach_sent",
        "reply",
        "positive_reply",
        "meeting_booked",
        "meeting_completed",
        "note",
        "do_not_contact",
    }
    event_counts = Counter(
        e.get("type") if e.get("type") in allowed_events else "other_manual_activity"
        for e in history
    )
    return {
        "generated_at": utcnow().isoformat(),
        "population_people": len(rows),
        "reported_accounts": len({r["domain"] for r in rows if r.get("domain")}),
        "scores": score_summary(results),
        "observed_manual_events": dict(event_counts),
        "outreach": outreach_summary(rows),
        "claims": {
            "automated_messages_sent": 0,
            "predictive_validation": "not_established",
            "placement_or_revenue_impact": "not_established",
        },
        "privacy": "Aggregate counts only; no prospect names, domains, emails, notes, or quotes.",
    }


def outreach_summary(rows):
    """Distinct-person denominators from actual logged events, never inferred from research."""
    sent, accepted, outreach, positive = set(), set(), set(), set()
    for row in rows:
        for event in json.loads(row.get("activity_history") or "[]"):
            kind, status = event.get("type"), event.get("status")
            if kind == "connection_sent" or status == "connection_sent":
                sent.add(row["id"])
            if kind == "connection_accepted" or status == "connected":
                accepted.add(row["id"])
            if kind == "outreach_sent":
                outreach.add(row["id"])
            if kind == "positive_reply":
                positive.add(row["id"])
    return {
        "connection_statuses": dict(Counter(r["connection_status"] for r in rows)),
        "people_with_logged_requests": len(sent),
        "people_with_logged_acceptances": len(accepted),
        "connection_acceptance_rate": len(sent & accepted) / len(sent) if sent else None,
        "people_with_logged_outreach": len(outreach),
        "people_with_logged_positive_replies": len(positive),
        "positive_reply_rate": len(outreach & positive) / len(outreach) if outreach else None,
        "measurement": "Distinct people with logged request/outreach events; unknown denominators are null.",
    }

"""ICP scoring and the ready gate, run on the warehouse.

Golden says the data is good (verified email + LinkedIn). Ready says *contact them now*:

    golden + verified email + not do-not-contact + lead_status open
    + fresh signal (<= icp.signal_max_age_days) + tier in icp.ready.tiers
    + an outreach angle + a source URL the angle can cite

Scores are written to ``core.score_snapshots`` (one per person per day per model
version). Lifecycle changes go through ``ops.transition`` so each one is logged in
``ops.state_transitions``. Nothing here sends anything.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date

import psycopg
from psycopg.types.json import Jsonb

# States the gate may move between. Later states (in_sequence, replied, ...) belong to
# outbound and are never touched here.
PRE_READY = ("scraped", "researched", "enriched", "silver_waiting", "needs_manual", "golden")
GATED = PRE_READY + ("ready",)


def _has_word(text: str, terms) -> bool:
    return any(re.search(rf"(?<![a-z]){re.escape(t.casefold())}(?![a-z])", text) for t in terms)


def matches_rule(title: str, rule: dict | None) -> bool:
    """Seniority x function title rule, e.g. 'SVP Global Sales' or 'Head of B2B Marketing'."""
    if not rule:
        return False
    if _has_word(title, rule.get("exclude", [])):
        return False
    if _has_word(title, rule.get("seniority_alone", [])):
        return True
    return _has_word(title, rule.get("seniority", [])) and _has_word(
        title, rule.get("function", [])
    )


def segment(title: str | None, icp: dict) -> str | None:
    """'founder', 'gtm_leader' or None (outside the ICP)."""
    # "Vice President" is a seniority, not the "President" title: read it as "vp" so a
    # bare "Vice President" needs a function to qualify, like any other VP.
    text = re.sub(r"\bvice[\s-]+president\b", "vp", (title or "").casefold())
    if not text:
        return None
    if _has_word(text, icp["founder_titles"]):
        return "founder"  # "Founder & Managing Director" is still a founder
    if _has_word(text, icp["exclude_titles"]):
        return None
    if _has_word(text, icp.get("gtm_leader_titles", [])) or matches_rule(
        text, icp.get("gtm_leader_rule")
    ):
        return "gtm_leader"
    return None


def signal_age(signal_date: str | None, today: date) -> int | None:
    if not signal_date or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", signal_date):
        return None
    return (today - date.fromisoformat(signal_date)).days


def score_person(person: dict, icp: dict, today: date) -> dict:
    weights = icp["weights"]
    reasons: list[str] = []

    seg = segment(person.get("title"), icp)
    lead_fit = weights["lead_fit"] if seg else 0
    reasons.append(f"title {person.get('title')!r}: " + (seg or "outside the ICP"))

    age = signal_age(person.get("signal_date"), today)
    qualifying = _has_word((person.get("signal") or "").casefold(), icp["qualifying_signals"])
    if age is None:
        intent = 0
        reasons.append("no exact signal date")
    elif age > icp["signal_max_age_days"]:
        intent = 0
        reasons.append(f"signal is {age} days old")
    else:
        intent = weights["intent"] if qualifying else weights["intent"] // 2
        reasons.append(
            f"signal {age} days old, " + ("qualifying kind" if qualifying else "unclassified kind")
        )

    # No ICP account research exists yet: unscored, not zero.
    account_fit = None
    reasons.append("account fit unscored (no ICP account research)")

    if person.get("reply_status") == "positive" or person.get("connection_status") == "connected":
        engagement = weights["engagement"]
    elif person.get("connection_status") == "requested":
        engagement = weights["engagement"] // 2
    else:
        engagement = 0

    total = lead_fit + intent + engagement + (account_fit or 0)
    if not seg:
        tier = "tier_3"  # outside the ICP never ranks up, whatever else it has
    elif total >= icp["tier1_min"]:
        tier = "tier_1"
    elif total >= icp["tier2_min"]:
        tier = "tier_2"
    else:
        tier = "tier_3"
    return {"segment": seg, "lead_fit": lead_fit, "intent": intent, "account_fit": account_fit,
            "engagement": engagement, "total": total, "tier": tier, "signal_age": age,
            "reasons": reasons}  # fmt: skip


def ready_blockers(person: dict, score: dict, icp: dict) -> list[str]:
    """Why a person is not ready; empty means ready."""
    blockers = []
    if person["layer"] != "golden":
        blockers.append("not golden")
    if person.get("email_status") != "verified":
        blockers.append("email not verified")
    if person.get("do_not_contact"):
        blockers.append("do not contact")
    if person.get("lead_status") != "open":
        blockers.append(f"lead_status {person.get('lead_status')}")
    age = score["signal_age"]
    if age is None or age > icp["signal_max_age_days"]:
        blockers.append("signal not fresh")
    if score["tier"] not in icp["ready"]["tiers"]:
        blockers.append(f"{score['tier']}" + ("" if score["segment"] else " (outside ICP)"))
    if not (person.get("outreach_angle") or "").strip():
        blockers.append("no outreach angle")
    if not (person.get("source_url") or "").strip():
        blockers.append("no source to cite")
    return blockers


COLUMNS = ("prospect_id", "layer", "name", "title", "company", "signal", "signal_date",
           "source_url", "outreach_angle", "email_status", "do_not_contact", "lead_status",
           "connection_status", "reply_status", "lifecycle_stage")  # fmt: skip


def run(conn: psycopg.Connection, rules: dict, *, today: date | None = None,
        dry_run: bool = False) -> dict:  # fmt: skip
    """Score everyone, then move golden people in or out of ready."""
    icp = rules["icp"]
    today = today or date.today()
    conn.autocommit = True
    people = [dict(zip(COLUMNS, row, strict=True)) for row in conn.execute(
        "select " + ", ".join(COLUMNS) + " from core.all_prospects")]  # fmt: skip

    run_id = None
    if not dry_run:
        run_id = conn.execute(
            "insert into ops.pipeline_runs (job, details) values ('score', %s) returning run_id",
            (Jsonb({"model_version": icp["model_version"], "people": len(people)}),),
        ).fetchone()[0]

    tiers: Counter = Counter()
    blocked: Counter = Counter()
    moves: Counter = Counter()
    ready_people = []
    for person in people:
        score = score_person(person, icp, today)
        tiers[score["tier"]] += 1
        blockers = ready_blockers(person, score, icp)
        # Only people the gate can move are counted; outbound states are left alone.
        if person["layer"] == "golden" and person["lifecycle_stage"] in GATED:
            for b in blockers:
                blocked[b] += 1
            if not blockers:
                ready_people.append(person["name"])

        stage = person["lifecycle_stage"]
        target = None
        if stage in GATED and person["layer"] == "golden":
            target = "ready" if not blockers else "golden"
        if dry_run:
            if target and target != stage:
                moves[f"{stage} -> {target}"] += 1
            continue

        with conn.transaction():
            conn.execute(
                "insert into core.score_snapshots (prospect_id, snapshot_date, account_fit,"
                " lead_fit, intent, engagement, total, tier, model_version, reasons)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " on conflict (prospect_id, snapshot_date, model_version) do update set"
                " account_fit = excluded.account_fit, lead_fit = excluded.lead_fit,"
                " intent = excluded.intent, engagement = excluded.engagement,"
                " total = excluded.total, tier = excluded.tier, reasons = excluded.reasons",
                (person["prospect_id"], today, score["account_fit"], score["lead_fit"],
                 score["intent"], score["engagement"], score["total"], score["tier"],
                 icp["model_version"], Jsonb(score["reasons"] + [f"ready blockers: {blockers}"])),
            )  # fmt: skip
            if target and target != stage:
                reason = "ready gate passed" if target == "ready" else (
                    "ready gate: " + "; ".join(blockers))  # fmt: skip
                conn.execute("select ops.transition(%s, %s, %s, 'system', %s)",
                             (person["prospect_id"], target, run_id, reason))  # fmt: skip
                moves[f"{stage} -> {target}"] += 1

    if run_id:
        conn.execute(
            "update ops.pipeline_runs set finished_at = now(), status = 'succeeded',"
            " rows_in = %s, rows_ok = %s, details = details || %s where run_id = %s",
            (len(people), len(people), Jsonb({"tiers": tiers, "moves": moves}), run_id),
        )
    golden = sum(1 for p in people if p["layer"] == "golden")
    return {"run_id": str(run_id) if run_id else None, "dry_run": dry_run,
            "model_version": icp["model_version"], "scored": len(people),
            "tiers": dict(tiers), "golden": golden, "ready": len(ready_people),
            "golden_not_ready_because": dict(blocked.most_common()),
            "lifecycle_moves": dict(moves)}  # fmt: skip

"""Versioned evidence-assisted priority policy. Scores are hypotheses, not probabilities."""

from __future__ import annotations

import json
from datetime import timedelta

from .models import timestamp, utcnow
from .research import digest, profile_hash
from .store import truth

SCORE_COLUMNS = (
    "account_fit_score",
    "lead_fit_score",
    "hiring_intent_score",
    "contract_intent_score",
    "engagement_score",
    "full_time_priority",
    "contract_priority",
    "tier",
    "full_time_tier",
    "contract_tier",
    "score_status",
    "score_reasons",
    "research_updated_at",
    "evidence_coverage",
    "recommended_next_action",
    "score_model_version",
)


def validate_policy(policy):
    if not isinstance(policy, dict) or not isinstance(policy.get("profile"), dict):
        raise ValueError("scoring.profile must be configured")
    if not policy.get("model_version"):
        raise ValueError("scoring.model_version is required")
    for field in ("research_max_age_days", "engagement_max_age_days"):
        if type(policy.get(field)) is not int or policy[field] <= 0:
            raise ValueError(f"scoring.{field} must be positive")
    if not 0 <= policy.get("tier2_min", -1) < policy.get("tier1_min", -1) <= 100:
        raise ValueError("Scoring thresholds must satisfy 0 <= tier2 < tier1 <= 100")
    for family, keys in {
        "account_fit": ("strong", "moderate", "weak"),
        "intent": ("strong", "moderate", "none"),
        "lead_fit": ("decision_maker", "relevant", "other"),
        "engagement": ("positive_reply", "replied", "connected", "none"),
    }.items():
        values = policy.get("weights", {}).get(family, {})
        if set(values) != set(keys) or any(type(v) is not int or v < 0 for v in values.values()):
            raise ValueError(f"Invalid scoring weights: {family}")
    if sum(max(v.values()) for v in policy["weights"].values()) != 100:
        raise ValueError("Scoring component maxima must total 100")
    for field in ("decision_maker_titles", "relevant_titles"):
        if not isinstance(policy.get(field), list) or any(
            not isinstance(v, str) for v in policy[field]
        ):
            raise ValueError(f"scoring.{field} must list title phrases")
    return policy


def research_state(report, policy, now):
    if report is None:
        return "needs_research"
    if report.profile_hash != profile_hash(policy):
        return "profile_changed"
    if report.researched_at > now:
        return "future_research"
    if now - report.researched_at > timedelta(days=policy["research_max_age_days"]):
        return "stale_research"
    if report.status == "failed":
        return "research_failed"
    if not report.identity_confirmed:
        return "identity_unconfirmed"
    return "current"


def active_finding(report, feature, policy, now):
    finding = report.findings.get(feature)
    unknown = "uncertain" if feature.endswith("_remote") else "unknown"
    if not finding:
        return unknown
    if finding.value in {"unknown", "uncertain"}:
        return finding.value
    evidence = [e for e in report.evidence if e.id in finding.evidence_ids]
    valid = [
        e
        for e in evidence
        if timedelta(0) <= now - e.retrieved_at <= timedelta(days=policy["research_max_age_days"])
        and (not e.published_at or e.published_at <= now)
    ]
    # Third-party assertions alone cannot establish current hiring or remote eligibility.
    if feature in {"full_time_remote", "contract_remote", "hiring", "contracting"}:
        valid = [e for e in valid if e.source_type in {"official", "job_listing"}]
    if feature in {"hiring", "contracting"}:
        valid = [
            e
            for e in valid
            if not e.published_at
            or now - e.published_at <= timedelta(days=policy["research_max_age_days"])
        ]
    if feature.endswith("_remote"):
        location_known = policy["profile"].get("work_location") not in {None, "", "unspecified"}
        if (
            finding.value == "eligible"
            and finding.eligibility_scope != "worldwide"
            and not (location_known and finding.eligibility_scope == "candidate_location")
        ):
            return "uncertain"
        if (
            finding.value == "ineligible"
            and not location_known
            and finding.eligibility_scope != "onsite_only"
        ):
            return "uncertain"
    return finding.value if valid else unknown


def lead_fit(row, policy):
    title = row.get("job_title", "").casefold()
    # Only a provisional contact-role heuristic; never used to infer company hiring.
    group = "other"
    if any(term.casefold() in title for term in policy["decision_maker_titles"]):
        group = "decision_maker"
    elif any(term.casefold() in title for term in policy["relevant_titles"]):
        group = "relevant"
    return policy["weights"]["lead_fit"][group]


def engagement(row, policy, now):
    observed = row.get("status_changed_at") or row.get("last_contact_at")
    if not observed or not timedelta(0) <= now - timestamp(observed) <= timedelta(
        days=policy["engagement_max_age_days"]
    ):
        return 0
    status = row.get("connection_status")
    history = json.loads(row.get("activity_history") or "[]")
    positive = any(
        event.get("type") == "positive_reply"
        and event.get("timestamp")
        and timedelta(0)
        <= now - timestamp(event["timestamp"])
        <= timedelta(days=policy["engagement_max_age_days"])
        for event in history
    )
    group = (
        "positive_reply"
        if status == "meeting_booked" or (status == "replied" and positive)
        else "replied"
        if status == "replied"
        else "connected"
        if status in {"connected", "conversation_started"}
        else "none"
    )
    return policy["weights"]["engagement"][group]


def score(row, report, policy, *, now=None):
    now = now or utcnow()
    version = policy["model_version"] + ":" + digest(policy)[:12]
    result = dict.fromkeys(SCORE_COLUMNS, "")
    result.update(
        score_model_version=version,
        score_status=research_state(report, policy, now),
        lead_fit_score=lead_fit(row, policy),
        engagement_score=engagement(row, policy, now),
        recommended_next_action="Research account and remote eligibility",
        evidence_coverage="0/5",
    )
    if report:
        result["research_updated_at"] = report.researched_at.isoformat()
    if truth(row.get("do_not_contact")) or row.get("connection_status") == "closed":
        result.update(
            score_status="excluded",
            score_reasons="Do not contact or closed; excluded from outreach",
            recommended_next_action="Do not contact",
        )
        return result
    if result["score_status"] != "current":
        result["score_reasons"] = (
            "No current, identity-confirmed research for the configured goals; score withheld"
        )
        return result
    # Identity evidence itself must also be fresh; a checked flag alone is insufficient.
    identity_sources = [e for e in report.evidence if e.id in report.identity_evidence_ids]
    if not any(
        timedelta(0) <= now - e.retrieved_at <= timedelta(days=policy["research_max_age_days"])
        and (not e.published_at or e.published_at <= now)
        for e in identity_sources
    ):
        result.update(
            score_status="stale_identity",
            score_reasons="Account identity evidence is stale or future-dated",
        )
        return result
    values = {
        key: active_finding(report, key, policy, now)
        for key in ("role_fit", "full_time_remote", "contract_remote", "hiring", "contracting")
    }
    covered = sum(value not in {"unknown", "uncertain"} for value in values.values())
    result["evidence_coverage"] = f"{covered}/5"
    reasons = [f"{key}={value}" for key, value in values.items()]
    if values["role_fit"] == "unknown":
        result.update(score_status="insufficient_evidence", score_reasons="; ".join(reasons))
        return result
    fit = policy["weights"]["account_fit"][values["role_fit"]]
    result["account_fit_score"] = fit
    tiers = []
    for pursuit, finding in (("full_time", "hiring"), ("contract", "contracting")):
        remote = values[pursuit + "_remote"]
        intent = values[finding]
        if intent != "unknown":
            points = policy["weights"]["intent"][intent]
            result["hiring_intent_score" if pursuit == "full_time" else "contract_intent_score"] = (
                points
            )
            total = fit + result["lead_fit_score"] + result["engagement_score"] + points
            result[pursuit + "_priority"] = total
        else:
            total = None
        if remote == "ineligible" or values["role_fit"] == "weak":
            tier = "tier_3"
        elif total is None:
            tier = ""
        elif (
            total >= policy["tier1_min"]
            and remote == "eligible"
            and intent == "strong"
            and values["role_fit"] == "strong"
        ):
            tier = "tier_1"
        elif total >= policy["tier2_min"] or remote == "uncertain":
            tier = "tier_2"
        else:
            tier = "tier_3"
        result[pursuit + "_tier"] = tier
        if tier:
            tiers.append(tier)
    if not tiers:
        result.update(
            score_status="insufficient_evidence",
            score_reasons="; ".join(reasons),
            recommended_next_action="Research current roles or a supported contract opportunity",
        )
        return result
    tier = min(tiers)
    status = (
        "scored"
        if (
            tier == "tier_1"
            or all(v != "uncertain" for k, v in values.items() if k.endswith("_remote"))
        )
        else "provisional"
    )
    result.update(
        tier=tier,
        score_status=status,
        score_reasons="; ".join(reasons),
        recommended_next_action=report.next_action
        if tier != "tier_3"
        else "Deprioritize; retain evidence and revisit if circumstances change",
    )
    return result


def score_rows(rows, research, policy, *, now=None):
    validate_policy(policy)
    cache = {}
    results = []
    for row in rows:
        domain = row.get("domain")
        if domain and domain not in cache:
            cache[domain] = research.load(domain)
        results.append(
            {
                "id": row["id"],
                "name": row["full_name"],
                "company": row.get("company_name"),
                "domain": domain,
                **score(row, cache.get(domain), policy, now=now),
            }
        )
    return results

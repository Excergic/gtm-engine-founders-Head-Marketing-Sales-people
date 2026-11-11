"""Account-level, source-backed research receipts. No paid calls or identity merging."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import normalize_domain, timestamp, utcnow

ALLOWED = {
    "role_fit": {"strong", "moderate", "weak", "unknown"},
    "full_time_remote": {"eligible", "uncertain", "ineligible"},
    "contract_remote": {"eligible", "uncertain", "ineligible"},
    "hiring": {"strong", "moderate", "none", "unknown"},
    "contracting": {"strong", "moderate", "none", "unknown"},
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    url: str
    title: str = Field(min_length=1)
    excerpt: str = Field(min_length=1, max_length=1200)
    source_type: Literal["official", "job_listing", "third_party"]
    retrieved_at: datetime
    published_at: datetime | None = None

    @field_validator("url")
    @classmethod
    def public_url(cls, url):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Evidence needs an HTTP(S) URL without credentials")
        return url

    @field_validator("retrieved_at", "published_at", mode="before")
    @classmethod
    def dates(cls, value):
        return timestamp(value) if value else None


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str
    evidence_ids: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1)
    eligibility_scope: Literal[
        "worldwide", "candidate_location", "restricted", "onsite_only", "unknown"
    ] = "unknown"


class AccountResearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    domain: str
    company_name: str
    profile_hash: str
    researched_at: datetime
    researcher: str = Field(min_length=1)
    status: Literal["complete", "partial", "failed"]
    identity_confirmed: bool = False
    identity_evidence_ids: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    findings: dict[str, Finding] = Field(default_factory=dict)
    next_action: str = Field(min_length=1)
    gaps: list[str] = Field(default_factory=list)
    # This is an explanation of possible work, never an observed purchase/hiring signal.
    opportunity_hypothesis: str = ""

    @field_validator("domain")
    @classmethod
    def domain_name(cls, value):
        return normalize_domain(value)

    @field_validator("researched_at", mode="before")
    @classmethod
    def date(cls, value):
        return timestamp(value)

    @field_validator("findings", mode="before")
    @classmethod
    def normalize_remote_unknown(cls, findings):
        # Older agents used the generic unknown label for missing eligibility.
        # Map only that missing-data synonym; never infer eligible/ineligible.
        if not isinstance(findings, dict):
            return findings
        normalized = dict(findings)
        for key in ("full_time_remote", "contract_remote"):
            finding = normalized.get(key)
            if isinstance(finding, dict) and finding.get("value") == "unknown":
                normalized[key] = {**finding, "value": "uncertain"}
        return normalized

    @model_validator(mode="after")
    def evidence_contract(self):
        ids = [e.id for e in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("Evidence IDs must be unique within an account")
        ids = set(ids)
        if set(self.findings) - set(ALLOWED):
            raise ValueError("Unsupported research finding")
        if not set(self.identity_evidence_ids) <= ids:
            raise ValueError("Unknown identity evidence ID")
        if self.identity_confirmed and not self.identity_evidence_ids:
            raise ValueError("Confirmed account identity needs evidence")
        if self.identity_confirmed and not any(
            e.id in self.identity_evidence_ids
            and e.source_type == "official"
            and (
                normalize_domain(e.url) == self.domain
                or normalize_domain(e.url).endswith("." + self.domain)
            )
            for e in self.evidence
        ):
            raise ValueError(
                "Identity confirmation requires a source on the account official domain"
            )
        for key, finding in self.findings.items():
            if finding.value not in ALLOWED[key]:
                raise ValueError(f"Unsupported value for {key}")
            if not set(finding.evidence_ids) <= ids:
                raise ValueError("Unknown finding evidence ID")
            if finding.value not in {"unknown", "uncertain"} and not finding.evidence_ids:
                raise ValueError(f"{key} needs supporting evidence")
        return self


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".research-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2, default=str)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


class ResearchStore:
    def __init__(self, directory):
        self.directory = Path(directory)

    def path(self, domain):
        return self.directory / "accounts" / f"{normalize_domain(domain)}.json"

    def load(self, domain):
        path = self.path(domain)
        return AccountResearch.model_validate_json(path.read_text()) if path.exists() else None

    def save(self, report):
        now = utcnow()
        if report.researched_at > now:
            raise ValueError("Research timestamp is in the future")
        if any(
            e.retrieved_at > now or (e.published_at and e.published_at > now)
            for e in report.evidence
        ):
            raise ValueError("Evidence timestamp is in the future")
        current = self.load(report.domain)
        if current and current.researched_at > report.researched_at:
            raise ValueError("Cannot replace newer research with older research")
        payload = report.model_dump(mode="json")
        receipt = self.directory / "history" / report.domain / f"{digest(payload)}.json"
        if not receipt.exists():
            atomic_json(receipt, payload)
        atomic_json(self.path(report.domain), payload)
        return receipt


def profile_hash(policy):
    return digest(policy["profile"])


def research_queue(rows, research, policy, *, now=None, include_current=False):
    from .scoring import research_state
    from .store import truth

    now = now or utcnow()
    groups = {}
    for row in rows:
        if (
            not row.get("domain")
            or truth(row.get("do_not_contact"))
            or row["connection_status"] == "closed"
        ):
            continue
        account = groups.setdefault(
            row["domain"],
            {
                "domain": row["domain"],
                "company_names": set(),
                "contact_ids": [],
                "job_titles": set(),
                "source_hints": [],
                "gaps": set(),
            },
        )
        account["company_names"].add(row["company_name"])
        account["contact_ids"].append(row["id"])
        account["job_titles"].add(row.get("job_title", ""))
        if row.get("signal"):
            hint = {
                "claim": row["signal"],
                "event_date": row.get("signal_date"),
                "url": row.get("source_url"),
                "status": "unverified_import_hint",
            }
            if hint not in account["source_hints"]:
                account["source_hints"].append(hint)
        if not row.get("email") and not row.get("linkedin_url"):
            account["gaps"].add("Missing contact identity; enrich only after qualification")
    output = []
    for domain, account in sorted(groups.items()):
        current = research.load(domain)
        status = research_state(current, policy, now)
        if not include_current and status == "current":
            continue
        account.update(
            research_status=status, homepage=f"https://{domain}", profile_hash=profile_hash(policy)
        )
        for field in ("company_names", "job_titles", "gaps"):
            account[field] = sorted(account[field])
        account["source_hints"] = account["source_hints"][:5]
        output.append(account)
    priority = {
        "needs_research": 0,
        "profile_changed": 1,
        "stale_research": 1,
        "identity_unconfirmed": 2,
        "research_failed": 3,
        "current": 4,
    }
    return sorted(output, key=lambda a: (priority.get(a["research_status"], 2), a["domain"]))

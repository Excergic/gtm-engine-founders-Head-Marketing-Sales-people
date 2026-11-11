"""Canonical input contracts for the local CSV tracker. All timestamps are UTC-aware."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.headerregistry import Address
from enum import StrEnum
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


def timestamp(value: str | datetime) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def normalize_domain(value: str) -> str:
    url = urlsplit(value if "://" in value else "https://" + value)
    try:
        _ = url.port
    except ValueError as exc:
        raise ValueError("invalid URL port") from exc
    host = (url.hostname or "").lower().rstrip(".").removeprefix("www.")
    host = host.encode("idna").decode("ascii")
    if (
        url.username
        or url.password
        or "." not in host
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host)
        or any(
            not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
            for label in host.split(".")
        )
    ):
        raise ValueError("invalid company domain")
    return host


def normalize_linkedin(value: str) -> str:
    url = urlsplit(value if "://" in value else "https://" + value)
    path = unquote(url.path).rstrip("/").lower()
    if (
        url.scheme not in {"http", "https"}
        or (url.hostname or "").lower()
        not in {"linkedin.com", "www.linkedin.com", "in.linkedin.com"}
        or not re.fullmatch(r"/in/[a-z0-9_%\-]+", path)
    ):
        raise ValueError("expected a LinkedIn /in/ profile URL")
    return "https://www.linkedin.com" + path


class Source(StrEnum):
    linkedin = "linkedin"
    grok = "grok"


class Objective(StrEnum):
    consulting_client = "consulting_client"
    remote_job = "remote_job"


class ConnectionStatus(StrEnum):
    discovered = "discovered"
    connection_sent = "connection_sent"
    connection_pending = "connection_pending"
    connected = "connected"
    conversation_started = "conversation_started"
    replied = "replied"
    meeting_booked = "meeting_booked"
    no_response = "no_response"
    closed = "closed"


class Prospect(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    full_name: str = Field(min_length=2)
    source: Source
    objective: Objective = Objective.consulting_client
    email: str | None = None
    email_verified: bool = False
    linkedin_url: str | None = None
    company_name: str | None = None
    domain: str | None = None
    domain_verified: bool = False
    website: str | None = None
    job_title: str | None = None
    industry: str | None = None
    company_size: int | None = Field(default=None, ge=1)
    icp_segment: str = "unclassified"
    discovered_at: datetime = Field(default_factory=utcnow)
    external_id: str | None = None
    source_reference: str = "manual"
    connection_status: ConnectionStatus = ConnectionStatus.discovered
    status_observed_at: datetime | None = None
    connection_request_at: datetime | None = None
    last_contact_at: datetime | None = None
    next_followup_at: datetime | None = None
    notes: str | None = None
    do_not_contact: bool = False
    signal_level: str = "none"
    signal_at: datetime | None = None
    signal_provenance: str | None = None
    signal_objective: Objective | None = None
    create_opportunity: bool = False
    opportunity_stage: str | None = None

    @model_validator(mode="before")
    @classmethod
    def blanks(cls, values):
        return {k: v for k, v in values.items() if v is not None and v != ""}

    @field_validator("full_name")
    @classmethod
    def name(cls, value):
        return " ".join(value.split())

    @field_validator("email")
    @classmethod
    def email_address(cls, value):
        if value is not None:
            value = value.casefold()
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
                raise ValueError("invalid email address")
            try:
                address = Address(addr_spec=value)
                normalize_domain(address.domain)
                email_domain = address.domain.encode("idna").decode("ascii").rstrip(".")
            except ValueError as exc:
                raise ValueError("invalid email address") from exc
            value = address.username + "@" + email_domain
        return value

    @field_validator("domain")
    @classmethod
    def domain_name(cls, value):
        return normalize_domain(value) if value else None

    @field_validator("linkedin_url")
    @classmethod
    def linkedin(cls, value):
        return normalize_linkedin(value) if value else None

    @field_validator("website")
    @classmethod
    def website_url(cls, value):
        if value:
            normalize_domain(value)
            value = value if "://" in value else "https://" + value
            if urlsplit(value).scheme not in {"http", "https"}:
                raise ValueError("website must use HTTP or HTTPS")
        return value

    @field_validator(
        "discovered_at",
        "connection_request_at",
        "status_observed_at",
        "last_contact_at",
        "next_followup_at",
        "signal_at",
        mode="before",
    )
    @classmethod
    def dates(cls, value):
        return timestamp(value) if value else None

    @field_validator("signal_level")
    @classmethod
    def signal(cls, value):
        if value not in {"strong", "moderate", "none"}:
            raise ValueError("signal_level must be strong, moderate, or none")
        return value

    @model_validator(mode="after")
    def derive_domain(self):
        if not self.domain and self.website:
            self.domain = normalize_domain(self.website)
        if self.email_verified and not self.email:
            raise ValueError("email_verified requires email")
        if self.signal_level != "none" and (not self.signal_at or not self.signal_provenance):
            raise ValueError("signals require timestamp and provenance")
        return self

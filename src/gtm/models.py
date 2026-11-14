"""Canonical input contracts for the local CSV tracker. All timestamps are UTC-aware."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
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


def normalize_http_url(value: str) -> str:
    url = urlsplit(value if "://" in value else "https://" + value)
    if url.scheme not in {"http", "https"} or "." not in (url.hostname or ""):
        raise ValueError("expected an http(s) URL with a domain")
    return url.geturl()


def normalize_x_handle(value: str) -> str:
    """Accept '@name', 'name', or an x.com / twitter.com profile URL; return '@name'."""
    text = value.strip()
    if "/" in text or "." in text:
        url = urlsplit(text if "://" in text else "https://" + text)
        host = (url.hostname or "").lower().removeprefix("www.")
        if host not in {"x.com", "twitter.com"}:
            raise ValueError("x_handle must be a handle or an x.com profile URL")
        text = url.path.strip("/").split("/")[0]
    handle = text.removeprefix("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
        raise ValueError("invalid X handle")
    return "@" + handle


# Grok columns that signal-based outbound depends on. Rows missing any are still
# captured, but flagged for review rather than silently accepted.
SIGNAL_FIELDS = ("signal", "signal_date", "source_url")


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
    """Canonical person contract shared by every source (Grok, LinkedIn, manual)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    # Identity
    full_name: str = Field(min_length=2, max_length=200)
    source: Source
    objective: Objective = Objective.consulting_client
    email: str | None = None
    linkedin_url: str | None = None
    x_handle: str | None = None
    job_title: str | None = Field(default=None, max_length=200)
    country: str | None = Field(default=None, max_length=100)
    city: str | None = Field(default=None, max_length=100)
    # Company
    company_name: str | None = Field(default=None, max_length=200)
    domain: str | None = None
    website: str | None = None
    # Signal (why this person, why now)
    signal: str | None = Field(default=None, max_length=1000)
    signal_date: str | None = None  # YYYY-MM-DD, or YYYY-MM / YYYY when Grok is imprecise
    source_url: str | None = None
    why_fit: str | None = Field(default=None, max_length=1000)
    outreach_angle: str | None = Field(default=None, max_length=1000)
    # Provenance and lifecycle
    discovered_at: datetime = Field(default_factory=utcnow)
    source_reference: str = "manual"
    connection_status: ConnectionStatus = ConnectionStatus.discovered
    status_observed_at: datetime | None = None
    connection_request_at: datetime | None = None
    last_contact_at: datetime | None = None
    next_followup_at: datetime | None = None
    notes: str | None = None
    do_not_contact: bool = False
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
        mode="before",
    )
    @classmethod
    def dates(cls, value):
        return timestamp(value) if value else None

    @field_validator("source_url")
    @classmethod
    def source_link(cls, value):
        return normalize_http_url(value) if value else None

    @field_validator("x_handle")
    @classmethod
    def x_profile(cls, value):
        return normalize_x_handle(value) if value else None

    @field_validator("signal_date", mode="before")
    @classmethod
    def signal_day(cls, value):
        """Keep the precision the source gave; never invent a day or month."""
        if isinstance(value, datetime | date):
            value = value.isoformat()
        text = str(value).strip()
        if "T" in text:
            text = timestamp(text).date().isoformat()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            date.fromisoformat(text)
        elif re.fullmatch(r"\d{4}-\d{2}", text):
            date.fromisoformat(text + "-01")
        elif not re.fullmatch(r"\d{4}", text):
            raise ValueError("signal_date must be YYYY-MM-DD (or YYYY-MM / YYYY)")
        today = (utcnow() + timedelta(days=1)).date().isoformat()
        if text > today[: len(text)]:
            raise ValueError("signal_date is in the future")
        return text

    @property
    def signal_date_precision(self) -> str | None:
        if not self.signal_date:
            return None
        return {10: "day", 7: "month", 4: "year"}[len(self.signal_date)]

    def missing_signal_fields(self) -> list[str]:
        return [name for name in SIGNAL_FIELDS if not getattr(self, name)]

    @model_validator(mode="after")
    def derive_domain(self):
        if not self.domain and self.website:
            self.domain = normalize_domain(self.website)
        return self

"""Editable master CSV with atomic replacement, private backups, and strict reads."""

from __future__ import annotations

import csv
import json
import os
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

from .models import ConnectionStatus, Prospect, timestamp, utcnow

FIELDS = [
    "id",
    "full_name",
    "email",
    "linkedin_url",
    "company_name",
    "domain",
    "website",
    "job_title",
    "country",
    "city",
    "x_handle",
    "source_url",
    "signal",
    "signal_date",
    "why_fit",
    "outreach_angle",
    "sources",
    "segments",
    "original_source",
    "discovered_at",
    "last_seen",
    "objectives",
    "consulting_stage",
    "job_stage",
    "connection_status",
    "status_changed_at",
    "connection_request_at",
    "last_contact_at",
    "next_followup_at",
    "notes",
    "do_not_contact",
    "needs_review",
    "review_reason",
    "activity_history",
    "completed_actions",
    "source_data",
]
JSON_FIELDS = ("activity_history", "completed_actions", "source_data")
DATE_FIELDS = (
    "status_changed_at",
    "discovered_at",
    "last_seen",
    "connection_request_at",
    "last_contact_at",
    "next_followup_at",
)


def truth(value):
    if str(value).strip().casefold() not in {"", "false", "0", "no", "true", "1", "yes"}:
        raise ValueError("Expected true/false in boolean column")
    return str(value).strip().casefold() in {"true", "1", "yes"}


def validate(row, number):
    if not row.get("id") or not row.get("full_name"):
        raise ValueError(
            f"Master CSV row {number} needs id and full_name; restore a backup if damaged"
        )
    # Normalize edited identity fields exactly like incoming data. No network verification implied.
    p = Prospect(
        source="grok",
        full_name=row["full_name"],
        email=row.get("email"),
        linkedin_url=row.get("linkedin_url"),
        domain=row.get("domain"),
        website=row.get("website"),
    )
    for field in ("full_name", "email", "linkedin_url", "domain", "website"):
        row[field] = getattr(p, field) or ""
    ConnectionStatus(row.get("connection_status") or "discovered")
    for field in ("do_not_contact", "needs_review"):
        truth(row.get(field, ""))
    for field in DATE_FIELDS:
        if row.get(field):
            timestamp(row[field])
    for field in JSON_FIELDS:
        if not isinstance(json.loads(row.get(field) or "[]"), list):
            raise ValueError(f"Master CSV row {number}: {field} must be a JSON list")
    if any(
        not isinstance(item, dict) or not item.get("key")
        for item in json.loads(row.get("source_data") or "[]")
    ):
        raise ValueError(f"Master CSV row {number}: invalid source_data; restore a backup")


class CSVStore:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file, strict=True)
            headers = reader.fieldnames or []
            if not (set(FIELDS) - {"segments"}).issubset(headers) or len(set(headers)) != len(
                headers
            ):
                raise ValueError("Master CSV has missing/duplicate headers; restore a backup")
            rows = []
            ids = set()
            for number, raw in enumerate(reader, 2):
                if not any(raw.values()):
                    continue
                if None in raw:
                    raise ValueError(f"Master CSV row {number} has too many columns")
                row = {key: (value or "") for key, value in raw.items()}
                row.setdefault("segments", "")
                validate(row, number)
                if row["id"] in ids:
                    raise ValueError("Master CSV contains duplicate IDs; resolve before importing")
                ids.add(row["id"])
                rows.append(row)
            return rows

    def write(self, rows):
        """Caller holds the writer lock. Failed writes leave the previous master intact."""
        for number, row in enumerate(rows, 2):
            validate(row, number)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError("Master CSV must not be a symlink")
        # Keep every pre-write version, including manual spreadsheet edits.
        if self.path.exists():
            backups = self.path.parent / "backups"
            backups.mkdir(exist_ok=True, mode=0o700)
            backup = backups / f"{self.path.stem}-{utcnow():%Y%m%dT%H%M%S%f}-{uuid4().hex[:8]}.csv"
            shutil.copyfile(self.path, backup)
            backup.chmod(0o600)
            prune_backups(backups, self.path.stem)
        fields = list(dict.fromkeys(FIELDS + [key for row in rows for key in row]))
        fd, tmp = tempfile.mkstemp(prefix=".prospects-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, self.path)
            parent_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            Path(tmp).unlink(missing_ok=True)


def backup_keep() -> int:
    """Number of master backups to retain (GTM_BACKUP_KEEP, default 30, minimum 1)."""
    try:
        return max(1, int(os.environ.get("GTM_BACKUP_KEEP", "30")))
    except ValueError:
        return 30


def prune_backups(directory: Path, stem: str, keep: int | None = None) -> list[Path]:
    """Delete all but the newest `keep` backups. Names sort chronologically by timestamp."""
    keep = backup_keep() if keep is None else max(1, keep)
    files = sorted(directory.glob(f"{stem}-*.csv"))
    stale = files[:-keep]
    for path in stale:
        path.unlink(missing_ok=True)
    return stale

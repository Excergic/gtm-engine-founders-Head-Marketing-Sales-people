"""Deepline adapter: runs Deepline plays/tools through the ``deepline`` CLI.

- linkedin_url: ``prebuilt/person-to-linkedin-harvestapi`` (first/last name + domain/company)
- email:        ``prebuilt/person-to-email`` (Deepline's own multi-provider waterfall)
- verify:       ``bounceban_verify_single`` (deliverable / risky / undeliverable)

Play and tool IDs come from config, so they can be swapped without code changes.
The CLI call is injectable (``runner``) so tests never spend credits.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from ..enrichment import Finding
from . import register
from .clay import name_tokens

VERDICTS = {"deliverable": "verified", "risky": "risky", "undeliverable": "invalid",
            "unknown": "unknown", "valid": "verified", "invalid": "invalid",
            "catch_all": "risky", "accept_all": "risky"}  # fmt: skip


def _cli(args: list[str], timeout: int) -> dict:
    done = subprocess.run(
        ["deepline", *args, "--json"], capture_output=True, text=True, timeout=timeout
    )
    if done.returncode != 0:
        raise RuntimeError(f"deepline {args[0]} {args[1]} failed: {done.stderr.strip()[:300]}")
    return json.loads(done.stdout)


def first(data, *keys):
    """First non-empty value for any of ``keys`` anywhere in nested JSON."""
    if isinstance(data, dict):
        for key in keys:
            if data.get(key) not in (None, ""):
                return data[key]
        for value in data.values():
            found = first(value, *keys)
            if found not in (None, ""):
                return found
    elif isinstance(data, list):
        for value in data:
            found = first(value, *keys)
            if found not in (None, ""):
                return found
    return None


def split_name(name: str) -> tuple[str, str]:
    tokens = [t for t in name_tokens(name) if re.fullmatch(r"[\w'-]+", t)]
    words = [w for w in re.split(r"[\s,]+", name) if w.strip(".").casefold() in tokens]
    return (words[0], words[-1]) if len(words) >= 2 else ("", "")


@register("deepline")
class DeeplineEnricher:
    stages = frozenset({"linkedin_url", "email", "verify"})

    def __init__(self, name: str, settings: dict, data_dir: Path, runner: Callable | None = None):
        self.name = name
        self.settings = settings
        self.timeout = int(settings.get("timeout_seconds", 300))
        self.runner = runner or _cli

    def _run(self, kind: str, ref: str, payload: dict) -> dict:
        verb = ["plays", "run", ref] if kind == "play" else ["tools", "execute", ref]
        return self.runner([*verb, "--input", json.dumps(payload)], self.timeout)

    def find(self, stage: str, people: list[dict]) -> list[Finding]:
        findings = []
        for person in people:
            first_name, last_name = split_name(person["name"])
            try:
                finding = getattr(self, f"_{stage}")(person, first_name, last_name)
            except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                finding = None
                person.setdefault("errors", []).append(f"{self.name}.{stage}: {exc}")
            if finding:
                findings.append(finding)
        return findings

    def _linkedin_url(self, person, first_name, last_name):
        if not (first_name and last_name):
            return None
        payload = {"first_name": first_name, "last_name": last_name,
                   "company_name": person["company"]}  # fmt: skip
        if person.get("domain"):
            payload["domain"] = person["domain"]
        response = self._run("play", self.settings["linkedin_play"], payload)
        url = first(response, "linkedin_url")
        if not url:
            return None
        return Finding(prospect_id=person["prospect_id"], provider=self.name, linkedin_url=url,
                       confidence=0.7, request=payload, response=response)  # fmt: skip

    def _email(self, person, first_name, last_name):
        payload = {}
        if person.get("linkedin_url"):
            payload["linkedin_url"] = person["linkedin_url"]
        if first_name and last_name and person.get("domain"):
            payload.update(first_name=first_name, last_name=last_name, domain=person["domain"])
        if not payload:
            return None
        response = self._run("play", self.settings["email_play"], payload)
        email = first(response, "email")
        if not email:
            return None
        validated = first(response, "email_validated")
        status = first(response, "email_validation_status")
        status = "verified" if validated is True else VERDICTS.get(str(status).lower(), "unknown")
        return Finding(prospect_id=person["prospect_id"], provider=self.name, email=email,
                       email_status=status, confidence=0.9 if validated else 0.6,
                       request=payload, response=response)  # fmt: skip

    def _verify(self, person, first_name, last_name):
        if not person.get("email"):
            return None
        payload = {"email": person["email"]}
        response = self._run("tool", self.settings["verify_tool"], payload)
        verdict = first(response, "result", "email_status")
        if verdict is None:
            return None
        return Finding(prospect_id=person["prospect_id"], provider=self.name,
                       email=person["email"],
                       email_status=VERDICTS.get(str(verdict).lower(), "unknown"),
                       confidence=(first(response, "score") or 0) / 100 or None,
                       request=payload, response=response)  # fmt: skip

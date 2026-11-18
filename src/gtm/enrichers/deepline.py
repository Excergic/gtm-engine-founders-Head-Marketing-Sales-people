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
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from ..enrichment import Finding
from . import ProviderError, record_failure, register
from .clay import name_tokens

VERDICTS = {"deliverable": "verified", "risky": "risky", "undeliverable": "invalid",
            "unknown": "unknown", "valid": "verified", "invalid": "invalid",
            "catch_all": "risky", "accept_all": "risky"}  # fmt: skip


# Failures a retry cannot fix: stop asking, and stop the run if it is about credits.
PERMANENT = ("insufficient", "credit", "balance", "payment method", "billing",
             "unauthorized", "forbidden", "not authenticated", "invalid input",
             "validation")  # fmt: skip


def _error_text(done: subprocess.CompletedProcess) -> str:
    """The CLI prints its JSON error on stdout and progress logs on stderr."""
    try:
        data = json.loads(done.stdout)
        error = data.get("error") if isinstance(data, dict) else None
        message = error.get("message") if isinstance(error, dict) else data.get("message")
        if message:
            return str(message)
    except (ValueError, AttributeError):
        pass
    tail = [line for line in (done.stderr or "").strip().splitlines() if line.strip()][-2:]
    return " | ".join(tail) or (done.stdout or "").strip()[-300:] or f"exit {done.returncode}"


def _cli(args: list[str], timeout: int, attempts: int = 2, backoff: float = 10.0) -> dict:
    """Run one deepline command; retry temporary failures with backoff."""
    label = f"deepline {args[0]} {args[1]}"
    for attempt in range(1, attempts + 1):
        try:
            done = subprocess.run(
                ["deepline", *args, "--json"], capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            error, retryable = f"timed out after {timeout}s", True
        else:
            if done.returncode == 0:
                try:
                    return json.loads(done.stdout)
                except ValueError:
                    error, retryable = "output was not JSON", True
            else:
                error = _error_text(done)
                retryable = not any(word in error.casefold() for word in PERMANENT)
        if not retryable or attempt == attempts:
            suffix = f" (after {attempt} attempts)" if attempt > 1 else ""
            raise ProviderError(f"{label}: {error[:400]}{suffix}", retryable=retryable)
        time.sleep(backoff * attempt)
    raise AssertionError("unreachable")


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
        attempts = int(settings.get("attempts", 2))
        self.runner = runner or (lambda args, timeout: _cli(args, timeout, attempts=attempts))

    def _run(self, kind: str, ref: str, payload: dict) -> dict:
        if kind == "play":
            # --full: without it the CLI prints a bounded preview with no play result.
            args = ["plays", "run", ref, "--input", json.dumps(payload), "--full"]
        else:
            args = ["tools", "execute", ref, "--input", json.dumps(payload)]
        return self.runner(args, self.timeout)

    @staticmethod
    def _result(response: dict) -> dict:
        """The play's own output. Only this is trusted: the waterfall attempts nested
        inside it can hold candidates the play rejected."""
        result = response.get("result")
        if not isinstance(result, dict):
            raise ValueError("play response has no result object")
        return result

    def find(self, stage: str, people: list[dict]) -> list[Finding]:
        findings = []
        for person in people:
            first_name, last_name = split_name(person["name"])
            try:
                finding = getattr(self, f"_{stage}")(person, first_name, last_name)
            except ValidationError as exc:
                # The provider returned a value we refuse (e.g. a malformed email).
                finding = None
                error = ProviderError(f"rejected value: {exc.errors()[0]['msg']}", retryable=False)
                record_failure(person, self.name, stage, error)
            except Exception as exc:  # one person's failure never stops the batch
                finding = None
                record_failure(person, self.name, stage, exc)
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
        result = self._result(response)
        url = result.get("linkedin_url")
        if not url or result.get("identity_validation_status") != "verified":
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
        result = self._result(response)
        email = result.get("email")
        if not email or result.get("miss_reason"):
            return None
        validated = result.get("email_validated")
        status = result.get("email_validation_status")
        status = "verified" if validated is True else VERDICTS.get(str(status).lower(), "unknown")
        return Finding(prospect_id=person["prospect_id"], provider=self.name, email=email,
                       email_status=status, confidence=0.9 if validated else 0.6,
                       request=payload, response=response)  # fmt: skip

    def _verify(self, person, first_name, last_name):
        if not person.get("email"):
            return None
        payload = {"email": person["email"]}
        response = self._run("tool", self.settings["verify_tool"], payload)
        raw = (response.get("toolResponse") or {}).get("rawV2") or {}
        verdict = raw.get("result")
        if not isinstance(verdict, str):
            return None
        return Finding(prospect_id=person["prospect_id"], provider=self.name,
                       email=person["email"],
                       email_status=VERDICTS.get(verdict.lower(), "unknown"),
                       confidence=(raw.get("score") or 0) / 100 or None,
                       request=payload, response=response)  # fmt: skip

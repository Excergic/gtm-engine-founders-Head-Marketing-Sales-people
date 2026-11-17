"""Clay adapter.

Clay is reached through the Claude connection (no API key in this repo), so a Clay run
saves its results as JSON in ``<data>/enrichment/clay/``:

    {"requests": [{"prospect_id": ..., "contactName": ..., "companyIdentifier": ...}],
     "task": <get-task-context response>}

This adapter reads every such file and turns contacts into findings. A contact is only
accepted for the prospect it was requested for (same contactName + companyIdentifier),
and only when the returned name matches the person's name.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..enrichment import Finding
from . import register

SUFFIXES = {"phd", "md", "mba", "jr", "sr", "ii", "iii"}


def name_tokens(name: str) -> list[str]:
    words = re.split(r"[\s,]+", (name or "").casefold())
    return [w.strip(".") for w in words if w.strip(".") and w.strip(".") not in SUFFIXES]


def same_name(a: str, b: str) -> bool:
    x, y = name_tokens(a), name_tokens(b)
    return bool(x) and bool(y) and x[0] == y[0] and x[-1] == y[-1]


@register("clay_results")
class ClayResultsEnricher:
    stages = frozenset({"linkedin_url", "email"})

    def __init__(self, name: str, settings: dict, data_dir: Path):
        self.name = name
        self.directory = data_dir / settings.get("results_dir", "enrichment/clay")

    def _contacts(self) -> dict[str, list[tuple[dict, dict]]]:
        """prospect_id -> [(request, contact)], from every saved Clay result file."""
        found: dict[str, list[tuple[dict, dict]]] = {}
        for path in sorted(self.directory.glob("*.json")):
            data = json.loads(path.read_text())
            contacts = data.get("task", {}).get("contacts", [])
            for request in data.get("requests", []):
                for contact in contacts:
                    if contact.get("companyIdentifier") == request.get(
                        "companyIdentifier"
                    ) and same_name(contact.get("name", ""), request.get("contactName", "")):
                        found.setdefault(request["prospect_id"], []).append((request, contact))
        return found

    @staticmethod
    def _email(contact: dict) -> str | None:
        for enrichment in contact.get("enrichments") or []:
            if enrichment.get("state") != "completed" or not enrichment.get("value"):
                continue
            try:
                value = json.loads(enrichment["value"])
            except (TypeError, ValueError):
                continue
            for key in ("Work Email", "Email"):
                if isinstance(value, dict) and value.get(key):
                    return value[key]
        return None

    def find(self, stage: str, people: list[dict]) -> list[Finding]:
        contacts = self._contacts()
        findings = []
        for person in people:
            for request, contact in contacts.get(person["prospect_id"], []):
                if not same_name(contact.get("name", ""), person["name"]):
                    continue
                value = contact.get("url") if stage == "linkedin_url" else self._email(contact)
                if not value:
                    continue
                findings.append(
                    Finding(
                        prospect_id=person["prospect_id"],
                        provider=self.name,
                        **(
                            {"linkedin_url": value} if stage == "linkedin_url" else {"email": value}
                        ),
                        # Clay's email lookup returns no deliverability verdict.
                        confidence=0.8,
                        request=request,
                        response=contact,
                    )
                )
                break
        return findings

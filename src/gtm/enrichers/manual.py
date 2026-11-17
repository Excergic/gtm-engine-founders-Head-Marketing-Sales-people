"""Manual adapter: findings typed in by a person, saved as JSON in
``<data>/enrichment/manual/*.json`` as ``{"findings": [{"prospect_id": ..., "email": ...}]}``.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..enrichment import Finding
from . import register


@register("manual")
class ManualEnricher:
    stages = frozenset({"linkedin_url", "email", "verify"})

    def __init__(self, name: str, settings: dict, data_dir: Path):
        self.name = name
        self.directory = data_dir / settings.get("results_dir", "enrichment/manual")

    def find(self, stage: str, people: list[dict]) -> list[Finding]:
        wanted = {p["prospect_id"] for p in people}
        field = "email_status" if stage == "verify" else stage
        findings = []
        for path in sorted(self.directory.glob("*.json")):
            for raw in json.loads(path.read_text()).get("findings", []):
                if raw.get("prospect_id") in wanted and raw.get(field):
                    findings.append(Finding.model_validate({**raw, "provider": self.name}))
        return findings

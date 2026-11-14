"""Optional local paths and small YAML rules; no credentials or remote services."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class Settings(BaseModel):
    tracker: Path = Path("data/prospects.csv")
    grok_directory: Path = Path("data/incoming/grok")
    linkedin_directory: Path = Path("data/incoming/linkedin")
    data_directory: Path = Path("data")
    database: Path = Path("data/state/local.sqlite")
    config_directory: Path = Path("config")
    stability_seconds: float = Field(default=2.0, ge=0.1, le=60)

    @classmethod
    def load(cls):
        load_dotenv(Path.cwd() / ".env", interpolate=False)
        names = {
            "tracker": "LOCAL_TRACKER_CSV",
            "grok_directory": "GROK_CSV_DIRECTORY",
            "linkedin_directory": "LINKEDIN_CSV_DIRECTORY",
            "data_directory": "GTM_DATA_DIRECTORY",
            "database": "LOCAL_STATE_DATABASE",
            "config_directory": "GTM_CONFIG_DIRECTORY",
            "stability_seconds": "GROK_FILE_STABILITY_SECONDS",
        }
        settings = cls(
            **{key: os.environ[name] for key, name in names.items() if name in os.environ}
        )
        for inbox in (settings.grok_directory, settings.linkedin_directory):
            if settings.tracker.resolve().is_relative_to(inbox.resolve()):
                raise ValueError("LOCAL_TRACKER_CSV must be outside source inbox directories")
        return settings

    def rules(self):
        try:
            with (self.config_directory / "gtm.yaml").open() as file:
                rules = yaml.safe_load(file)
        except yaml.YAMLError as exc:
            raise ValueError("Invalid YAML in gtm.yaml; check indentation") from exc
        if not isinstance(rules, dict):
            raise ValueError("gtm.yaml must contain mappings, pipelines and followups")
        for source in ("grok", "linkedin"):
            mapping = rules.get("mappings", {}).get(source)
            if not isinstance(mapping, dict) or any(
                not isinstance(names, list) or any(not isinstance(a, str) for a in names)
                for names in mapping.values()
            ):
                raise ValueError(f"mappings.{source} must map fields to column aliases")
            from .models import Prospect

            unknown = sorted(set(mapping) - set(Prospect.model_fields))
            if unknown:
                raise ValueError(
                    f"mappings.{source} has fields not in the Prospect model: {', '.join(unknown)}"
                )
        for field in ("pending_days", "proposal_days", "outreach_days"):
            value = rules.get("followups", {}).get(field)
            if type(value) is not int or value < 0:
                raise ValueError(f"followups.{field} must be a nonnegative integer")
        for objective in ("consulting_client", "remote_job", "closed"):
            stages = rules.get("pipelines", {}).get(objective)
            if (
                not isinstance(stages, list)
                or not stages
                or any(not isinstance(s, str) for s in stages)
            ):
                raise ValueError(f"pipelines.{objective} must contain stage names")
        if "scoring" in rules:
            from .scoring import validate_policy

            validate_policy(rules["scoring"])
        return rules

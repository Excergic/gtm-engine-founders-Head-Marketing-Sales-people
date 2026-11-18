"""Paths and the YAML rules (config/gtm.yaml). The warehouse (DATABASE_URL) is the only
record; local files are inputs (Grok CSVs) and run logs."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class Settings(BaseModel):
    grok_directory: Path = Path("data/incoming/grok")
    linkedin_directory: Path = Path("data/incoming/linkedin")
    data_directory: Path = Path("data")
    config_directory: Path = Path("config")
    stability_seconds: float = Field(default=2.0, ge=0.1, le=60)

    @classmethod
    def load(cls):
        load_dotenv(Path.cwd() / ".env", interpolate=False)
        names = {
            "grok_directory": "GROK_CSV_DIRECTORY",
            "linkedin_directory": "LINKEDIN_CSV_DIRECTORY",
            "data_directory": "GTM_DATA_DIRECTORY",
            "config_directory": "GTM_CONFIG_DIRECTORY",
            "stability_seconds": "GROK_FILE_STABILITY_SECONDS",
        }
        settings = cls(
            **{key: os.environ[name] for key, name in names.items() if name in os.environ}
        )
        return settings

    def rules(self):
        try:
            with (self.config_directory / "gtm.yaml").open() as file:
                rules = yaml.safe_load(file)
        except yaml.YAMLError as exc:
            raise ValueError("Invalid YAML in gtm.yaml; check indentation") from exc
        if not isinstance(rules, dict):
            raise ValueError("gtm.yaml must contain mappings and icp")
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
        if not isinstance(rules.get("icp"), dict):
            raise ValueError("gtm.yaml must contain an icp: block")
        weights = rules["icp"].get("weights", {})
        if sum(weights.values()) != 100:
            raise ValueError("icp.weights must add up to 100")
        return rules

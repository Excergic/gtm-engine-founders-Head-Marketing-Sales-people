"""Authorized CSV and manual adapters; no network or LinkedIn automation."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from .models import Prospect, Source


@dataclass
class ParsedRow:
    number: int
    prospect: Prospect | None
    error: str | None = None


def parse_csv(path: Path, source: Source, mapping: dict, discovered_at=None, reference=None):
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file, strict=True)
        headers = reader.fieldnames or []
        normalized = [h.strip().casefold() for h in headers]
        if len(set(normalized)) != len(normalized):
            raise ValueError("CSV contains duplicate column headers")
        if not headers:
            raise ValueError("CSV has no header")
        aliases = {key: [key, *names] for key, names in mapping.items()}
        # All canonical fields work without explicitly listing them in YAML.
        for key in Prospect.model_fields:
            aliases.setdefault(key, [key])
        for number, raw in enumerate(reader, 2):
            if not any(v for v in raw.values()):
                continue
            if None in raw:
                yield ParsedRow(number, None, "more values than column headers")
                continue
            values = {k.strip().casefold(): (v.strip() if v else "") for k, v in raw.items()}
            data = {}
            for key, names in aliases.items():
                for alias in names:
                    if values.get(alias.casefold()):
                        data[key] = values[alias.casefold()]
                        break
            data.update(source=source, source_reference=f"{reference or path.name}:{number}")
            if discovered_at and not data.get("discovered_at"):
                data["discovered_at"] = discovered_at
            try:
                yield ParsedRow(number, Prospect.model_validate(data))
            except ValidationError as exc:
                # Do not put rejected row contents (PII) in operational logs.
                errors = "; ".join(
                    f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
                    for e in exc.errors(include_input=False)
                )
                yield ParsedRow(number, None, errors)


class LinkedInAdapter:
    source = Source.linkedin

    @staticmethod
    def manual(**values):
        return Prospect(source=Source.linkedin, **values)

    @classmethod
    def parse(cls, path, mapping, discovered_at=None):
        return parse_csv(path, cls.source, mapping, discovered_at)


class GrokAdapter:
    source = Source.grok

    @classmethod
    def parse(cls, path, mapping, discovered_at=None):
        return parse_csv(path, cls.source, mapping, discovered_at)

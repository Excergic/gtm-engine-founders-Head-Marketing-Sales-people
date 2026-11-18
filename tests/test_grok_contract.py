"""The Grok CSV contract: 13 columns, validated by the Pydantic Prospect model (the same
parser the warehouse import uses)."""

import csv
import os
import time
from pathlib import Path

import pytest

from gtm.config import Settings
from gtm.ingestion import parse_csv
from gtm.models import Prospect, Source

COLUMNS = [
    "name", "title", "company", "company_website", "country", "city", "linkedin_url",
    "x_handle", "source_url", "signal", "signal_date", "why_fit", "outreach_angle",
]  # fmt: skip


def grok_row(**overrides):
    row = {
        "name": "Riley Synthetic",
        "title": "Chief Revenue Officer",
        "company": "Synthetic Cloud",
        "company_website": "https://www.synthetic-cloud.example",
        "country": "USA",
        "city": "Austin",
        "linkedin_url": "https://www.linkedin.com/in/riley-synthetic/",
        "x_handle": "https://x.com/riley_synth",
        "source_url": "https://news.example/synthetic-cloud-series-a",
        "signal": "Raised Series A; hiring first GTM engineer",
        "signal_date": "2026-11-01",
        "why_fit": "New CRO building outbound from scratch",
        "outreach_angle": "Offer a signal-based outbound system for the first 90 days",
    }
    row.update(overrides)
    return row


def write(path, rows, *, settled=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    if settled:  # the importer skips files modified in the last minute (still writing)
        old = time.time() - 600
        os.utime(path, (old, old))
    return path


def parse(tmp_path, **overrides):
    mapping = Settings(config_directory=Path(__file__).parents[1] / "config").rules()
    path = write(tmp_path / "d.csv", [grok_row(**overrides)])
    (row,) = list(parse_csv(path, Source.grok, mapping["mappings"]["grok"]))
    return row


def test_full_row_maps_every_column(tmp_path):
    p = parse(tmp_path).prospect
    assert p.full_name == "Riley Synthetic"
    assert p.job_title == "Chief Revenue Officer"
    assert p.domain == "synthetic-cloud.example"
    assert p.linkedin_url == "https://www.linkedin.com/in/riley-synthetic"
    assert p.x_handle == "@riley_synth"
    assert p.signal_date == "2026-11-01"
    assert str(p.source_url).startswith("https://news.example/")
    assert p.outreach_angle.startswith("Offer a signal-based")


def test_missing_signal_fields_still_parse(tmp_path):
    # Kept and flagged for review by the import (see test_warehouse), never dropped.
    p = parse(tmp_path, signal="", signal_date="", source_url="").prospect
    assert p is not None and not p.signal and not p.signal_date


@pytest.mark.parametrize("value,precision", [("2026-10", "month"), ("2026", "year")])
def test_imprecise_signal_date_is_kept_not_invented(tmp_path, value, precision):
    p = parse(tmp_path, signal_date=value).prospect
    assert p.signal_date == value and p.signal_date_precision == precision


@pytest.mark.parametrize(
    "overrides,error",
    [
        ({"signal_date": "2099-01-01"}, "signal_date"),
        ({"signal_date": "Oct 26"}, "signal_date"),
        ({"signal_date": "2026-13-01"}, "signal_date"),
        ({"source_url": "not a url"}, "source_url"),
        ({"x_handle": "https://facebook.com/riley"}, "x_handle"),
        ({"linkedin_url": "https://www.linkedin.com/company/synthetic"}, "linkedin_url"),
        ({"name": "R"}, "full_name"),
    ],
)
def test_invalid_values_are_rejected(tmp_path, overrides, error):
    row = parse(tmp_path, **overrides)
    assert row.prospect is None and row.error.startswith(error)


def test_x_handle_forms_normalize():
    for value in ("@riley_synth", "riley_synth", "x.com/riley_synth", "twitter.com/riley_synth"):
        assert Prospect(full_name="Riley S", source=Source.grok, x_handle=value).x_handle == (
            "@riley_synth"
        )


def test_unknown_mapping_field_fails_config_load(tmp_path):
    import yaml

    rules = yaml.safe_load((Path(__file__).parents[1] / "config/gtm.yaml").read_text())
    rules["mappings"]["grok"]["signl"] = ["signl"]
    (tmp_path / "gtm.yaml").write_text(yaml.safe_dump(rules))
    with pytest.raises(ValueError, match="not in the Prospect model: signl"):
        Settings(config_directory=tmp_path).rules()

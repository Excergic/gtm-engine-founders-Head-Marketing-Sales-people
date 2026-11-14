"""The Grok CSV contract: 13 columns, validated by the Pydantic Prospect model."""

import copy
import csv

import pytest

from gtm.config import Settings
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


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_full_row_maps_every_column(system):
    service, settings = system
    result = service.import_file(write(settings.grok_directory / "d.csv", [grok_row()]), "grok")
    assert result["created"] == 1
    row = service.store.load()[0]
    assert row["full_name"] == "Riley Synthetic"
    assert row["job_title"] == "Chief Revenue Officer"
    assert row["domain"] == "synthetic-cloud.example"
    assert row["linkedin_url"] == "https://www.linkedin.com/in/riley-synthetic"
    assert row["x_handle"] == "@riley_synth"
    assert row["signal_date"] == "2026-11-01"
    assert row["source_url"].startswith("https://news.example/")
    assert row["outreach_angle"].startswith("Offer a signal-based")
    assert row["needs_review"] == "false"


def test_missing_signal_fields_are_captured_and_flagged(system):
    service, settings = system
    path = write(
        settings.grok_directory / "d.csv", [grok_row(signal="", signal_date="", source_url="")]
    )
    assert service.import_file(path, "grok")["created"] == 1
    row = service.store.load()[0]
    assert row["needs_review"] == "true"
    assert "Missing signal fields: signal, signal_date, source_url" in row["review_reason"]


@pytest.mark.parametrize("value,precision", [("2026-10", "month"), ("2026", "year")])
def test_imprecise_signal_date_is_kept_not_invented(system, value, precision):
    service, settings = system
    path = write(settings.grok_directory / "d.csv", [grok_row(signal_date=value)])
    service.import_file(path, "grok")
    row = service.store.load()[0]
    assert row["signal_date"] == value
    assert row["needs_review"] == "true"
    assert f"{precision} precision only" in row["review_reason"]


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
def test_invalid_values_are_rejected_without_writing(system, overrides, error):
    service, settings = system
    path = write(settings.grok_directory / "d.csv", [grok_row(**overrides)])
    result = service.import_file(path, "grok")
    assert result["invalid"] == 1 and result["created"] == 0
    assert result["rows"][0]["error"].startswith(error)


def test_x_handle_forms_normalize():
    for value in ("@riley_synth", "riley_synth", "x.com/riley_synth", "twitter.com/riley_synth"):
        assert Prospect(full_name="Riley S", source=Source.grok, x_handle=value).x_handle == (
            "@riley_synth"
        )


def test_unknown_mapping_field_fails_config_load(tmp_path):
    from pathlib import Path

    import yaml

    rules = yaml.safe_load((Path(__file__).parents[1] / "config/gtm.yaml").read_text())
    rules["mappings"]["grok"]["signl"] = ["signl"]
    (tmp_path / "gtm.yaml").write_text(yaml.safe_dump(rules))
    with pytest.raises(ValueError, match="not in the Prospect model: signl"):
        Settings(config_directory=tmp_path).rules()


def test_mapping_edits_do_not_change_import_fingerprints(system):
    service, settings = system
    path = write(settings.grok_directory / "d.csv", [grok_row()])
    before = service.file_key(path, "grok")
    service.rules = copy.deepcopy(service.rules)
    service.rules["mappings"]["grok"]["country"] = ["country", "nation"]
    assert service.file_key(path, "grok") == before

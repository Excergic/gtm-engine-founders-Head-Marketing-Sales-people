import csv
import json
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gtm.cli import app
from gtm.inbox import GrokInbox
from gtm.models import normalize_domain, normalize_linkedin, utcnow
from gtm.state import State


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(dict.fromkeys(k for r in rows for k in r)))
        writer.writeheader()
        writer.writerows(rows)
    return path


def lead(**extra):
    return {
        "name": "Alex Synthetic",
        "company": "Synthetic Co",
        "company_website": "https://www.synthetic.example/about",
        "linkedin_url": "linkedin.com/in/alex-synthetic?trk=test",
        **extra,
    }


def job_for(system, **kwargs):
    service, settings = system
    return GrokInbox(service, settings, sleep=kwargs.get("sleep", lambda _: None))


def test_existing_grok_schema_no_verification_columns_needed(system):
    service, settings = system
    path = write_csv(
        settings.grok_directory / "daily.csv",
        [
            lead(
                country="Synthetic Land",
                signal="Synthetic funding",
                why_fit="Relevant founder",
                outreach_angle="Custom intro",
            )
        ],
    )
    result = job_for(system).run()
    assert result["files"][0]["created"] == 1
    row = service.store.load()[0]
    assert row["domain"] == "synthetic.example"
    assert row["country"] == "Synthetic Land" and row["signal"] == "Synthetic funding"
    assert row["outreach_angle"] == "Custom intro"
    assert path.exists()
    assert len(list((settings.data_directory / "processed").glob("*.csv"))) == 1


def test_repeat_and_renamed_file_skips(system):
    service, settings = system
    p = write_csv(settings.grok_directory / "a.csv", [lead()])
    first = job_for(system).run()
    assert first["files"][0]["created"] == 1
    (p.parent / "b.csv").write_bytes(p.read_bytes())
    again = job_for(system).run()
    assert sum(f["skipped"] for f in again["files"]) == 2
    assert len(service.store.load()) == 1


def test_same_person_two_sources_and_objectives(system):
    service, settings = system
    a = write_csv(settings.grok_directory / "a.csv", [lead(objective="consulting_client")])
    b = write_csv(settings.linkedin_directory / "b.csv", [lead(objective="remote_job")])
    service.import_file(a, "grok")
    service.import_file(b, "linkedin")
    rows = service.store.load()
    assert len(rows) == 1
    assert rows[0]["sources"] == "grok|linkedin"
    assert rows[0]["objectives"] == "consulting_client|remote_job"
    assert rows[0]["original_source"] == "grok"
    assert len(json.loads(rows[0]["source_data"])) == 2


def test_missing_email_domain_and_linkedin_captured_for_review(system):
    service, settings = system
    p = write_csv(
        settings.grok_directory / "weak.csv",
        [{"name": "Taylor Synthetic", "company": "Unknown Synthetic"}],
    )
    result = service.import_file(p, "grok")
    assert result["created"] == 1
    assert service.store.load()[0]["needs_review"] == "true"
    assert service.import_file(p, "grok")["skipped"] == 1


def test_missing_domain_with_strong_identity_is_allowed(system):
    service, settings = system
    p = write_csv(
        settings.grok_directory / "no-domain.csv",
        [{"name": "Alex Synthetic", "email": "ALEX@synthetic.example"}],
    )
    service.import_file(p, "grok")
    assert service.store.load()[0]["email"] == "alex@synthetic.example"


def test_multiple_people_same_company_do_not_merge(system):
    service, settings = system
    p = write_csv(
        settings.grok_directory / "two.csv",
        [lead(), lead(name="Taylor Synthetic", linkedin_url="linkedin.com/in/taylor-synthetic")],
    )
    assert service.import_file(p, "grok")["created"] == 2
    assert len({r["domain"] for r in service.store.load()}) == 1


def test_ambiguous_name_company_held_without_merge(system):
    service, settings = system
    p = write_csv(settings.grok_directory / "a.csv", [lead()])
    service.import_file(p, "grok")
    q = write_csv(settings.grok_directory / "b.csv", [lead(linkedin_url="")])
    assert service.import_file(q, "grok")["review"] == 1
    assert len(service.store.load()) == 1


def test_conflicting_strong_identities_go_to_review(system):
    service, settings = system
    p = write_csv(settings.grok_directory / "a.csv", [lead(email="first@synthetic.example")])
    service.import_file(p, "grok")
    q = write_csv(settings.grok_directory / "b.csv", [lead(email="other@synthetic.example")])
    assert service.import_file(q, "grok")["review"] == 1
    assert service.store.load()[0]["email"] == "first@synthetic.example"


def test_new_observation_preserves_manual_notes_status_and_unknown_columns(system):
    service, settings = system
    p = write_csv(settings.grok_directory / "a.csv", [lead(notes="Initial note")])
    service.import_file(p, "grok")
    person = service.store.load()[0]
    with service.state.lock():
        service.update(person["id"], status="connected", notes="Manual note")
    rows = service.store.load()
    rows[0]["my_custom_column"] = "Keep this"
    service.store.write(rows)
    q = write_csv(settings.grok_directory / "b.csv", [lead(notes="", title="Founder")])
    service.import_file(q, "grok")
    person = service.store.load()[0]
    assert person["connection_status"] == "connected"
    assert "Manual note" in person["notes"] and "Initial note" in person["notes"]
    assert person["my_custom_column"] == "Keep this"
    assert len(list((settings.tracker.parent / "backups").glob("*.csv"))) >= 2


def test_partial_file_success_survives_invalid_row(system):
    service, settings = system
    write_csv(
        settings.grok_directory / "partial.csv",
        [lead(), lead(name="Invalid Synthetic", email="broken")],
    )
    result = job_for(system).run()
    assert result["files"][0]["created"] == 1 and result["files"][0]["invalid"] == 1
    assert len(service.store.load()) == 1
    assert job_for(system).run(retry_only=True)["files"][0]["skipped"] == 1
    assert len(list((settings.data_directory / "failed").glob("*.csv"))) == 1


def test_crash_after_master_write_before_checkpoint_recovers(system, monkeypatch):
    service, settings = system
    write_csv(settings.grok_directory / "crash.csv", [lead()])
    original = service.state.result
    monkeypatch.setattr(
        service.state, "result", lambda *a, **k: (_ for _ in ()).throw(SystemExit())
    )
    with pytest.raises(SystemExit):
        job_for(system).run()
    assert len(service.store.load()) == 1
    monkeypatch.setattr(service.state, "result", original)
    result = job_for(system).run(retry_only=True)
    assert result["files"][0]["skipped"] == 1
    assert len(service.store.load()) == 1
    assert any(r["status"] == "interrupted" for r in service.state.history())


def test_atomic_replace_failure_keeps_master_and_retry_works(system, monkeypatch):
    service, settings = system
    a = write_csv(settings.grok_directory / "a.csv", [lead()])
    service.import_file(a, "grok")
    before = settings.tracker.read_bytes()
    b = write_csv(
        settings.grok_directory / "b.csv",
        [lead(name="Taylor Synthetic", linkedin_url="linkedin.com/in/taylor-synthetic")],
    )
    import gtm.store as store_module

    original = store_module.os.replace
    monkeypatch.setattr(
        store_module.os,
        "replace",
        lambda *args: (_ for _ in ()).throw(OSError("synthetic disk failure")),
    )
    with pytest.raises(OSError):
        service.import_file(b, "grok")
    assert settings.tracker.read_bytes() == before
    monkeypatch.setattr(store_module.os, "replace", original)
    assert service.import_file(b, "grok")["created"] == 1


def test_incomplete_files_and_temporary_files_deferred(system):
    _, settings = system
    p = write_csv(settings.grok_directory / "daily.csv", [lead()])
    (p.parent / ".hidden.csv").write_text("not csv")
    (p.parent / "daily.csv.tmp").write_text("unfinished")

    def mutate(_):
        with p.open("a") as file:
            file.write("\n")

    result = job_for(system, sleep=mutate).run()
    assert not result["files"] and len(result["deferred"]) == 1
    assert not settings.tracker.exists()


def test_simultaneous_writer_rejected(system):
    service, settings = system
    other = State(settings.database)
    try:
        with service.state.lock():
            with pytest.raises(RuntimeError, match="another GTM writer"):
                with other.lock():
                    pass
    finally:
        other.close()


def test_preview_uses_batch_overlay_and_writes_nothing(system):
    service, settings = system
    write_csv(settings.grok_directory / "a.csv", [lead()])
    write_csv(settings.grok_directory / "b.csv", [lead(title="Founder")])
    before = len(service.state.history())
    result = job_for(system).run(dry_run=True)
    assert sum(f["created"] for f in result["files"]) == 1
    assert sum(f["updated"] for f in result["files"]) == 1
    assert not settings.tracker.exists()
    assert not (settings.data_directory / "processed").exists()
    assert len(service.state.history()) == before


def test_followups_complete_stays_complete_after_reimport(system):
    service, settings = system
    p = write_csv(settings.grok_directory / "a.csv", [lead()])
    service.import_file(p, "grok")
    person_id = service.store.load()[0]["id"]
    service.update(person_id, status="connected")
    action = service.actions()[0]
    service.complete(action["id"])
    q = write_csv(settings.grok_directory / "b.csv", [lead(title="Founder")])
    service.import_file(q, "grok")
    assert not service.actions()


def test_pending_seven_days_due_and_dnc(system):
    service, settings = system
    day = utcnow() - timedelta(days=8)
    p = write_csv(
        settings.grok_directory / "pending.csv",
        [lead(connection_status="connection_pending", connection_request_at=day.isoformat())],
    )
    service.import_file(p, "grok")
    assert service.actions()[0]["action"] == "Review pending connection"
    person_id = service.store.load()[0]["id"]
    service.update(person_id, do_not_contact=True)
    assert not service.actions()


def test_due_date_and_separate_stages(system):
    service, settings = system
    p = write_csv(settings.grok_directory / "a.csv", [lead()])
    service.import_file(p, "grok")
    person_id = service.store.load()[0]["id"]
    service.update(
        person_id,
        followup=(utcnow() - timedelta(days=1)).isoformat(),
        objective="consulting_client",
        stage="qualified",
    )
    service.update(person_id, objective="remote_job", stage="role_researched")
    row = service.store.load()[0]
    assert row["consulting_stage"] == "qualified" and row["job_stage"] == "role_researched"
    assert len(service.actions()) == 1
    with pytest.raises(ValueError, match="valid"):
        service.update(person_id, objective="remote_job", stage="won")


def test_corrupt_master_refuses_to_overwrite(system):
    service, settings = system
    settings.tracker.parent.mkdir(parents=True, exist_ok=True)
    settings.tracker.write_text("wrong,headers\ninvalid,content\n")
    p = write_csv(settings.grok_directory / "a.csv", [lead()])
    with pytest.raises(ValueError, match="headers"):
        service.import_file(p, "grok")
    assert settings.tracker.read_text().startswith("wrong,headers")


def test_duplicate_ids_in_master_rejected(system):
    service, settings = system
    p = write_csv(settings.grok_directory / "a.csv", [lead()])
    service.import_file(p, "grok")
    rows = service.store.load()
    service.store.write(rows + rows)
    with pytest.raises(ValueError, match="duplicate IDs"):
        service.store.load()


@pytest.mark.parametrize(
    "raw,expected",
    [("WWW.Example.COM/a?q=1", "example.com"), ("https://bücher.example", "xn--bcher-kva.example")],
)
def test_normalization(raw, expected):
    assert normalize_domain(raw) == expected
    assert (
        normalize_linkedin("linkedin.com/in/Alex-Synthetic/?trk=1")
        == "https://www.linkedin.com/in/alex-synthetic"
    )


def test_cli_end_to_end_no_credentials(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GTM_CONFIG_DIRECTORY", str(Path(__file__).parents[1] / "config"))
    monkeypatch.setenv("LOCAL_TRACKER_CSV", str(tmp_path / "data/prospects.csv"))
    monkeypatch.setenv("LOCAL_STATE_DATABASE", str(tmp_path / "data/state/local.sqlite"))
    monkeypatch.setenv("GTM_DATA_DIRECTORY", str(tmp_path / "data"))
    monkeypatch.setenv("GROK_CSV_DIRECTORY", str(tmp_path / "data/incoming/grok"))
    monkeypatch.setenv("GROK_FILE_STABILITY_SECONDS", "0.1")
    write_csv(tmp_path / "data/incoming/grok/a.csv", [lead()])
    runner = CliRunner()
    preview = runner.invoke(app, ["import", "grok", "--all", "--dry-run"])
    assert preview.exit_code == 0, preview.output
    assert not (tmp_path / "data/state/local.sqlite").exists()
    assert not (tmp_path / "data/prospects.csv").exists()
    result = runner.invoke(app, ["import", "grok", "--all"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["files"][0]["created"] == 1
    for command in (
        ["daily"],
        ["health"],
        ["people"],
        ["followups"],
        ["import", "history"],
        ["import", "review"],
    ):
        result = runner.invoke(app, command)
        assert result.exit_code == 0, result.output
    health = json.loads(runner.invoke(app, ["health"]).output)
    assert health["mode"] == "local_csv" and health["people"] == 1
    assert runner.invoke(app, ["setup-attio"]).exit_code == 2


def test_original_synthetic_acceptance_fixtures(system):
    service, _ = system
    root = Path(__file__).parent / "fixtures"
    a = service.import_file(root / "grok.csv", "grok")
    b = service.import_file(root / "linkedin.csv", "linkedin")
    assert a["created"] == 10 and b["created"] == 2 and b["updated"] == 3
    assert len(service.store.load()) == 12
    assert sum(r["sources"] == "grok|linkedin" for r in service.store.load()) == 3
    assert service.import_file(root / "grok.csv", "grok")["skipped"] == 10


def test_founders_and_vps_folders_discovered_and_labeled(system):
    service, settings = system
    write_csv(settings.grok_directory / "founders/2026-11-10.csv", [lead()])
    write_csv(
        settings.grok_directory / "VPs/2026-11-10.csv",
        [lead(name="Taylor Synthetic", linkedin_url="linkedin.com/in/taylor-synthetic")],
    )
    result = job_for(system).run()
    assert sum(f["created"] for f in result["files"]) == 2
    assert {r["segments"] for r in service.store.load()} == {"founders", "VPs"}
    assert sum(f["skipped"] for f in job_for(system).run()["files"]) == 2


def test_identical_person_file_in_both_folders_preserves_both_segments(system):
    service, settings = system
    a = write_csv(settings.grok_directory / "founders/a.csv", [lead()])
    b = settings.grok_directory / "VPs/b.csv"
    b.parent.mkdir(parents=True)
    b.write_bytes(a.read_bytes())
    result = job_for(system).run()
    assert sum(f["created"] for f in result["files"]) == 1
    assert sum(f["updated"] for f in result["files"]) == 1
    rows = service.store.load()
    assert len(rows) == 1 and set(rows[0]["segments"].split("|")) == {"founders", "VPs"}
    assert {o["segment"] for o in json.loads(rows[0]["source_data"])} == {"founders", "VPs"}


def test_segment_dry_run_and_crash_snapshot_recovery(system, monkeypatch):
    service, settings = system
    write_csv(settings.grok_directory / "VPs/daily.csv", [lead()])
    preview = job_for(system).run(dry_run=True)
    assert preview["files"][0]["created"] == 1
    assert not settings.tracker.exists()
    original = service.state.result
    monkeypatch.setattr(
        service.state, "result", lambda *a, **k: (_ for _ in ()).throw(SystemExit())
    )
    with pytest.raises(SystemExit):
        job_for(system).run()
    monkeypatch.setattr(service.state, "result", original)
    retry = job_for(system).run(retry_only=True)
    assert retry["files"][0]["skipped"] == 1
    assert len(service.store.load()) == 1
    assert service.store.load()[0]["segments"] == "VPs"


def test_recursive_discovery_ignores_hidden_symlink_and_temp_folders(system, tmp_path):
    _, settings = system
    real = write_csv(settings.grok_directory / "founders/archive/a.csv", [lead()])
    write_csv(settings.grok_directory / ".hidden/b.csv", [lead()])
    external = write_csv(tmp_path / "external/c.csv", [lead()])
    (settings.grok_directory / "VPs").symlink_to(external.parent, target_is_directory=True)
    (real.parent / "incomplete.csv.tmp").write_text("unfinished")
    assert job_for(system).discover() == [real.absolute()]


def test_pre_segment_master_still_loads_without_migration(system):
    service, settings = system
    path = write_csv(settings.grok_directory / "a.csv", [lead()])
    service.import_file(path, "grok")
    rows = service.store.load()
    for row in rows:
        del row["segments"]
    with settings.tracker.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    assert service.store.load()[0]["segments"] == ""


def test_backups_are_rotated(tmp_path, monkeypatch):
    from gtm.store import prune_backups

    for i in range(5):
        (tmp_path / f"prospects-2026111{i}T000000000000-abcd{i}.csv").write_text("x")
    removed = prune_backups(tmp_path, "prospects", keep=2)
    assert len(removed) == 3
    remaining = sorted(p.name for p in tmp_path.glob("*.csv"))
    assert remaining == [
        "prospects-20261113T000000000000-abcd3.csv",
        "prospects-20261114T000000000000-abcd4.csv",
    ]
    monkeypatch.setenv("GTM_BACKUP_KEEP", "bad")
    from gtm.store import backup_keep

    assert backup_keep() == 30

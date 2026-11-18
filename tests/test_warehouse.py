"""Warehouse schema rules, run against a throwaway Postgres (never the real database).

Uses TEST_DATABASE_URL when set (a server where the tests may create databases);
otherwise starts a temporary local cluster with initdb/pg_ctl. Skips if neither exists.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from gtm import warehouse

POSTGRES_APP = Path("/Applications/Postgres.app/Contents/Versions/latest/bin")
GROK_COLUMNS = [
    "name", "title", "company", "company_website", "country", "city", "linkedin_url",
    "x_handle", "source_url", "signal", "signal_date", "why_fit", "outreach_angle",
]  # fmt: skip


def _binary(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    return str(POSTGRES_APP / name) if (POSTGRES_APP / name).exists() else None


@pytest.fixture(scope="session")
def server_url():
    if url := os.environ.get("TEST_DATABASE_URL"):
        yield url
        return
    initdb, pg_ctl = _binary("initdb"), _binary("pg_ctl")
    if not (initdb and pg_ctl):
        pytest.skip("no Postgres available: set TEST_DATABASE_URL or install Postgres")
    root = Path(tempfile.mkdtemp(prefix="gtm-pg-"))
    data = root / "data"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    subprocess.run(
        [initdb, "-D", str(data), "-U", "postgres", "--auth=trust", "-E", "UTF8"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [pg_ctl, "-D", str(data), "-l", str(root / "log"), "-w", "start",
         "-o", f"-p {port} -k {root} -c listen_addresses=127.0.0.1"],
        check=True,
        capture_output=True,
    )  # fmt: skip
    try:
        yield f"postgresql://postgres@127.0.0.1:{port}/postgres"
    finally:
        subprocess.run([pg_ctl, "-D", str(data), "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def blank_db(server_url):
    name = "gtm_test_" + uuid.uuid4().hex[:12]
    with psycopg.connect(server_url, autocommit=True) as admin:
        admin.execute(f'create database "{name}"')
    conn = psycopg.connect(server_url.rsplit("/", 1)[0] + "/" + name)
    yield conn
    conn.close()
    with psycopg.connect(server_url, autocommit=True) as admin:
        admin.execute(f'drop database "{name}" with (force)')


@pytest.fixture
def db(blank_db):
    warehouse.migrate(blank_db)
    return blank_db


def person(**overrides):
    row = {
        "name": "Ada Example",
        "title": "VP of Marketing",
        "company": "Example SaaS",
        "company_website": "https://example.com",
        "source_url": "https://example.com/news",
        "signal": "Raised a Series A",
        "signal_date": "2026-11-01",
        "segment": ["VPs"],
    }
    row.update(overrides)
    return row


def register(conn, **overrides) -> uuid.UUID:
    pid = conn.execute(
        "select ops.register_prospect(%s, null, 'system', 'test import')",
        (Jsonb(person(**overrides)),),
    ).fetchone()[0]
    conn.commit()
    return pid


def update(conn, pid, **changes) -> str:
    layer = conn.execute(
        "select ops.update_prospect(%s, %s, null, 'system', 'test')", (pid, Jsonb(changes))
    ).fetchone()[0]
    conn.commit()
    return layer


def layer_of(conn, pid) -> str:
    return conn.execute(
        "select layer from core.prospect_registry where prospect_id = %s", (pid,)
    ).fetchone()[0]


def rejected(conn, sql, params=(), *, match):
    """The statement (or its commit) must fail with an error mentioning `match`."""
    with pytest.raises(psycopg.Error, match=match):
        conn.execute(sql, params)
        conn.commit()
    conn.rollback()


def linkedin(tag):
    return f"https://www.linkedin.com/in/{tag}"


def golden(conn, tag="ada"):
    pid = register(conn, linkedin_url=linkedin(tag))
    update(conn, pid, email=f"{tag}@example.com", email_status="verified")
    return pid


def signal_for(conn, pid):
    sid = conn.execute(
        "insert into core.signals (prospect_id, signal, source) values (%s, 'Raised', 'grok')"
        " returning signal_id",
        (pid,),
    ).fetchone()[0]
    conn.commit()
    return sid


# --- migrations -----------------------------------------------------------------


def test_migrate_is_idempotent_and_creates_every_table(blank_db):
    first = warehouse.migrate(blank_db)
    assert first == [
        "0001_layered_warehouse.sql",
        "0002_access_roles.sql",
        "0003_raw_row_outcomes.sql",
    ]
    assert warehouse.migrate(blank_db) == []
    tables = {
        f"{s}.{t}"
        for s, t in blank_db.execute(
            "select table_schema, table_name from information_schema.tables"
            " where table_schema in ('raw','core','staging','silver','golden','outbound','ops')"
            " and table_type = 'BASE TABLE'"
        )
    }
    assert tables == {
        "raw.files", "raw.grok_rows", "raw.linkedin_rows", "raw.provider_responses",
        "core.prospect_registry", "core.companies", "core.signals", "core.enrichment",
        "core.person_research", "core.account_research", "core.score_snapshots",
        "staging.prospects", "silver.prospects", "golden.prospects",
        "outbound.messages", "outbound.events", "outbound.tasks", "outbound.deals",
        "ops.pipeline_runs", "ops.state_transitions", "ops.ai_outputs", "ops.schema_migrations",
        "ops.raw_row_outcomes",
    }  # fmt: skip
    assert all(row["state"] == "applied" for row in warehouse.status(blank_db))


def test_changed_migration_is_refused(blank_db, tmp_path):
    (tmp_path / "0001_first.sql").write_text("create table ops_probe (id int);")
    warehouse.migrate(blank_db, tmp_path)
    (tmp_path / "0001_first.sql").write_text("create table ops_probe (id bigint);")
    assert warehouse.status(blank_db, tmp_path)[0]["state"] == "changed"
    with pytest.raises(warehouse.WarehouseError, match="changed after it was applied"):
        warehouse.migrate(blank_db, tmp_path)


def test_each_migration_is_committed_by_itself(blank_db, server_url):
    name = blank_db.info.dbname
    warehouse.migrate(blank_db)
    blank_db.close()  # closing without commit must not lose anything
    with psycopg.connect(server_url.rsplit("/", 1)[0] + "/" + name) as fresh:
        assert all(row["state"] == "applied" for row in warehouse.status(fresh))


def test_failed_migration_rolls_back(blank_db, tmp_path):
    (tmp_path / "0001_bad.sql").write_text("create table half_done (id int); select 1/0;")
    with pytest.raises(psycopg.Error):
        warehouse.migrate(blank_db, tmp_path)
    blank_db.rollback()
    assert blank_db.execute("select to_regclass('public.half_done')").fetchone()[0] is None
    assert warehouse.status(blank_db, tmp_path)[0]["state"] == "pending"


# --- Grok column contract -----------------------------------------------------------


def test_every_layer_starts_with_the_13_grok_columns(db):
    raw = [
        r[0]
        for r in db.execute(
            "select column_name from information_schema.columns"
            " where table_schema = 'raw' and table_name = 'grok_rows' order by ordinal_position"
        )
    ]
    assert raw[4:17] == GROK_COLUMNS
    for layer in ("staging", "silver", "golden"):
        cols = [
            r[0]
            for r in db.execute(
                "select column_name from information_schema.columns"
                " where table_schema = %s and table_name = 'prospects' order by ordinal_position",
                (layer,),
            )
        ]
        assert cols[2:15] == GROK_COLUMNS, layer


def test_raw_rows_keep_bad_values_and_are_append_only(db):
    fid = db.execute(
        "insert into raw.files (source, segment, original_path, sha256, size_bytes)"
        " values ('grok', 'VPs', 'x.csv', %s, 10) returning file_id",
        ("a" * 64,),
    ).fetchone()[0]
    db.execute(
        "insert into raw.grok_rows (file_id, row_number, name, signal_date, validation_status)"
        " values (%s, 2, 'Ada', 'last week', 'invalid')",
        (fid,),
    )
    db.commit()
    rejected(db, "update raw.grok_rows set name = 'x'", match="append-only")
    rejected(db, "delete from raw.grok_rows", match="append-only")
    rejected(db, "delete from raw.files", match="append-only")


# --- layers --------------------------------------------------------------------------


def test_register_places_person_by_contact_data(db):
    assert layer_of(db, register(db)) == "staging"
    assert layer_of(db, register(db, linkedin_url=linkedin("li-only"))) == "silver"
    both = register(db, linkedin_url=linkedin("both"), email="b@x.com", email_status="verified")
    assert layer_of(db, both) == "golden"
    unverified = register(db, email="u@x.com", email_status="risky")
    assert layer_of(db, unverified) == "staging"


def test_enrichment_moves_staging_to_silver_to_golden(db):
    pid = register(db)
    assert update(db, pid, linkedin_url=linkedin("ada")) == "silver"
    assert update(db, pid, email="ada@example.com", email_status="risky") == "silver"
    assert update(db, pid, email_status="verified") == "golden"
    assert db.execute("select count(*) from staging.prospects").fetchone()[0] == 0
    assert db.execute("select count(*) from silver.prospects").fetchone()[0] == 0
    row = db.execute(
        "select name, email, linkedin_url from golden.prospects where prospect_id = %s", (pid,)
    ).fetchone()
    assert row == ("Ada Example", "ada@example.com", linkedin("ada"))
    moves = db.execute(
        "select from_layer, to_layer from ops.state_transitions where prospect_id = %s"
        " order by transition_id",
        (pid,),
    ).fetchall()
    assert moves == [(None, "staging"), ("staging", "silver"), ("silver", "golden")]


def test_layer_rules_are_enforced_by_the_database(db):
    pid = register(db)
    # Writing straight into a table that does not match the registry is refused.
    rejected(
        db,
        "update staging.prospects set linkedin_url = %s where prospect_id = %s",
        (linkedin("sneaky"), pid),
        match="layer_requirements",
    )
    no_email = uuid.uuid4()
    db.execute(
        "insert into core.prospect_registry (prospect_id, layer) values (%s, 'golden')",
        (no_email,),
    )
    rejected(
        db,
        "insert into golden.prospects (prospect_id, name, title, company, linkedin_url)"
        " values (%s, 'No Email', 'CMO', 'Co', %s)",
        (no_email, linkedin("no-email")),
        match="layer_requirements",
    )
    # A row can only live in the layer the registry says.
    other = register(db)
    rejected(
        db,
        "insert into silver.prospects (prospect_id, name, title, company, linkedin_url)"
        " values (%s, 'Dup', 'CMO', 'Co', %s)",
        (other, linkedin("dup-layer")),
        match="in_registered_layer",
    )


def test_people_never_move_down(db):
    pid = register(db, linkedin_url=linkedin("ada"))
    rejected(db, "select ops.update_prospect(%s, %s, null, 'system', 't')",
             (pid, Jsonb({"linkedin_url": None})), match="down from silver")  # fmt: skip
    gold = golden(db, "gold")
    assert update(db, gold, email_status="bounced") == "golden"


def test_email_and_linkedin_are_unique_across_layers(db):
    register(db, linkedin_url=linkedin("taken"))
    rejected(db, "select ops.register_prospect(%s, null, 'system', 't')",
             (Jsonb(person(linkedin_url=linkedin("taken"))),), match="linkedin_url_key")  # fmt: skip
    golden(db, "gold")
    staged = register(db)
    rejected(db, "select ops.update_prospect(%s, %s, null, 'system', 't')",
             (staged, Jsonb({"email": "gold@example.com", "email_status": "verified"})),
             match="email_key")  # fmt: skip


def test_lifecycle_changes_only_through_transition(db):
    pid = register(db)
    rejected(
        db,
        "update staging.prospects set lifecycle_stage = 'ready' where prospect_id = %s",
        (pid,),
        match="must go through ops.transition",
    )
    db.execute("select ops.transition(%s, 'researched', null, 'system', 'researched')", (pid,))
    db.commit()
    assert db.execute(
        "select lifecycle_stage from staging.prospects where prospect_id = %s", (pid,)
    ).fetchone() == ("researched",)
    assert db.execute(
        "select from_state, to_state from ops.state_transitions"
        " where prospect_id = %s order by transition_id desc limit 1",
        (pid,),
    ).fetchone() == ("scraped", "researched")
    rejected(db, "update ops.state_transitions set reason = 'edited'", match="append-only")


# --- outbound only on golden -------------------------------------------------------------


def test_outbound_requires_a_golden_prospect(db):
    silver = register(db, linkedin_url=linkedin("silver"))
    sid = signal_for(db, silver)
    rejected(
        db,
        "insert into outbound.messages (prospect_id, signal_id, channel, body)"
        " values (%s, %s, 'linkedin', 'hi')",
        (silver, sid),
        match="messages_prospect_id_fkey",
    )
    gold = golden(db)
    gid = signal_for(db, gold)
    db.execute(
        "insert into outbound.messages (prospect_id, signal_id, channel, body)"
        " values (%s, %s, 'email', 'hi')",
        (gold, gid),
    )
    db.commit()
    # A golden person with history cannot be deleted.
    rejected(
        db,
        "delete from golden.prospects where prospect_id = %s",
        (gold,),
        match="messages_prospect_id_fkey",
    )


def test_signals_are_append_only_except_message_link(db):
    gold = golden(db)
    sid = signal_for(db, gold)
    mid = db.execute(
        "insert into outbound.messages (prospect_id, signal_id, channel, body)"
        " values (%s, %s, 'email', 'hi') returning message_id",
        (gold, sid),
    ).fetchone()[0]
    db.execute("update core.signals set used_in_message_id = %s where signal_id = %s", (mid, sid))
    db.commit()
    rejected(
        db,
        "update core.signals set signal = 'edited' where signal_id = %s",
        (sid,),
        match="append-only",
    )
    rejected(db, "delete from core.signals where signal_id = %s", (sid,), match="append-only")


def test_outbound_role_cannot_read_raw_staging_or_silver(db):
    for schema in ("raw", "staging", "silver"):
        assert not db.execute(
            "select has_schema_privilege('gtm_outbound', %s, 'usage')", (schema,)
        ).fetchone()[0]
    assert db.execute(
        "select has_table_privilege('gtm_outbound', 'golden.prospects', 'select')"
    ).fetchone()[0]


def test_unknown_columns_are_refused(db):
    rejected(db, "select ops.register_prospect(%s, null, 'system', 't')",
             (Jsonb(person(full_name="Old Name")),), match="unknown prospect columns: full_name")  # fmt: skip


# --- loading existing local data -------------------------------------------------------


def test_load_existing_places_people_and_keeps_raw_files(db, system):
    from test_grok_contract import grok_row, write

    from gtm.warehouse_load import layer_counts, load_existing

    service, settings = system
    db.commit()
    path = write(
        settings.grok_directory / "founders" / "2026-11-01_founders.csv",
        [
            grok_row(),
            grok_row(name="Sam Nolink", linkedin_url="", company_website="https://b.example"),
            grok_row(name="Bad Date", linkedin_url="", signal_date="last week"),
        ],
    )
    service.import_file(path, "grok")
    rows = service.store.load()
    rows[0]["x_handle"] = "riley_synth"  # older master rows store handles without "@"
    with service.state.lock():
        service.write_rows(rows)
    with service.state.lock():  # writes plain-text score_reasons, as the real master has
        service.write_rows(service.store.load())

    result = load_existing(db, settings.data_directory, settings.tracker, settings.rules())
    assert result["problems"] == []
    counts = layer_counts(db)
    assert counts["raw.files"] == 1
    assert (counts["raw.grok_rows valid"], counts["raw.grok_rows review"]) == (1, 1)
    assert counts["raw.grok_rows invalid"] == 1
    assert (counts["staging.prospects"], counts["silver.prospects"]) == (1, 1)
    assert counts["golden.prospects"] == 0
    assert counts["core.signals"] == 2 and counts["core.companies"] == 2

    copied = settings.data_directory / "raw"
    assert [p.read_bytes() for p in copied.rglob("*.csv")] == [path.read_bytes()]
    assert (
        db.execute(
            "select count(*) from core.prospect_registry where first_raw_row_id is not null"
        ).fetchone()[0]
        == 2
    )
    silver = db.execute("select name, segment, linkedin_url from silver.prospects").fetchone()
    assert silver == (
        "Riley Synthetic",
        ["founders"],
        "https://www.linkedin.com/in/riley-synthetic",
    )

    assert db.execute("select x_handle from silver.prospects").fetchone() == ("@riley_synth",)
    assert (
        next(r for r in __import__("csv").DictReader(settings.tracker.open()))["x_handle"]
        == "riley_synth"
    )

    rerun = load_existing(db, settings.data_directory, settings.tracker, settings.rules())
    assert rerun["counts"]["people_already_present"] == 2
    assert layer_counts(db) == counts


# --- importing into the warehouse ---------------------------------------------------------


def _import(db, settings):
    from gtm.warehouse_import import WarehouseImporter

    return WarehouseImporter(db, settings.data_directory, settings.rules()).run()


def test_import_records_one_outcome_per_row_and_follows_identity_rules(db, system):
    from test_grok_contract import grok_row, write

    from gtm.warehouse_import import resolve, review_queue

    _, settings = system
    db.commit()
    sam = grok_row(name="Sam Nolink", linkedin_url="", company_website="https://b.example")
    write(settings.grok_directory / "VPs" / "2026-11-12_vps.csv", [grok_row(), sam])
    first = _import(db, settings)
    assert first["outcomes"] == {"registered": 2}

    write(
        settings.grok_directory / "founders" / "2026-11-13_founders.csv",
        [
            # Same LinkedIn URL, newer signal: merged, signal appended, founders segment added.
            grok_row(signal="Hired a VP of Sales", signal_date="2026-11-10", city=""),
            sam,  # identical row again: duplicate
            {**sam, "signal": "Opened a RevOps role"},  # same name+company, no key: review
            grok_row(name="New Person", linkedin_url="", company_website="https://c.example"),
            grok_row(name="Bad Date", linkedin_url="", signal_date="soon"),
        ],
    )
    second = _import(db, settings)
    assert second["outcomes"] == {
        "merged": 1, "duplicate": 1, "held_for_review": 1, "registered": 1, "invalid": 1,
    }  # fmt: skip
    assert second["problems"] == []

    riley = db.execute(
        "select segment, signal, signal_date, city from silver.prospects where name = %s",
        ("Riley Synthetic",),
    ).fetchone()
    assert riley == (["VPs", "founders"], "Hired a VP of Sales", "2026-11-10", "Austin")
    assert db.execute(
        "select count(*) from core.signals s join silver.prospects p using (prospect_id)"
        " where p.name = 'Riley Synthetic'"
    ).fetchone() == (2,)

    # Every raw row has exactly one outcome; a rerun changes nothing.
    assert db.execute(
        "select count(*) from raw.grok_rows g left join ops.raw_row_outcomes o using (raw_row_id)"
        " where o.raw_row_id is null"
    ).fetchone() == (0,)
    assert _import(db, settings)["outcomes"] == {}

    queue = review_queue(db)
    assert len(queue) == 1 and queue[0]["candidate"]["name"] == "Sam Nolink"
    result = resolve(
        db, settings.data_directory, settings.rules(), queue[0]["raw_row_id"], "merge", "tester"
    )
    assert result["resolution"] == "merge"
    assert review_queue(db) == []
    assert db.execute(
        "select signal from staging.prospects where name = 'Sam Nolink'"
    ).fetchone() == ("Raised Series A; hiring first GTM engineer",)  # older signal stays current
    assert db.execute(
        "select count(*) from core.signals s join staging.prospects p using (prospect_id)"
        " where p.name = 'Sam Nolink'"
    ).fetchone() == (2,)


def test_import_after_initial_load_does_not_reimport_loaded_rows(db, system):
    from test_grok_contract import grok_row, write

    from gtm.warehouse_load import load_existing

    service, settings = system
    db.commit()
    path = write(settings.grok_directory / "VPs" / "2026-11-01_vps.csv", [grok_row()])
    service.import_file(path, "grok")
    load_existing(db, settings.data_directory, settings.tracker, settings.rules())
    assert _import(db, settings)["outcomes"] == {}
    assert db.execute("select outcome from ops.raw_row_outcomes").fetchall() == [("migrated",)]


# --- CSV views -----------------------------------------------------------------------------


def _view(settings):
    return settings.data_directory / "views" / "prospects.csv"


def _rewrite(path, edit):
    import csv as _csv

    with path.open(newline="") as file:
        reader = _csv.DictReader(file)
        columns, rows = reader.fieldnames, list(reader)
    for row in rows:
        edit(row)
    with path.open("w", newline="") as file:
        writer = _csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def test_view_edits_are_validated_and_applied_in_one_transaction(db, system):
    from gtm.warehouse_views import apply_view_edits, export_views

    _, settings = system
    pid = register(db, linkedin_url=linkedin("ada"))
    register(db, name="Bo Other", company="Other Co")
    db.autocommit = True
    views = settings.data_directory / "views"
    assert export_views(db, views)["prospects.csv"] == 2

    def add_email(row):
        if row["prospect_id"] == str(pid):
            row["email"] = "Ada@Example.com"
            row["email_status"] = "verified"
            row["notes"] = "met at conference"

    _rewrite(_view(settings), add_email)
    result = apply_view_edits(db, views)
    assert result["errors"] == [] and result["layers"] == {str(pid): "golden"}
    assert db.execute(
        "select email, notes from golden.prospects where prospect_id = %s", (pid,)
    ).fetchone() == ("ada@example.com", "met at conference")

    # Read-only column edits and stale rows are refused, and nothing is written.
    export_views(db, views)
    _rewrite(_view(settings), lambda row: row.update(tier="tier_1", notes="x"))
    refused = apply_view_edits(db, views)
    assert refused["applied"] == 0 and any("tier is read-only" in e for e in refused["errors"])
    assert db.execute("select count(*) from core.all_prospects where notes = 'x'").fetchone() == (
        0,
    )

    export_views(db, views)
    db.execute("select ops.update_prospect(%s, %s, null, 'system', 't')",
               (pid, Jsonb({"owner": "someone"})))  # fmt: skip
    _rewrite(_view(settings), lambda row: row.update(notes="stale edit"))
    stale = apply_view_edits(db, views)
    assert any("changed in the warehouse after export" in e for e in stale["errors"])

    # A database rule failure rolls back every row in the batch.
    export_views(db, views)

    def break_one(row):
        row["notes"] = "batch"
        if row["name"] == "Bo Other":
            row["email_status"] = "verified"  # verified without an email: refused

    _rewrite(_view(settings), break_one)
    with pytest.raises(psycopg.Error):
        apply_view_edits(db, views)
    assert db.execute(
        "select count(*) from core.all_prospects where notes = 'batch'"
    ).fetchone() == (0,)


# --- enrichment -------------------------------------------------------------------------------


def test_enrichment_batch_prefers_silver_and_skips_golden_and_dnc(db):
    from gtm.enrichment import select_batch

    staged = register(db, signal_date="2026-11-10")
    silver = register(
        db, name="Li Only", linkedin_url=linkedin("li-only"), signal_date="2026-10-01"
    )
    golden(db, "gold")
    blocked = register(db, name="No Contact")
    update(db, blocked, do_not_contact=True)
    batch = select_batch(db, 10)
    assert [p["prospect_id"] for p in batch] == [str(silver), str(staged)]
    assert batch[0]["needs"] == ["email"] and batch[1]["needs"] == ["linkedin_url", "email"]


def test_findings_fill_blanks_record_provenance_and_promote(db):
    from gtm.enrichment import Finding, apply_findings

    silver = register(db, name="Li Only", linkedin_url=linkedin("li-only"))
    staged = register(db, name="Nothing Yet")
    risky = register(db, name="Risky Mail", linkedin_url=linkedin("risky"))
    other = golden(db, "taken")
    findings = [
        Finding(prospect_id=str(silver), provider="clay", email="Li@Example.com",
                email_status="valid", confidence=0.9, response={"raw": 1}),
        Finding(prospect_id=str(staged), provider="vibe_prospecting",
                linkedin_url="linkedin.com/in/nothing-yet/"),
        Finding(prospect_id=str(risky), provider="clay", email="r@example.com",
                email_status="catch-all"),
        # Different LinkedIn for someone who already has one: kept, reported.
        Finding(prospect_id=str(silver), provider="vibe_prospecting",
                linkedin_url=linkedin("someone-else")),
        # Email that already belongs to another person: not applied.
        Finding(prospect_id=str(staged), provider="clay", email="taken@example.com",
                email_status="verified"),
    ]  # fmt: skip

    preview = apply_findings(db, findings, dry_run=True)
    assert preview["run_id"] is None and layer_of(db, silver) == "silver"

    result = apply_findings(db, findings)
    assert layer_of(db, silver) == "golden"
    assert layer_of(db, staged) == "silver"
    assert layer_of(db, risky) == "silver"  # catch-all is risky, not verified
    assert result["counts"]["promoted_silver_to_golden"] == 1
    assert result["counts"]["promoted_staging_to_silver"] == 1
    assert result["counts"]["conflicting_value_kept"] == 1
    assert result["counts"]["possible_duplicate"] == 1
    assert db.execute(
        "select email, email_status, linkedin_url from golden.prospects where prospect_id = %s",
        (silver,),
    ).fetchone() == ("li@example.com", "verified", linkedin("li-only"))
    assert db.execute(
        "select provider, field, value from core.enrichment where prospect_id = %s"
        " order by provider, field",
        (silver,),
    ).fetchall() == [
        ("clay", "email", "li@example.com"),
        ("clay", "email_status", "verified"),
        ("vibe_prospecting", "linkedin_url", linkedin("someone-else")),
    ]
    assert db.execute("select count(*) from raw.provider_responses").fetchone() == (5,)
    assert db.execute(
        "select email from golden.prospects where prospect_id = %s", (other,)
    ).fetchone() == ("taken@example.com",)


def test_findings_reject_invented_looking_values(db):
    from pydantic import ValidationError

    from gtm.enrichment import Finding

    pid = str(register(db))
    for bad in (
        {"linkedin_url": "https://www.linkedin.com/company/acme"},
        {"email": "not-an-email"},
        {"email": "a@b.com", "email_status": "probably fine"},
    ):
        with pytest.raises(ValidationError):
            Finding(prospect_id=pid, provider="clay", **bad)


# --- enrichment waterfall and provider adapters -------------------------------------------


def _clay_file(directory, requests, contacts):
    import json as _json

    directory.mkdir(parents=True, exist_ok=True)
    (directory / "run.json").write_text(
        _json.dumps({"requests": requests, "task": {"contacts": contacts}})
    )


def test_clay_adapter_only_accepts_the_requested_person(tmp_path):
    from gtm.enrichers.clay import ClayResultsEnricher

    clay_dir = tmp_path / "enrichment" / "clay"
    _clay_file(
        clay_dir,
        [{"prospect_id": "p1", "contactName": "Jay Prakash", "companyIdentifier": "s.com"},
         {"prospect_id": "p2", "contactName": "Ann Lee", "companyIdentifier": "a.com"}],
        [{"name": "Jay Prakash, PhD", "companyIdentifier": "s.com",
          "url": "https://www.linkedin.com/in/jay/",
          "enrichments": [{"state": "completed", "value": '{"Work Email":"jp@s.com"}'}]},
         {"name": "Someone Else", "companyIdentifier": "a.com",
          "url": "https://www.linkedin.com/in/else/", "enrichments": []}],
    )  # fmt: skip
    clay = ClayResultsEnricher("clay", {"results_dir": "enrichment/clay"}, tmp_path)
    people = [{"prospect_id": "p1", "name": "Jay Prakash"},
              {"prospect_id": "p2", "name": "Ann Lee"}]  # fmt: skip
    emails = clay.find("email", people)
    assert [(f.prospect_id, f.email, f.email_status) for f in emails] == [("p1", "jp@s.com", None)]
    links = clay.find("linkedin_url", people)
    assert [(f.prospect_id, f.linkedin_url) for f in links] == [
        ("p1", "https://www.linkedin.com/in/jay")
    ]


def test_deepline_adapter_maps_plays_and_verdicts(tmp_path):
    from gtm.enrichers.deepline import DeeplineEnricher

    calls = []

    # Shapes copied from real `deepline plays run --json --full` / `tools execute` output.
    def runner(args, timeout):
        calls.append(args)
        ref = args[2]
        if ref == "li-play":
            return {"run": {"status": "completed"}, "result": {
                "linkedin_url": "https://www.linkedin.com/in/ann-lee",
                "identity_validation_status": "verified"}}  # fmt: skip
        if ref == "email-play":
            return {"result": {"email": "ann@a.com", "email_validated": False,
                               "email_validation_status": "catch_all", "miss_reason": None}}  # fmt: skip
        return {"toolResponse": {"rawV2": {"result": "deliverable", "score": 97}}}

    deepline = DeeplineEnricher(
        "deepline",
        {"linkedin_play": "li-play", "email_play": "email-play", "verify_tool": "verify-tool"},
        tmp_path,
        runner=runner,
    )
    person = {"prospect_id": "p2", "name": "Ann Lee", "company": "A", "domain": "a.com",
              "linkedin_url": None, "email": None}  # fmt: skip
    (li,) = deepline.find("linkedin_url", [person])
    assert li.linkedin_url == "https://www.linkedin.com/in/ann-lee"
    (em,) = deepline.find("email", [person])
    assert (em.email, em.email_status) == ("ann@a.com", "risky")
    (ver,) = deepline.find("verify", [{**person, "email": "ann@a.com"}])
    assert (ver.email_status, ver.confidence) == ("verified", 0.97)
    assert [c[:3] for c in calls] == [["plays", "run", "li-play"], ["plays", "run", "email-play"],
                                      ["tools", "execute", "verify-tool"]]  # fmt: skip
    assert "--full" in calls[0] and "--full" in calls[1]

    # A miss: candidates rejected inside the play's waterfall must never be used.
    def missing(args, timeout):
        if args[2] == "li-play":
            return {"result": {"linkedin_url": None, "identity_validation_status": "rejected",
                               "waterfall_attempts": {"primary": {
                                   "linkedin_url": "https://www.linkedin.com/in/someone-else"}}}}  # fmt: skip
        return {"result": {"email": None, "miss_reason": "no_valid_email",
                           "waterfall_attempts": {"x": {"email": "guess@a.com"}}}}  # fmt: skip

    miss = DeeplineEnricher("deepline", {"linkedin_play": "li-play", "email_play": "e"},
                            tmp_path, runner=missing)  # fmt: skip
    assert miss.find("linkedin_url", [{**person}]) == []
    assert miss.find("email", [{**person}]) == []

    # A preview without a result is an error, not a silent miss.
    preview = DeeplineEnricher("deepline", {"email_play": "e"}, tmp_path,
                               runner=lambda a, t: {"run": {"status": "completed"}})  # fmt: skip
    who = {**person}
    assert preview.find("email", [who]) == []
    assert who["errors"] == ["deepline.email: play response has no result object"]

    def failing(args, timeout):
        raise RuntimeError("insufficient credits")

    broke = DeeplineEnricher("deepline", {"email_play": "x"}, tmp_path, runner=failing)
    who = {**person}
    assert broke.find("email", [who]) == []
    assert who["errors"] == ["deepline.email: insufficient credits"]


def test_waterfall_asks_each_provider_only_for_whats_still_missing(db):
    from gtm.enrichment import Finding, run_waterfall

    a = register(db, name="Ann Lee")  # needs LinkedIn + email
    b = register(db, name="Bo Kim", linkedin_url=linkedin("bo"))  # needs email
    asked = []

    class Fake:
        def __init__(self, name, answers):
            self.name, self.answers = name, answers
            self.stages = frozenset({"linkedin_url", "email", "verify"})

        def find(self, stage, people):
            asked.append((self.name, stage, sorted(p["name"] for p in people)))
            out = []
            for p in people:
                value = self.answers.get((stage, p["name"]))
                if value:
                    field = {"verify": "email_status"}.get(stage, stage)
                    extra = {"email": p["email"]} if stage == "verify" else {}
                    out.append(Finding(prospect_id=p["prospect_id"], provider=self.name,
                                       **{field: value}, **extra))  # fmt: skip
            return out

    first = Fake("first", {("linkedin_url", "Ann Lee"): "https://www.linkedin.com/in/ann",
                           ("email", "Bo Kim"): "bo@b.com"})  # fmt: skip
    second = Fake("second", {("email", "Ann Lee"): "ann@a.com",
                             ("verify", "Ann Lee"): "deliverable",
                             ("verify", "Bo Kim"): "undeliverable"})  # fmt: skip
    waterfall = {"linkedin_url": ["first", "second"], "email": ["first", "second"],
                 "verify": ["second"]}  # fmt: skip
    ids = [str(a), str(b)]

    plan = run_waterfall(db, ids, waterfall, {"first": first, "second": second}, dry_run=True)
    assert asked == [] and plan["run_id"] is None  # a dry run never calls providers

    result = run_waterfall(db, ids, waterfall, {"first": first, "second": second})
    assert asked == [
        ("first", "linkedin_url", ["Ann Lee"]),
        # second is not asked for LinkedIn: nobody is missing it any more
        ("first", "email", ["Ann Lee", "Bo Kim"]),
        ("second", "email", ["Ann Lee"]),
        ("second", "verify", ["Ann Lee", "Bo Kim"]),
    ]
    assert layer_of(db, a) == "golden"
    assert layer_of(db, b) == "silver"  # undeliverable email: not golden
    assert result["layers_now"] == {"golden": 1, "silver": 1}
    assert db.execute("select count(distinct run_id) from core.enrichment").fetchone() == (
        1,
    )  # one run for the whole waterfall


def test_waterfall_survives_a_dropped_connection_and_journals_findings(db, tmp_path):
    """Neon suspends idle computes; a slow provider call must not lose paid results."""
    from gtm.enrichment import FileEnricher, Finding, run_waterfall

    pid = register(db, name="Ann Lee", linkedin_url=linkedin("ann"))
    dsn = db.info.dsn
    work = psycopg.connect(dsn)
    opened = []

    def connect():
        opened.append(psycopg.connect(dsn))
        return opened[-1]

    class Slow:
        name, stages = "slow", frozenset({"email"})

        def find(self, stage, people):
            work.close()  # the database drops the connection while the provider works
            return [Finding(prospect_id=p["prospect_id"], provider="slow", email="ann@a.com",
                            email_status="verified") for p in people]  # fmt: skip

    journal = tmp_path / "findings"
    result = run_waterfall(work, [str(pid)], {"email": ["slow"]}, {"slow": Slow()},
                           connect=connect, journal_dir=journal)  # fmt: skip
    assert len(opened) == 1 and result["errors"] == []
    assert layer_of(db, pid) == "golden"
    (saved,) = journal.glob("*_email_slow.json")
    assert [f.email for f in FileEnricher(saved).enrich([])] == ["ann@a.com"]
    for conn in opened:
        conn.close()

    # Without a way to reconnect the failure is raised, never swallowed.
    lost = psycopg.connect(dsn)
    lost.close()
    with pytest.raises(psycopg.OperationalError):
        run_waterfall(lost, [str(pid)], {"email": ["slow"]}, {"slow": Slow()})


def _icp_rules():
    from gtm.config import Settings

    return Settings.load().rules()


def _golden_person(conn, tag, **overrides):
    row = {"name": f"{tag.title()} Person", "linkedin_url": linkedin(tag),
           "outreach_angle": "Congrats on the round; how are you building pipeline?"}  # fmt: skip
    row.update(overrides)
    pid = register(conn, **row)
    update(conn, pid, email=f"{tag}@example.com", email_status="verified")
    return pid


def _stage(conn, pid):
    return conn.execute(
        "select lifecycle_stage from core.all_prospects where prospect_id = %s", (pid,)
    ).fetchone()[0]


def test_icp_segments_follow_the_icp():
    from gtm.icp import segment

    rules = _icp_rules()
    icp, scoring = rules["icp"], rules["scoring"]
    assert segment("Co-founder & CTO", icp, scoring) == "founder"
    assert segment("Founder & Managing Director", icp, scoring) == "founder"
    assert segment("SVP Global Sales", icp, scoring) == "gtm_leader"
    assert segment("Chief Revenue Officer", icp, scoring) == "gtm_leader"
    assert segment("Head of B2B Marketing", icp, scoring) == "gtm_leader"
    for outside in ("Enterprise Sales Director", "Director of GTM & Strategy",
                    "Marketing Manager", "Account Executive", "Vice President", None):  # fmt: skip
        assert segment(outside, icp, scoring) is None, outside


def test_ready_gate_scores_everyone_and_moves_only_golden(db):
    from datetime import date

    from gtm import icp

    today = date(2026, 11, 10)
    rules = _icp_rules()
    fresh = _golden_person(db, "fresh", title="Co-founder & CEO", signal="Raised a $9M seed",
                           signal_date="2026-11-01")  # fmt: skip
    stale = _golden_person(db, "stale", title="CMO", signal_date="2026-08-01")
    director = _golden_person(db, "dir", title="Sales Director", signal_date="2026-11-05")
    no_angle = _golden_person(db, "quiet", title="CRO", signal_date="2026-11-05",
                              outreach_angle=None)  # fmt: skip
    silver = register(db, name="Sil Ver", title="CEO", linkedin_url=linkedin("sil"),
                      signal_date="2026-11-05")  # fmt: skip
    busy = _golden_person(db, "busy", title="CEO", signal_date="2026-11-05")
    db.execute("select ops.transition(%s, 'in_sequence', null, 'system', 'test')", (busy,))
    db.commit()

    plan = icp.run(db, rules, today=today, dry_run=True)
    assert plan["ready"] == 1 and plan["run_id"] is None
    assert db.execute("select count(*) from core.score_snapshots").fetchone() == (0,)
    assert _stage(db, fresh) == "scraped"  # dry run moves nobody

    result = icp.run(db, rules, today=today)
    assert result["ready"] == 1
    assert _stage(db, fresh) == "ready"
    assert _stage(db, stale) == "golden"
    assert _stage(db, director) == "golden"
    assert _stage(db, no_angle) == "golden"
    assert _stage(db, silver) == "scraped"  # not golden: the gate never touches it
    assert _stage(db, busy) == "in_sequence"  # outbound states are never touched
    assert result["golden_not_ready_because"] == {
        "signal not fresh": 1, "tier_3 (outside ICP)": 1, "no outreach angle": 1}  # fmt: skip

    tiers = dict(db.execute(
        "select prospect_id, tier from core.score_snapshots where model_version = 'icp-v1'"
    ).fetchall())  # fmt: skip
    assert len(tiers) == 6  # everyone is scored, silver included
    assert tiers[fresh] == "tier_1" and tiers[director] == "tier_3"
    reasons = db.execute(
        "select to_state, reason from ops.state_transitions where prospect_id = %s"
        " order by transition_id", (stale,)).fetchall()  # fmt: skip
    assert reasons[-1] == ("golden", "ready gate: signal not fresh")

    # Re-running the same day is idempotent; once the signal ages out, ready falls back.
    again = icp.run(db, rules, today=today)
    assert again["lifecycle_moves"] == {}
    later = icp.run(db, rules, today=date(2026, 12, 15))
    assert _stage(db, fresh) == "golden" and later["lifecycle_moves"] == {"ready -> golden": 1}

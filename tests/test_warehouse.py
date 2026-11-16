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
    assert first == ["0001_layered_warehouse.sql", "0002_access_roles.sql"]
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

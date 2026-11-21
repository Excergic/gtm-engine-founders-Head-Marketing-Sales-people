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
import time
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
        "0004_enrichment_attempts.sql",
        "0005_pipeline_runs.sql",
        "0006_outbound_lists.sql",
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
        "outbound.list_members",
        "ops.pipeline_runs", "ops.state_transitions", "ops.ai_outputs", "ops.schema_migrations",
        "ops.raw_row_outcomes", "ops.enrichment_attempts",
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


def _import(db, settings):
    from gtm.warehouse_import import WarehouseImporter

    return WarehouseImporter(db, settings.data_directory, settings.rules()).run()


def test_import_records_one_outcome_per_row_and_follows_identity_rules(db, settings):
    from test_grok_contract import grok_row, write

    from gtm.warehouse_import import resolve, review_queue

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


def test_old_employer_email_goes_to_its_own_list_not_ready(db):
    from datetime import date

    from gtm import icp, outbound

    today = date(2026, 11, 10)
    rules = _icp_rules()
    moved = _golden_person(db, "moved", title="Chief Revenue Officer", company="FinBursa",
                           signal="Appointed CRO", signal_date="2026-11-05")  # fmt: skip
    update(db, moved, email="moved@blackrock.com", email_status="verified")
    signal_for(db, moved)

    result = icp.run(db, rules, today=today)
    assert _stage(db, moved) == "golden"  # held out of ready
    assert result["golden_not_ready_because"] == {icp.WRONG_DOMAIN: 1}
    assert result["lists"] == {"old-employer-email": {"active": 1, "removed_now": 0}}
    version = rules["icp"]["model_version"]
    assert outbound.pick(db, version)["picked"] == []
    picked = outbound.pick(db, version, list_name="old-employer-email")["picked"]
    assert [p["email"] for p in picked] == ["moved@blackrock.com"]

    # Their current address turns up: ready again, and off the list (row kept).
    update(db, moved, email="moved@finbursa.com", email_status="verified")
    result = icp.run(db, rules, today=today)
    assert _stage(db, moved) == "ready"
    assert result["lists"] == {"old-employer-email": {"active": 0, "removed_now": 1}}
    assert db.execute("select count(*) from outbound.list_members").fetchone() == (1,)


def test_icp_segments_follow_the_icp():
    from gtm.icp import segment

    rules = _icp_rules()
    icp = rules["icp"]
    assert segment("Co-founder & CTO", icp) == "founder"
    assert segment("Founder & Managing Director", icp) == "founder"
    assert segment("SVP Global Sales", icp) == "gtm_leader"
    assert segment("Chief Revenue Officer", icp) == "gtm_leader"
    assert segment("Head of B2B Marketing", icp) == "gtm_leader"
    for outside in ("Enterprise Sales Director", "Director of GTM & Strategy",
                    "Marketing Manager", "Account Executive", "Vice President",
                    "Hiring Manager", "Head of Business Development", None):  # fmt: skip
        assert segment(outside, icp) is None, outside


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


def test_deepline_cli_reads_real_errors_and_retries_only_temporary_ones(monkeypatch):
    import subprocess

    from gtm.enrichers import ProviderError
    from gtm.enrichers import deepline as dl

    monkeypatch.setattr(dl.time, "sleep", lambda s: None)
    calls = []

    def fake(results):
        def run(cmd, **kw):
            calls.append(cmd)
            item = results.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        return run

    def done(code, out="", err=""):
        return subprocess.CompletedProcess([], code, stdout=out, stderr=err)

    # Temporary failure (error JSON on stdout, logs on stderr), then success.
    monkeypatch.setattr(dl.subprocess, "run", fake([
        done(1, '{"ok": false, "error": {"message": "upstream provider 502"}}', "[log] started"),
        done(0, '{"result": {"email": "a@b.com"}}')]))  # fmt: skip
    assert dl._cli(["plays", "run", "x"], 5) == {"result": {"email": "a@b.com"}}
    assert len(calls) == 2

    # No credits: permanent, not retried, and the message is the real one.
    calls.clear()
    monkeypatch.setattr(dl.subprocess, "run", fake([
        done(1, '{"ok": false, "error": {"message": "Insufficient credits"}}')]))  # fmt: skip
    with pytest.raises(ProviderError, match="Insufficient credits") as err:
        dl._cli(["plays", "run", "x"], 5)
    assert err.value.retryable is False and len(calls) == 1

    # Timeouts: retried, then reported as retryable so the retry queue picks it up.
    monkeypatch.setattr(dl.subprocess, "run", fake([
        subprocess.TimeoutExpired("deepline", 5), subprocess.TimeoutExpired("deepline", 5)]))  # fmt: skip
    with pytest.raises(ProviderError, match="timed out after 5s .after 2 attempts") as err:
        dl._cli(["plays", "run", "x"], 5)
    assert err.value.retryable is True


def test_failures_are_isolated_recorded_and_queued_for_retry(db):
    from gtm.enrichers import ProviderError, record_failure
    from gtm.enrichment import Finding, retry_queue, run_waterfall

    ok = register(db, name="Ok Person", linkedin_url=linkedin("ok"))
    flaky = register(db, name="Flaky Person", linkedin_url=linkedin("flaky"))
    broke = register(db, name="Broke Person", linkedin_url=linkedin("broke"))
    nothing = register(db, name="Nothing Person", linkedin_url=linkedin("none"))
    asked = []

    class Crashes:  # a whole provider blows up
        name, stages = "crashes", frozenset({"email"})

        def find(self, stage, people):
            asked.append("crashes")
            raise RuntimeError("adapter bug")

    class Mixed:  # per person: found, temporary error, permanent error, miss
        name, stages = "mixed", frozenset({"email"})

        def find(self, stage, people):
            asked.append(sorted(p["name"] for p in people))
            out = []
            for p in people:
                if p["name"] == "Ok Person":
                    out.append(Finding(prospect_id=p["prospect_id"], provider="mixed",
                                       email="ok@a.com", email_status="verified"))  # fmt: skip
                elif p["name"] == "Flaky Person":
                    record_failure(p, "mixed", stage, ProviderError("502", retryable=True))
                elif p["name"] == "Broke Person":
                    record_failure(p, "mixed", stage, ProviderError("no credits", retryable=False))
            return out

    ids = [str(ok), str(flaky), str(broke), str(nothing)]
    result = run_waterfall(db, ids, {"email": ["crashes", "mixed"]},
                           {"crashes": Crashes(), "mixed": Mixed()})  # fmt: skip
    # The crash did not stop the next provider, which was still asked about everyone.
    assert asked == ["crashes", ["Broke Person", "Flaky Person", "Nothing Person", "Ok Person"]]
    assert layer_of(db, ok) == "golden"
    steps = {s["provider"]: s for s in result["steps"]}
    assert steps["crashes"]["failed"] == 4 and steps["mixed"]["failed"] == 2

    outcomes = dict(db.execute(
        "select prospect_id, outcome || coalesce(':' || retryable::text, '')"
        " from ops.enrichment_latest where provider = 'mixed'").fetchall())  # fmt: skip
    assert outcomes == {ok: "found", flaky: "error:true", broke: "error:false", nothing: "miss"}
    # Retry queue: latest attempt was a retryable error, person not golden. Broke's latest
    # error on "mixed" is permanent, but the crash on "crashes" left a retryable one.
    assert set(retry_queue(db)) == {str(flaky), str(broke), str(nothing)}
    rejected(db, "update ops.enrichment_attempts set outcome = 'found'",
             match="append-only")  # fmt: skip


def test_run_batches_skips_a_failed_batch_and_stops_when_something_is_down(db, tmp_path):
    from gtm.enrichment import run_batches

    dsn = db.info.dsn
    ids = [str(register(db, name=f"P{i} Person", linkedin_url=linkedin(f"p{i}"))) for i in range(6)]
    calls = {"n": 0}

    def connect():
        calls["n"] += 1
        if calls["n"] == 1:  # the first batch cannot reach the database
            raise psycopg.OperationalError("connection refused")
        return psycopg.connect(dsn)

    class Quiet:
        name, stages = "quiet", frozenset({"email"})

        def find(self, stage, people):
            return []

    lines = []
    result = run_batches(connect, ids, {"email": ["quiet"]}, {"quiet": Quiet()}, limit=2,
                         runs_dir=tmp_path, log=lines.append)  # fmt: skip
    assert result["batches"] == 3 and len(result["failed_batches"]) == 1
    assert result["stopped"] is None
    assert "FAILED" in lines[0] and lines[-1].startswith("batch 3: 6/6")

    def down():
        raise psycopg.OperationalError("down")

    result = run_batches(down, ids, {"email": ["quiet"]}, {"quiet": Quiet()}, limit=1,
                         log=lambda s: None, max_consecutive_failures=2)  # fmt: skip
    assert result["stopped"] == "2 batches failed in a row" and result["batches"] == 2

    class NoCredits:
        name, stages = "broke", frozenset({"email"})

        def find(self, stage, people):
            from gtm.enrichers import ProviderError, record_failure

            for p in people:
                record_failure(p, "broke", stage,
                               ProviderError("Insufficient credits", retryable=False))  # fmt: skip
            return []

    result = run_batches(lambda: psycopg.connect(dsn), ids, {"email": ["broke"]},
                         {"broke": NoCredits()}, limit=2, log=lambda s: None)  # fmt: skip
    assert result["stopped"] == "provider reports a credit problem" and result["batches"] == 1


def test_mcp_tracking_queries_read_the_ready_list_and_history(db):
    from datetime import date

    from psycopg.rows import dict_row

    from gtm import icp, mcp_server

    ready = _golden_person(db, "rdy", title="Founder & CEO", signal="Raised a seed round",
                           signal_date=date.today().isoformat())  # fmt: skip
    _golden_person(db, "old", title="CMO", signal_date="2025-01-01")
    icp.run(db, _icp_rules())

    with psycopg.connect(db.info.dsn, row_factory=dict_row,
                         options="-c default_transaction_read_only=on") as ro:  # fmt: skip
        rows = mcp_server.ready_to_contact(ro)
        assert [r["name"] for r in rows] == ["Rdy Person"]
        assert rows[0]["tier"] == "tier_1" and isinstance(rows[0]["score"], int)
        assert [r["name"] for r in mcp_server.golden_prospects(ro, status="stale")] == [
            "Old Person"]  # fmt: skip
        detail = mcp_server.prospect(ro, str(ready))
        assert detail["prospect"]["lifecycle_stage"] == "ready"
        assert detail["history"][-1]["to_state"] == "ready"
        assert mcp_server.prospect(ro, "Person")["matches"]  # ambiguous name: list matches
        overview = mcp_server.pipeline_overview(ro)
        assert {"layer": "golden", "people": 2} in overview["layers"]
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            ro.execute("update golden.prospects set notes = 'x'")


def test_import_skips_files_still_being_written_and_survives_a_bad_file(db, settings):
    from test_grok_contract import grok_row, write

    write(settings.grok_directory / "VPs" / "good.csv", [grok_row()])
    write(settings.grok_directory / "VPs" / "fresh.csv", [grok_row(name="Fresh Person",
          linkedin_url="https://www.linkedin.com/in/fresh")], settled=False)  # fmt: skip
    bad = settings.grok_directory / "founders" / "bad.csv"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text('name,name\n"unterminated,x\n')  # duplicate headers + broken quoting
    os.utime(bad, (time.time() - 600, time.time() - 600))

    result = _import(db, settings)
    assert result["outcomes"] == {"registered": 1}  # the good file still went in
    assert result["raw"]["raw_files_skipped_still_writing"] == 1
    assert result["raw"]["raw_files_failed"] == 1
    assert any("bad.csv" in p for p in result["problems"])


class _FakeProvider:
    def __init__(self, name, emails=None, crash=False):
        self.name, self.emails, self.crash = name, emails or {}, crash
        self.stages = frozenset({"linkedin_url", "email", "verify"})
        self.asked = []

    def find(self, stage, people):
        from gtm.enrichment import Finding

        self.asked.append((stage, sorted(p["name"] for p in people)))
        if self.crash:
            raise RuntimeError("provider down")
        return [Finding(prospect_id=p["prospect_id"], provider=self.name,
                        email=self.emails[p["name"]], email_status="verified")
                for p in people if stage == "email" and p["name"] in self.emails]  # fmt: skip


def test_daily_pipeline_runs_every_step_and_records_the_run(db, settings):
    from gtm.pipeline import run_daily

    dsn = db.info.dsn
    connect = lambda: psycopg.connect(dsn)  # noqa: E731
    today = date_today()
    founder = register(db, name="Fran Founder", title="Co-founder & CEO",
                       linkedin_url=linkedin("fran"), signal_date=today,
                       outreach_angle="Congrats on the seed; how is outbound going?")  # fmt: skip
    register(db, name="Dee Director", title="Sales Director", linkedin_url=linkedin("dee"),
             signal_date=today)  # fmt: skip
    clay = _FakeProvider("clay", crash=True)  # a broken provider must not stop anything
    deepline = _FakeProvider("deepline", emails={"Fran Founder": "fran@examplesaas.com"})
    lines = []

    result = run_daily(settings, connect, credits=lambda: 100.0,
                       enrichers={"clay": clay, "deepline": deepline, "manual": deepline},
                       log=lines.append)  # fmt: skip
    steps = result["steps"]
    assert steps["import"]["status"] == "ok"
    # Only the ICP person was sent to paid enrichment; the Director was not.
    assert ("email", ["Fran Founder"]) in deepline.asked
    assert all("Dee Director" not in names for _, names in deepline.asked)
    assert steps["enrich"]["failed"]["email.clay"] == 1  # recorded, not fatal
    assert layer_of(db, founder) == "golden"
    assert steps["score"]["status"] == "ok" and steps["score"]["ready"] == 1
    # The clay crash left a retryable error, so retry_failed picked the person up... but
    # they are golden now, so the queue is empty.
    assert steps["retry_failed"]["people"] == 0
    row = db.execute("select status, details->'steps'->'score'->>'status' from ops.pipeline_runs"
                     " where job = 'pipeline'").fetchone()  # fmt: skip
    db.commit()
    assert row == (result["status"], "ok")

    # Low credits: paid steps are skipped, the free ones still run.
    low = run_daily(settings, connect, credits=lambda: 1.0, enrichers={"clay": clay,
                    "deepline": deepline, "manual": deepline}, log=lines.append)  # fmt: skip
    assert low["steps"]["enrich"]["status"] == "skipped"
    assert "below" in low["steps"]["enrich"]["reason"]
    assert low["steps"]["score"]["status"] == "ok"


def test_daily_pipeline_never_overlaps_and_closes_crashed_runs(db, settings):
    from gtm.pipeline import run_daily

    dsn = db.info.dsn
    db.execute("insert into ops.pipeline_runs (job, status, started_at)"
               " values ('pipeline', 'running', now() - interval '7 hours')")  # fmt: skip
    db.execute("insert into ops.pipeline_runs (job, status) values ('pipeline', 'running')")
    db.commit()
    result = run_daily(settings, lambda: psycopg.connect(dsn), credits=lambda: 100.0,
                       enrichers={}, log=lambda s: None)  # fmt: skip
    assert result["status"] == "skipped"
    assert db.execute("select status, error_summary from ops.pipeline_runs"
                      " order by started_at").fetchall() == [
        ("failed", "did not finish (crashed or killed)"), ("running", None)]  # fmt: skip


def test_schedule_installs_a_daily_launchd_job(tmp_path):
    import plistlib

    from gtm.schedule import LABEL, Schedule, build_plist, parse_time

    assert parse_time("07:30") == (7, 30)
    with pytest.raises(ValueError, match="HH:MM"):
        parse_time("7.30pm")
    spec = build_plist(tmp_path, "06:05", uv="/opt/homebrew/bin/uv")
    assert spec["StartCalendarInterval"] == {"Hour": 6, "Minute": 5}
    assert spec["ProgramArguments"][-2:] == ["gtm", "pipeline"]
    # launchd must not open files on the (external) project volume before the job starts.
    assert spec["WorkingDirectory"] == str(Path.home())
    assert spec["StandardOutPath"].startswith(str(Path.home() / "Library/Logs"))

    calls = []

    def runner(cmd, **kw):
        calls.append(cmd[1])
        import subprocess

        return subprocess.CompletedProcess(cmd, 0, stdout="last exit code = 0\n", stderr="")

    schedule = Schedule(tmp_path, home=tmp_path / "home", runner=runner)
    assert schedule.install("06:05", dry_run=True)["dry_run"] is True
    assert calls == [] and not schedule.plist_path.exists()
    schedule.install("06:05")
    assert calls == ["bootout", "bootstrap"]
    assert plistlib.loads(schedule.plist_path.read_bytes())["Label"] == LABEL
    assert schedule.status() == {"installed": True, "loaded": True, "runs_daily_at": "06:05",
                                 "last_exit_code": "0",
                                 "log": str(Path.home() / "Library/Logs/exergiclabs-gtm/pipeline.log")}  # fmt: skip
    assert schedule.uninstall() == {"uninstalled": True} and not schedule.plist_path.exists()


def date_today():
    from datetime import date

    return date.today().isoformat()

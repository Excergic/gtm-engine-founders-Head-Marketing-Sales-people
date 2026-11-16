"""Postgres warehouse: versioned migrations and connections.

Migrations are numbered SQL files in ``gtm/migrations``. Each runs in its own
transaction and is recorded in ``ops.schema_migrations`` with a checksum, so an applied
file that is later edited is refused instead of silently drifting.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class WarehouseError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


def database_url(explicit: str | None = None) -> str:
    url = explicit or os.environ.get("DATABASE_URL")
    if not url:
        raise WarehouseError("DATABASE_URL is not set (add it to .env)")
    return url


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(database_url(url))


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    found = []
    for path in sorted(directory.glob("*.sql")):
        match = _NAME.match(path.name)
        if not match:
            raise WarehouseError(f"bad migration file name: {path.name}")
        found.append(Migration(match.group(1), path.name, path.read_text()))
    versions = [m.version for m in found]
    if len(set(versions)) != len(versions):
        raise WarehouseError("duplicate migration version numbers")
    return found


_BOOTSTRAP = """
create schema if not exists ops;
create table if not exists ops.schema_migrations (
    version    text primary key,
    name       text not null,
    checksum   text not null,
    applied_at timestamptz not null default now()
);
"""


def applied(conn: psycopg.Connection) -> dict[str, tuple[str, str]]:
    conn.execute(_BOOTSTRAP)
    conn.commit()
    rows = conn.execute("select version, name, checksum from ops.schema_migrations").fetchall()
    return {version: (name, checksum) for version, name, checksum in rows}


def status(conn: psycopg.Connection, directory: Path = MIGRATIONS_DIR) -> list[dict]:
    done = applied(conn)
    result = []
    for m in load_migrations(directory):
        state = "pending"
        if m.version in done:
            state = "applied" if done[m.version][1] == m.checksum else "changed"
        result.append({"version": m.version, "name": m.name, "state": state})
    return result


def migrate(conn: psycopg.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations in order. Returns the names applied."""
    done = applied(conn)
    ran = []
    for m in load_migrations(directory):
        if m.version in done:
            if done[m.version][1] != m.checksum:
                raise WarehouseError(
                    f"{m.name} was changed after it was applied; add a new migration instead"
                )
            continue
        with conn.transaction():
            # Lock so two jobs cannot apply the same migration at once.
            conn.execute("lock table ops.schema_migrations in exclusive mode")
            if conn.execute(
                "select 1 from ops.schema_migrations where version = %s", (m.version,)
            ).fetchone():
                continue
            conn.execute(m.sql)
            conn.execute(
                "insert into ops.schema_migrations (version, name, checksum) values (%s, %s, %s)",
                (m.version, m.name, m.checksum),
            )
        ran.append(m.name)
    return ran

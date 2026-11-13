"""Real PostgreSQL test database (task doc §7 T12 / R8.5).

The suite runs against the live PostgreSQL service on a **dedicated test
database** — never the live ``subnet_acl`` database. One database is
provisioned once per pytest session (database, ``citext``, the ``registry``
schema owned by ``registry_app``, the application migrations), and every test
starts from truncated tables: per-test truncation, not a temp schema.

Connection details come from the environment, with defaults that match this
workspace's ``subnet-acl-postgres`` service. The database is reached on its
container address because the service publishes no host port (T1 keeps the
database off the host).

Safety: the test database name must contain ``test``. The fixture refuses to
provision or truncate anything else, so a mis-typed ``SUBNET_ACL_TEST_DB``
cannot wipe the live store.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

PG_HOST = os.environ.get("SUBNET_ACL_TEST_PG_HOST", "172.17.0.8")
PG_PORT = int(os.environ.get("SUBNET_ACL_TEST_PG_PORT", "5432"))
PG_ADMIN_PASSWORD = os.environ.get(
    "SUBNET_ACL_TEST_PG_ADMIN_PASSWORD", "subnet_acl_pg_admin"
)
TEST_DB = os.environ.get("SUBNET_ACL_TEST_DB", "subnet_acl_test_api")

REGISTRY_PW = os.environ.get("REGISTRY_DB_PASSWORD", "registry_app_pw")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"

REGISTRY_TABLES = [
    "registry.tasks",
    "registry.queue_config",
    "registry.queue_state",
    "registry.service_basic",
    "registry.service_endpoints",
    "registry.service_instances",
    "registry.services",
    "registry.api_users",
    "registry.import_state",
]


def _assert_test_db(name: str) -> None:
    if "test" not in name.lower():
        raise RuntimeError(
            f"refusing to run the test fixture against database {name!r}: "
            "the test database name must contain 'test'"
        )


def admin_url(dbname: str = "postgres") -> str:
    return (
        f"postgresql://postgres:{PG_ADMIN_PASSWORD}@{PG_HOST}:{PG_PORT}/{dbname}"
    )


def registry_url(dbname: str = TEST_DB) -> str:
    return f"postgresql://registry_app:{REGISTRY_PW}@{PG_HOST}:{PG_PORT}/{dbname}"


def ensure_database() -> None:
    """Create the test database and its schema once; never touch anything else."""
    _assert_test_db(TEST_DB)
    with psycopg.connect(admin_url(), autocommit=True, connect_timeout=10) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    with psycopg.connect(admin_url(TEST_DB), autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS citext")
        conn.execute(
            "CREATE SCHEMA IF NOT EXISTS registry AUTHORIZATION registry_app"
        )
        conn.execute("GRANT CONNECT ON DATABASE " f'"{TEST_DB}" TO registry_app, '
                     "gateway_app, gateway_reader")


def apply_registry_migrations() -> list[str]:
    """Apply the -api's own migrations as ``registry_app`` (the schema owner)."""
    applied: list[str] = []
    with psycopg.connect(
        registry_url(), row_factory=dict_row, autocommit=True
    ) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS registry.schema_migrations ("
            " version text PRIMARY KEY,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {
            row["version"]
            for row in conn.execute(
                "SELECT version FROM registry.schema_migrations"
            ).fetchall()
        }
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO registry.schema_migrations (version) VALUES (%s)",
                (path.name,),
            )
            applied.append(path.name)
    return applied


def truncate() -> None:
    """Empty every registry table and restart identity; per-test isolation.

    The queue's singleton rows are re-seeded so every test starts with the
    documented defaults and a fresh planner heartbeat (a missing heartbeat is
    the real ``queue_unavailable`` condition and must not leak into a test).
    """
    _assert_test_db(TEST_DB)
    with psycopg.connect(registry_url(), autocommit=True) as conn:
        conn.execute(
            "TRUNCATE " + ", ".join(REGISTRY_TABLES) + " RESTART IDENTITY CASCADE"
        )
        conn.execute(
            "INSERT INTO registry.queue_config (id) VALUES (1) ON CONFLICT (id) DO NOTHING"
        )
        conn.execute(
            "INSERT INTO registry.queue_state (id, last_dispatch_at) VALUES (1, now()) "
            "ON CONFLICT (id) DO NOTHING"
        )


def provision() -> None:
    ensure_database()
    apply_registry_migrations()

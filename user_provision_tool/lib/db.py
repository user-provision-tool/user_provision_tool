"""PostgreSQL access for the registry schema (T1).

The -api is the only writer of the ``registry`` schema and the only role that
migrates it. Every statement in this package is schema-qualified. The gateway
reads committed state through its own connection; it never calls in, and this
module never reads or references a gateway table (R1.3).

Connection behaviour:

* the URL comes from ``DATABASE_URL``;
* startup retries while the database is not ready (the compose order is
  retry-based, not ``depends_on``);
* migrations are serialized by a PostgreSQL advisory lock, so one process (and
  a second -api, if one is ever started) applies each version exactly once;
* a bounded connection pool is shared by the request handlers and the worker
  threads.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_log = logging.getLogger("provision-api.db")

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://registry_app:registry_app@subnet-acl-postgres:5432/subnet_acl",
)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"
MIGRATION_LOCK_KEY = 7_412_001  # stable, registry-owned advisory-lock key

_pool: ConnectionPool | None = None


class DatabaseUnavailable(RuntimeError):
    """The registry database cannot be reached or migrated."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


def _make_pool() -> ConnectionPool:
    # check=ConnectionPool.check_connection validates a recycled connection; a
    # dead connection from a restarted database is discarded, not handed out.
    return ConnectionPool(
        conninfo=DATABASE_URL,
        min_size=1,
        max_size=int(os.environ.get("REGISTRY_DB_POOL_SIZE", "8")),
        timeout=float(os.environ.get("REGISTRY_DB_POOL_TIMEOUT", "5")),
        kwargs={"row_factory": dict_row, "autocommit": False},
        check=ConnectionPool.check_connection,
        open=False,
    )


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = _make_pool()
        _pool.open(wait=True, timeout=float(os.environ.get("REGISTRY_DB_OPEN_TIMEOUT", "30")))
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """A pooled connection with a transaction that commits on clean exit."""
    pool = get_pool()
    with pool.connection() as conn:
        with conn.transaction():
            yield conn


@contextmanager
def read_connection() -> Iterator[psycopg.Connection]:
    """A pooled read-only connection (no explicit transaction block)."""
    pool = get_pool()
    with pool.connection() as conn:
        yield conn


def fetch_all(sql: str, params: tuple | dict | None = None) -> list[dict[str, Any]]:
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())


def fetch_one(sql: str, params: tuple | dict | None = None) -> dict[str, Any] | None:
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()


def execute(sql: str, params: tuple | dict | None = None) -> int:
    with get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.rowcount


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------


def _migration_files() -> list[tuple[str, Path]]:
    if not MIGRATIONS_DIR.is_dir():
        return []
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    return [(p.name, p) for p in files]


def _apply_migrations_locked(conn: psycopg.Connection) -> list[str]:
    applied: list[str] = []
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS registry.schema_migrations ("
            " version text PRIMARY KEY,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
        cur.execute("SELECT version FROM registry.schema_migrations")
        done = {r["version"] for r in cur.fetchall()}
        for version, path in _migration_files():
            if version in done:
                continue
            sql = path.read_text(encoding="utf-8")
            cur.execute(sql)
            cur.execute(
                "INSERT INTO registry.schema_migrations (version) VALUES (%s)",
                (version,),
            )
            applied.append(version)
            _log.info("applied registry migration %s", version)
    return applied


def migrate(timeout_seconds: float = 60.0) -> list[str]:
    """Apply every pending registry migration under the advisory lock.

    Retried until *timeout_seconds* because a fresh stack may start this
    process before the database healthcheck passes.
    """
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while True:
        try:
            with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
                with conn.transaction():
                    conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
                    return _apply_migrations_locked(conn)
        except Exception as exc:  # noqa: BLE001 — readiness retry, then fail closed
            last_error = exc
            if time.monotonic() >= deadline:
                raise DatabaseUnavailable(
                    f"registry database not ready after {timeout_seconds:.0f}s: {exc}"
                ) from exc
            time.sleep(2.0)


def health() -> dict[str, Any]:
    """Probe the database for /health (T1: health reports DB state)."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database(), current_schema()")
                row = cur.fetchone()
        return {"status": "ok", "database": row[0]}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": str(exc)}

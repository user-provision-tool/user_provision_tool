"""conftest.py — shared fixtures for all tests."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml

# Make lib/ importable from the project root
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import pg_test_db  # noqa: E402 — tests/ helper, set import path first

# The registry lives in PostgreSQL (T1). Point the application at the dedicated
# TEST database BEFORE any lib module is imported: lib.db reads DATABASE_URL at
# import time. The fixture refuses any database whose name lacks "test", so a
# mis-typed override cannot reach the live store.
os.environ["DATABASE_URL"] = pg_test_db.registry_url()

FIXTURES_DIR = Path(__file__).parent / "fixtures"
COMPOSE_TEMPLATE = FIXTURES_DIR / "docker-compose.template.yml.j2"
NGINX_TEMPLATE = FIXTURES_DIR / "myapp.template.nginx.conf.j2"


@pytest.fixture(scope="session", autouse=True)
def _pg_test_database():
    """Provision the real PostgreSQL test database once per session (T12)."""
    pg_test_db.provision()
    yield


@pytest.fixture(autouse=True)
def _pg_isolation(_pg_test_database):
    """Truncate every registry table before each test (per-test truncation).

    The module-level connection pool is closed first: the app closes it on
    lifespan shutdown, and a test holding the closed pool would otherwise fail
    with ``PoolClosed``. Reopening per test also guarantees no state crosses a
    test boundary through a pooled connection.
    """
    from lib import db as _db

    _db.close_pool()
    pg_test_db.truncate()
    try:
        yield
    finally:
        _db.close_pool()


@pytest.fixture(autouse=True)
def _provision_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A per-test PROVISION_DIR with a fresh service-Basic keyring (T2).

    The keyring is created exactly as on a fresh install (``load_or_create``);
    tests that exercise the keyring bootstrap override these paths themselves.
    """
    provision = tmp_path / "provision"
    generated = provision / "generated"
    generated.mkdir(parents=True)
    monkeypatch.setenv("PROVISION_DIR", str(provision))
    monkeypatch.setenv("GENERATED_DIR", str(generated))
    from lib import keys

    keys.load_or_create()
    return provision


@pytest.fixture()
def tmp_generated(tmp_path: Path) -> Path:
    """Return an isolated temp directory that acts as the 'generated/' folder."""
    return tmp_path


@pytest.fixture()
def registry_file(tmp_path: Path) -> Path:
    """Compatibility shim for the pre-PostgreSQL YAML registry.

    The registry is the ``registry`` schema now; isolation comes from the
    autouse per-test truncation. The returned path is unused by production code
    and exists only so older tests that requested this fixture still run.
    """
    return tmp_path / "user_registry.yml"


@pytest.fixture()
def mock_input_yes(monkeypatch: pytest.MonkeyPatch):
    """Patch builtins.input to always return 'y' (for volume confirmation prompts)."""
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")


@pytest.fixture()
def sample_entry(tmp_path: Path) -> dict:
    """A fully-populated registry entry for testing."""
    compose_out = str(tmp_path / "docker-compose.user-alice.0.yml")
    nginx_out = str(tmp_path / "myapp.user-alice.0.nginx.conf")
    htpasswd_out = str(tmp_path / "myapp.user-alice.0.htpasswd")
    return {
        "user_name": "alice",
        "passwd": "",
        "service_name": "myapp",
        "label": "0",
        "network_name": "myapp-user_alice-0",
        "compose_template_path": str(COMPOSE_TEMPLATE),
        "nginx_conf_template_path": str(NGINX_TEMPLATE),
        "compose_file_path": compose_out,
        "nginx_conf_path": nginx_out,
        "htpasswd_path": htpasswd_out,
        "volumes": {
            "app_data": "/srv/alice/app",
            "db_data": "/srv/alice/db",
        },
    }

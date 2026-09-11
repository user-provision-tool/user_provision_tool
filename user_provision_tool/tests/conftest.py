"""conftest.py — shared fixtures for all tests."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
import yaml

# Make lib/ importable from the project root
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES_DIR = Path(__file__).parent / "fixtures"
COMPOSE_TEMPLATE = FIXTURES_DIR / "docker-compose.template.yml.j2"
NGINX_TEMPLATE = FIXTURES_DIR / "myapp.template.nginx.conf.j2"


@pytest.fixture()
def tmp_generated(tmp_path: Path) -> Path:
    """Return an isolated temp directory that acts as the 'generated/' folder."""
    return tmp_path


@pytest.fixture()
def registry_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Patch registry.REGISTRY_FILE to point at a temp file for each test.

    ALSO resets the module cache: ``_load()`` returns ``_state["data"]`` unless
    the mtime is None, so patching only the path let a previous test's (or the
    live registry's) cached list leak into this test — the cause of the
    order-dependent "environment" failures.
    """
    from lib import registry as reg_mod
    reg_path = tmp_path / "user_registry.yml"
    monkeypatch.setattr(reg_mod, "REGISTRY_FILE", reg_path)
    reg_mod._state["data"] = []
    reg_mod._state["mtime"] = None
    return reg_path


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

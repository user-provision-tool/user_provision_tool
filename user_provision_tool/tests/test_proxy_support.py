"""Unit tests for proxy / --build-arg support across all layers.

Tests cover:
  - docker_ops.compose_build  →  --build-arg flags in the command
  - provisioner.register_user  →  build_args stored in registry + compose_build called
  - provisioner.rebuild_user   →  build_args passed through + fallback to registry
  - API RegisterRequest        →  build_args accepted
  - API RebuildRequest         →  build_args accepted
  - CLI register.py            →  --build-arg flag parsing
  - CLI rebuild.py             →  --build-arg flag parsing
  - MockProxy                   →  starts, relays HTTP, handles CONNECT
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

# Make lib/ importable
import sys
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from lib import docker_ops, provisioner, registry

FIXTURES_DIR = Path(__file__).parent / "fixtures"
COMPOSE_TEMPLATE = str(FIXTURES_DIR / "docker-compose.template.yml.j2")
NGINX_TEMPLATE = str(FIXTURES_DIR / "myapp.template.nginx.conf.j2")


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _mock_docker_ops_run(monkeypatch):
    """Patch docker_ops._run so no real subprocess is spawned.

    Returns a list that collects every (args, kwargs) call for inspection.
    """
    calls: list[list[str]] = []

    def fake_run(args, check=True):
        calls.append(list(args))
        import subprocess as sp
        return sp.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(docker_ops, "_run", fake_run)
    return calls


def _mock_network_nginx_reload(monkeypatch):
    """Silence network_connect / nginx_reload calls that require real docker."""
    monkeypatch.setattr(docker_ops, "network_connect", lambda *a, **kw: None)
    monkeypatch.setattr(docker_ops, "network_disconnect", lambda *a, **kw: None)
    monkeypatch.setattr(docker_ops, "nginx_reload", lambda *a, **kw: None)


# ─────────────────────────────────────────────────────────────────────
# docker_ops.compose_build
# ─────────────────────────────────────────────────────────────────────

class TestComposeBuildWithBuildArgs:
    """Verify --build-arg flags are correctly appended to the docker compose build command."""

    def test_no_build_args_produces_plain_command(self, monkeypatch):
        calls = _mock_docker_ops_run(monkeypatch)
        docker_ops.compose_build("/tmp/dc.yml")
        assert len(calls) == 1
        assert "--build-arg" not in calls[0]

    def test_single_build_arg(self, monkeypatch):
        calls = _mock_docker_ops_run(monkeypatch)
        docker_ops.compose_build(
            "/tmp/dc.yml",
            build_args={"HTTP_PROXY": "http://proxy:8080"},
        )
        cmd = calls[0]
        assert "--build-arg" in cmd
        assert "HTTP_PROXY=http://proxy:8080" in cmd

    def test_multiple_build_args(self, monkeypatch):
        calls = _mock_docker_ops_run(monkeypatch)
        docker_ops.compose_build(
            "/tmp/dc.yml",
            build_args={
                "HTTP_PROXY": "http://proxy:8080",
                "HTTPS_PROXY": "http://proxy:8443",
                "NO_PROXY": "localhost,127.0.0.1",
            },
        )
        cmd = calls[0]
        # Each build-arg flag is a separate element
        build_arg_indices = [i for i, v in enumerate(cmd) if v == "--build-arg"]
        assert len(build_arg_indices) == 3
        values = [cmd[i + 1] for i in build_arg_indices]
        assert "HTTP_PROXY=http://proxy:8080" in values
        assert "HTTPS_PROXY=http://proxy:8443" in values
        assert "NO_PROXY=localhost,127.0.0.1" in values

    def test_build_args_with_no_cache(self, monkeypatch):
        calls = _mock_docker_ops_run(monkeypatch)
        docker_ops.compose_build(
            "/tmp/dc.yml",
            no_cache=True,
            build_args={"HTTP_PROXY": "http://p:3128"},
        )
        cmd = calls[0]
        assert "--no-cache" in cmd
        assert "--build-arg" in cmd

    def test_build_args_with_env_file_and_project(self, monkeypatch):
        calls = _mock_docker_ops_run(monkeypatch)
        docker_ops.compose_build(
            "/tmp/dc.yml",
            env_file="/tmp/.env",
            project_name="myapp-user_alice-0",
            build_args={"HTTP_PROXY": "http://p:3128"},
        )
        cmd = calls[0]
        assert "--env-file" in cmd
        assert "/tmp/.env" in cmd
        assert "--project-name" in cmd
        assert "myapp-user_alice-0" in cmd
        assert "--build-arg" in cmd
        assert "HTTP_PROXY=http://p:3128" in cmd

    def test_empty_build_args_dict_produces_no_flags(self, monkeypatch):
        calls = _mock_docker_ops_run(monkeypatch)
        docker_ops.compose_build("/tmp/dc.yml", build_args={})
        cmd = calls[0]
        assert "--build-arg" not in cmd


# ─────────────────────────────────────────────────────────────────────
# provisioner.register_user  —  build_args stored in registry
# ─────────────────────────────────────────────────────────────────────

class TestRegisterUserWithBuildArgs:
    """Verify build_args flow through register_user → registry + compose_build."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, tmp_path):
        _mock_network_nginx_reload(monkeypatch)
        self.calls = _mock_docker_ops_run(monkeypatch)
        # Redirect registry to temp file
        from lib import registry as reg_mod
        self.reg_path = tmp_path / "user_registry.yml"
        monkeypatch.setattr(reg_mod, "REGISTRY_FILE", self.reg_path)
        self.tmp_path = tmp_path
        # user_data_dir so _auto_volumes creates the volume directories needed
        # by the compose template (app_data, db_data references)
        self.user_data_dir = tmp_path / "user_data"
        self.user_data_dir.mkdir()

    def test_register_with_build_args_stored_in_registry(self):
        provisioner.register_user(
            user_name="alice",
            service_name="myapp",
            label="0",
            compose_template=COMPOSE_TEMPLATE,
            output_dir=self.tmp_path,
            user_data_dir=self.user_data_dir,
            build_args={"HTTP_PROXY": "http://proxy:3128"},
        )
        entry = registry.get_user_service("alice", "myapp", "0")
        assert entry is not None
        assert entry["build_args"] == {"HTTP_PROXY": "http://proxy:3128"}

    def test_register_without_build_args_stores_empty_dict(self):
        provisioner.register_user(
            user_name="bob",
            service_name="myapp",
            label="0",
            compose_template=COMPOSE_TEMPLATE,
            output_dir=self.tmp_path,
            user_data_dir=self.user_data_dir,
        )
        entry = registry.get_user_service("bob", "myapp", "0")
        assert entry is not None
        assert entry["build_args"] == {}

    def test_register_with_build_args_calls_compose_build(self):
        """When build_args is provided, compose_build is called before compose_up."""
        provisioner.register_user(
            user_name="alice",
            service_name="myapp",
            label="0",
            compose_template=COMPOSE_TEMPLATE,
            output_dir=self.tmp_path,
            user_data_dir=self.user_data_dir,
            build_args={"HTTP_PROXY": "http://p:8080"},
        )
        # The docker compose subcommand is "build" or "up" — find it in each call
        subcommands = []
        for cmd in self.calls:
            for token in cmd:
                if token in ("build", "up", "down"):
                    subcommands.append(token)
        assert "build" in subcommands, f"Expected a compose_build call, got: {subcommands}"
        assert "up" in subcommands, f"Expected a compose_up call, got: {subcommands}"

    def test_register_without_build_args_skips_compose_build(self):
        """Without build_args, compose_build is NOT called — only compose_up."""
        provisioner.register_user(
            user_name="bob",
            service_name="myapp",
            label="0",
            compose_template=COMPOSE_TEMPLATE,
            output_dir=self.tmp_path,
            user_data_dir=self.user_data_dir,
        )
        subcommands = []
        for cmd in self.calls:
            for token in cmd:
                if token in ("build", "up", "down"):
                    subcommands.append(token)
        assert "build" not in subcommands, (
            f"compose_build should NOT be called when build_args is absent, "
            f"but got subcommands: {subcommands}"
        )
        assert "up" in subcommands, "compose_up should still be called"

    def test_register_with_multiple_build_args(self):
        provisioner.register_user(
            user_name="alice",
            service_name="myapp",
            label="0",
            compose_template=COMPOSE_TEMPLATE,
            output_dir=self.tmp_path,
            user_data_dir=self.user_data_dir,
            build_args={
                "HTTP_PROXY": "http://p:8080",
                "HTTPS_PROXY": "http://p:8443",
            },
        )
        entry = registry.get_user_service("alice", "myapp", "0")
        assert len(entry["build_args"]) == 2


# ─────────────────────────────────────────────────────────────────────
# provisioner.rebuild_user  —  build_args passed through + fallback
# ─────────────────────────────────────────────────────────────────────

class TestRebuildUserWithBuildArgs:
    """Verify build_args flow through rebuild_user."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, tmp_path):
        _mock_network_nginx_reload(monkeypatch)
        self.calls = _mock_docker_ops_run(monkeypatch)
        from lib import registry as reg_mod
        self.reg_path = tmp_path / "user_registry.yml"
        monkeypatch.setattr(reg_mod, "REGISTRY_FILE", self.reg_path)
        self.tmp_path = tmp_path
        self.user_data_dir = tmp_path / "user_data"
        self.user_data_dir.mkdir()

    def _register(self, user: str, build_args: dict | None = None):
        provisioner.register_user(
            user_name=user,
            service_name="myapp",
            label="0",
            compose_template=COMPOSE_TEMPLATE,
            output_dir=self.tmp_path,
            user_data_dir=self.user_data_dir,
            build_args=build_args,
        )
        self.calls.clear()  # reset for rebuild assertions

    def test_rebuild_explicit_build_args(self):
        self._register("alice")
        provisioner.rebuild_user(
            user_name="alice",
            service_name="myapp",
            label="0",
            no_cache=True,
            build_args={"HTTP_PROXY": "http://override:9999"},
        )
        build_cmds = [c for c in self.calls if "build" in c and "--build-arg" in c]
        assert len(build_cmds) == 1
        assert "HTTP_PROXY=http://override:9999" in build_cmds[0]

    def test_rebuild_falls_back_to_registry_build_args(self):
        """When build_args is not passed, registry-stored values are used."""
        self._register("alice", build_args={"HTTP_PROXY": "http://stored:3128"})
        provisioner.rebuild_user(
            user_name="alice",
            service_name="myapp",
            label="0",
            no_cache=True,
        )
        build_cmds = [c for c in self.calls if "build" in c and "--build-arg" in c]
        assert len(build_cmds) == 1
        assert "HTTP_PROXY=http://stored:3128" in build_cmds[0]

    def test_rebuild_explicit_overrides_registry(self):
        """Explicit build_args take precedence over registry-stored values."""
        self._register("alice", build_args={"HTTP_PROXY": "http://stored:3128"})
        provisioner.rebuild_user(
            user_name="alice",
            service_name="myapp",
            label="0",
            build_args={"HTTP_PROXY": "http://explicit:5555"},
        )
        build_cmds = [c for c in self.calls if "build" in c and "--build-arg" in c]
        assert len(build_cmds) == 1
        assert "HTTP_PROXY=http://explicit:5555" in build_cmds[0]
        assert "HTTP_PROXY=http://stored:3128" not in build_cmds[0]

    def test_rebuild_no_build_args_no_registry_stored(self):
        """Neither explicit nor registry build_args → no --build-arg in command."""
        self._register("alice")  # no build_args stored
        provisioner.rebuild_user(
            user_name="alice",
            service_name="myapp",
            label="0",
        )
        build_cmds = [c for c in self.calls if "build" in c]
        assert len(build_cmds) == 1
        assert "--build-arg" not in build_cmds[0]

    def test_rebuild_empty_registry_build_args_no_fallback(self):
        """Empty dict stored in registry should not produce --build-arg flags."""
        self._register("alice", build_args={})
        provisioner.rebuild_user(
            user_name="alice",
            service_name="myapp",
            label="0",
        )
        build_cmds = [c for c in self.calls if "build" in c]
        assert len(build_cmds) == 1
        assert "--build-arg" not in build_cmds[0]


# ─────────────────────────────────────────────────────────────────────
# API  —  RegisterRequest + RebuildRequest accept build_args
# ─────────────────────────────────────────────────────────────────────

class TestAPIProxySupport:
    """Verify the API models accept and validate build_args.

    Tests the Pydantic request models directly (no running server needed).
    """

    def test_register_request_accepts_build_args(self):
        from api import RegisterRequest
        req = RegisterRequest(
            user_name="prx1",
            service_name="graphiti",
            compose_template_path="/tmp/fake.yml.j2",
            build_args={"HTTP_PROXY": "http://proxy:3128", "HTTPS_PROXY": "http://proxy:3129"},
        )
        assert req.build_args == {"HTTP_PROXY": "http://proxy:3128", "HTTPS_PROXY": "http://proxy:3129"}

    def test_register_request_without_build_args_defaults_to_none(self):
        from api import RegisterRequest
        req = RegisterRequest(
            user_name="prx2",
            service_name="graphiti",
            compose_template_path="/tmp/fake.yml.j2",
        )
        assert req.build_args is None

    def test_register_request_empty_build_args(self):
        from api import RegisterRequest
        req = RegisterRequest(
            user_name="prx3",
            service_name="graphiti",
            compose_template_path="/tmp/fake.yml.j2",
            build_args={},
        )
        assert req.build_args == {}

    def test_rebuild_request_accepts_build_args(self):
        from api import RebuildRequest
        req = RebuildRequest(
            no_cache=True,
            build_args={"HTTP_PROXY": "http://proxy:8080", "NO_PROXY": "localhost"},
        )
        assert req.build_args == {"HTTP_PROXY": "http://proxy:8080", "NO_PROXY": "localhost"}

    def test_rebuild_request_without_build_args_defaults_to_none(self):
        from api import RebuildRequest
        req = RebuildRequest(no_cache=False)
        assert req.build_args is None

    def test_rebuild_request_defaults(self):
        from api import RebuildRequest
        req = RebuildRequest()
        assert req.no_cache is False
        assert req.build_args is None


# ─────────────────────────────────────────────────────────────────────
# CLI  —  --build-arg flag parsing
# ─────────────────────────────────────────────────────────────────────

class TestCLIBuildArgParsing:
    """Verify the CLI register and rebuild scripts parse --build-arg correctly."""

    def test_register_parses_single_build_arg(self):
        from cli.register import _parse_build_args
        result = _parse_build_args(["HTTP_PROXY=http://proxy:8080"])
        assert result == {"HTTP_PROXY": "http://proxy:8080"}

    def test_register_parses_multiple_build_args(self):
        from cli.register import _parse_build_args
        result = _parse_build_args([
            "HTTP_PROXY=http://p:8080",
            "HTTPS_PROXY=http://p:8443",
        ])
        assert len(result) == 2
        assert result["HTTP_PROXY"] == "http://p:8080"
        assert result["HTTPS_PROXY"] == "http://p:8443"

    def test_register_no_build_args_returns_none(self):
        from cli.register import _parse_build_args
        assert _parse_build_args([]) is None

    def test_register_build_arg_values_with_equals(self):
        """Values containing '=' should be split on the FIRST '=' only."""
        from cli.register import _parse_build_args
        result = _parse_build_args(["TOKEN=abc=def=ghi"])
        assert result == {"TOKEN": "abc=def=ghi"}

    def test_register_empty_values(self):
        from cli.register import _parse_build_args
        result = _parse_build_args(["EMPTY="])
        assert result == {"EMPTY": ""}

    def test_rebuild_parses_build_args(self):
        from cli.rebuild import _parse_build_args
        result = _parse_build_args([
            "HTTP_PROXY=http://proxy:3128",
            "NO_PROXY=localhost",
        ])
        assert result == {
            "HTTP_PROXY": "http://proxy:3128",
            "NO_PROXY": "localhost",
        }

    def test_rebuild_no_args_returns_none(self):
        from cli.rebuild import _parse_build_args
        assert _parse_build_args([]) is None

    def test_rebuild_spaces_around_equals(self):
        from cli.rebuild import _parse_build_args
        result = _parse_build_args(["KEY = value"])
        # The CLI parser does NOT strip spaces; the user should not include them.
        # This test documents current behavior.
        assert "KEY " in result or "KEY" in result


# ─────────────────────────────────────────────────────────────────────
# MockProxy
# ─────────────────────────────────────────────────────────────────────

class TestMockProxy:
    """Verify the mock proxy starts, stops, and relays HTTP requests."""

    def test_start_and_stop(self):
        from tests.mock_proxy import MockProxy
        proxy = MockProxy()
        proxy.start()
        assert proxy.port > 0
        assert proxy.url.startswith("http://127.0.0.1:")
        proxy.stop()

    def test_context_manager(self):
        from tests.mock_proxy import MockProxy
        with MockProxy(offline=True) as proxy:
            assert proxy.port > 0
        # After __exit__ the server is stopped

    def test_http_get_relay(self):
        from tests.mock_proxy import MockProxy
        import urllib.request
        with MockProxy(offline=True) as proxy:
            proxy_url = proxy.url
            proxy_handler = urllib.request.ProxyHandler({
                "http": proxy_url,
                "https": proxy_url,
            })
            opener = urllib.request.build_opener(proxy_handler)
            resp = opener.open("http://example.com", timeout=10)
            assert resp.status == 200
            assert proxy.request_count() >= 1
            assert proxy.history[0]["method"] == "GET"

    def test_clear_history(self):
        from tests.mock_proxy import MockProxy
        import urllib.request
        with MockProxy(offline=True) as proxy:
            proxy_handler = urllib.request.ProxyHandler({"http": proxy.url})
            opener = urllib.request.build_opener(proxy_handler)
            opener.open("http://example.com", timeout=10)
            assert proxy.request_count() >= 1
            proxy.clear_history()
            assert proxy.request_count() == 0
            assert proxy.history == []

    def test_history_structure(self):
        from tests.mock_proxy import MockProxy
        import urllib.request
        with MockProxy(offline=True) as proxy:
            proxy_handler = urllib.request.ProxyHandler({"http": proxy.url})
            opener = urllib.request.build_opener(proxy_handler)
            req = urllib.request.Request(
                "http://example.com",
                headers={"X-Test": "proxy-test"},
            )
            opener.open(req, timeout=10)
            assert proxy.request_count() >= 1
            entry = proxy.history[0]
            assert "method" in entry
            assert entry["method"] == "GET"
            assert "path" in entry
            assert "headers" in entry
            assert "body_len" in entry
            assert "timestamp" in entry

    def test_concurrent_proxy_unique_ports(self):
        """Two proxy instances on port 0 get different OS-assigned ports."""
        from tests.mock_proxy import MockProxy
        with MockProxy(port=0) as p1, MockProxy(port=0) as p2:
            assert p1.port != p2.port
            assert p1.port > 0
            assert p2.port > 0

    def test_port_explicit(self):
        from tests.mock_proxy import MockProxy
        # Use a fixed high port unlikely to be in use
        with MockProxy(port=19876) as proxy:
            assert proxy.port == 19876

    def test_empty_history_on_start(self):
        from tests.mock_proxy import MockProxy
        proxy = MockProxy()
        proxy.start()
        assert proxy.request_count() == 0
        proxy.stop()

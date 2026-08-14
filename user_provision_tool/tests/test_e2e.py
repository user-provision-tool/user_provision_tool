"""End-to-end tests for the user provision tool scripts.

These tests exercise the full registration → status → rebuild → removal
workflow by running the actual entry-point scripts through subprocess, with:
  - docker compose calls mocked out (subprocess.run patched)
  - a temp directory used for generated/ and user_registry.yml
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest
import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES_DIR = Path(__file__).parent / "fixtures"
COMPOSE_TEMPLATE = str(FIXTURES_DIR / "docker-compose.template.yml.j2")
NGINX_TEMPLATE = str(FIXTURES_DIR / "myapp.template.nginx.conf.j2")

from lib import registry as reg_mod
from lib import docker_ops


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_script(script: str, args: list[str], env_overrides: dict | None = None) -> subprocess.CompletedProcess:
    """Run a provision script in-process via importlib to allow monkeypatching."""
    raise NotImplementedError  # not used — we call main() directly instead


class _MockDockerCalls:
    """Thin wrapper so call_args_list stays in sync with the live raw_calls list."""

    def __init__(self, raw_calls: list[list[str]]):
        self._raw = raw_calls

    @property
    def call_args_list(self) -> list:
        return [call(c) for c in self._raw]

    def reset_mock(self) -> None:
        self._raw.clear()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect REGISTRY_FILE and generated/ to tmp_path for every e2e test."""
    monkeypatch.setattr(reg_mod, "REGISTRY_FILE", tmp_path / "user_registry.yml")
    # Patch the GENERATED_DIR used in cli.register
    import cli.register as reg_script
    monkeypatch.setattr(reg_script, "GENERATED_DIR", tmp_path / "generated")
    (tmp_path / "generated").mkdir()
    return tmp_path


@pytest.fixture()
def mock_docker(monkeypatch: pytest.MonkeyPatch) -> _MockDockerCalls:
    """Patch subprocess.Popen inside docker_ops to prevent real docker calls.

    Returns a wrapper whose ``call_args_list`` is populated with
    ``call(args)`` entries so existing tests using ``c.args[0]`` continue
    to work.  Supports ``reset_mock()``.
    """
    raw_calls: list[list[str]] = []

    class _FakeProc:
        def __init__(self, args, **kwargs):
            raw_calls.append(list(args))
            self.returncode = 0
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("")
        def wait(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass

    monkeypatch.setattr(docker_ops.subprocess, "Popen", _FakeProc)
    return _MockDockerCalls(raw_calls)


@pytest.fixture()
def mock_docker_ps_empty(monkeypatch: pytest.MonkeyPatch):
    """Make docker_ps return no containers."""
    monkeypatch.setattr(docker_ops, "docker_ps", lambda: [])


@pytest.fixture()
def registered_alice(
    tmp_path: Path,
    mock_docker: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> dict:
    """Run cli.register for alice and return the registry entry."""
    import shutil
    import cli.register as reg_script

    # Copy templates into tmp_path so it can serve as the project root
    compose_tpl = "docker-compose.template.yml.j2"
    nginx_tpl = "myapp.template.nginx.conf.j2"
    shutil.copy(FIXTURES_DIR / compose_tpl, tmp_path / compose_tpl)
    shutil.copy(FIXTURES_DIR / nginx_tpl, tmp_path / nginx_tpl)

    monkeypatch.setattr("getpass.getpass", lambda prompt="": "secret123")
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    sys_argv = [
        "cli/register.py",
        "-u", "alice",
        "-sn", "myapp",
        "-pr", str(tmp_path),
        "-tc", compose_tpl,
        "-tn", nginx_tpl,
        "-l", "0",
        "-d", "example.com",
        "-v", "app_data=/srv/alice/app",
        "-v", "db_data=/srv/alice/db",
    ]
    with patch.object(sys, "argv", sys_argv):
        reg_script.main()

    entry = reg_mod.get_user_service("alice", "myapp", "0")
    assert entry is not None, "Registration did not write registry entry"
    return entry


@pytest.fixture()
def registered_alice_no_passwd(
    tmp_path: Path,
    mock_docker: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> dict:
    """Run cli.register for alice with an empty password and return the registry entry."""
    import shutil
    import cli.register as reg_script

    compose_tpl = "docker-compose.template.yml.j2"
    nginx_tpl = "myapp.template.nginx.conf.j2"
    shutil.copy(FIXTURES_DIR / compose_tpl, tmp_path / compose_tpl)
    shutil.copy(FIXTURES_DIR / nginx_tpl, tmp_path / nginx_tpl)

    # Empty string → no htpasswd
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    sys_argv = [
        "cli/register.py",
        "-u", "alice",
        "-sn", "myapp",
        "-pr", str(tmp_path),
        "-tc", compose_tpl,
        "-tn", nginx_tpl,
        "-l", "0",
        "-d", "example.com",
        "-v", "app_data=/srv/alice/app",
        "-v", "db_data=/srv/alice/db",
    ]
    with patch.object(sys, "argv", sys_argv):
        reg_script.main()

    entry = reg_mod.get_user_service("alice", "myapp", "0")
    assert entry is not None, "Registration did not write registry entry"
    return entry


# ---------------------------------------------------------------------------
# E2E: Registration
# ---------------------------------------------------------------------------

class TestE2ERegistration:
    def test_registry_entry_created(self, registered_alice):
        assert registered_alice["user_name"] == "alice"
        assert registered_alice["service_name"] == "myapp"
        assert registered_alice["label"] == "0"
        assert registered_alice["volumes"]["app_data"] == "/srv/alice/app"
        # network_name must be stored in the registry entry
        assert registered_alice["network_name"] == "myapp-user_alice-0"

    def test_compose_file_generated(self, tmp_path, registered_alice):
        compose_path = Path(registered_alice["compose_file_path"])
        assert compose_path.exists()
        data = yaml.safe_load(compose_path.read_text())
        assert "web" in data["services"]
        assert "db" in data["services"]
        assert data["services"]["web"]["container_name"] == "myapp-user_alice-0-web"
        # Isolated network must be declared at top level and referenced by services
        expected_net = "myapp-user_alice-0"
        assert expected_net in data.get("networks", {})
        assert expected_net in data["services"]["web"].get("networks", [])
        assert expected_net in data["services"]["db"].get("networks", [])

    def test_nginx_conf_generated(self, registered_alice):
        nginx_path = Path(registered_alice["nginx_conf_path"])
        assert nginx_path.exists()
        content = nginx_path.read_text()
        assert "server_name myapp-alice-0.example.com;" in content
        assert "auth_basic_user_file" in content
        # Verify proxy_pass was rendered with the correct container_prefix
        assert "proxy_pass" in content
        assert "myapp-user_alice-0-web:80" in content
        # The raw template variable should NOT be in the rendered output
        assert "{{ container_prefix }}" not in content

    def test_htpasswd_file_generated(self, registered_alice):
        htpasswd_path = Path(registered_alice["htpasswd_path"])
        assert htpasswd_path.exists()
        content = htpasswd_path.read_text()
        assert content.startswith("alice:$2")

    def test_docker_compose_up_called(self, registered_alice, mock_docker):
        up_calls = [
            c for c in mock_docker.call_args_list
            if "up" in (c.args[0] if c.args else [])
        ]
        assert len(up_calls) >= 1

    def test_duplicate_registration_fails(
        self, registered_alice, mock_docker, monkeypatch, tmp_path
    ):
        import cli.register as reg_script
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "secret123")
        sys_argv = [
            "cli/register.py",
            "-u", "alice", "-sn", "myapp",
            "-pr", str(tmp_path),
            "-tc", "docker-compose.template.yml.j2",
            "-l", "0",
            "-v", "app_data=/srv/alice/app",
            "-v", "db_data=/srv/alice/db",
        ]
        with patch.object(sys, "argv", sys_argv):
            with pytest.raises(SystemExit) as exc:
                reg_script.main()
        assert exc.value.code != 0

    def test_invalid_username_rejected(self, mock_docker, monkeypatch, tmp_path):
        import cli.register as reg_script
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
        sys_argv = [
            "cli/register.py",
            "-u", "alice!", "-sn", "myapp",
            "-pr", str(tmp_path),
            "-tc", "docker-compose.template.yml.j2",
            "-l", "0",
        ]
        with patch.object(sys, "argv", sys_argv):
            with pytest.raises(SystemExit) as exc:
                reg_script.main()
        assert exc.value.code != 0

    def test_invalid_label_rejected(self, mock_docker, monkeypatch, tmp_path):
        import cli.register as reg_script
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
        sys_argv = [
            "cli/register.py",
            "-u", "alice", "-sn", "myapp",
            "-pr", str(tmp_path),
            "-tc", "docker-compose.template.yml.j2",
            "-l", "abc",
        ]
        with patch.object(sys, "argv", sys_argv):
            with pytest.raises(SystemExit) as exc:
                reg_script.main()
        assert exc.value.code != 0

    def test_volume_mismatch_abort(self, mock_docker, monkeypatch, tmp_path):
        """When wrong volume keys are provided and user types 'n', registration aborts."""
        import shutil
        import cli.register as reg_script
        compose_tpl = "docker-compose.template.yml.j2"
        shutil.copy(FIXTURES_DIR / compose_tpl, tmp_path / compose_tpl)
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        sys_argv = [
            "cli/register.py",
            "-u", "carol", "-sn", "myapp",
            "-pr", str(tmp_path),
            "-tc", compose_tpl, "-l", "0",
            "-v", "wrong_key=/some/path",  # key not in template → mismatch warning
        ]
        with patch.object(sys, "argv", sys_argv):
            with pytest.raises(SystemExit) as exc:
                reg_script.main()
        assert exc.value.code == 0  # exits cleanly with "Aborted"
        assert reg_mod.get_user_service("carol", "myapp", "0") is None

    def test_volume_mismatch_confirm_continues(self, mock_docker, monkeypatch, tmp_path):
        """When volumes don't match and user types 'y', registration proceeds.
        We supply app_data/db_data (not in template detection list → triggers
        'extra' warning) so the compose template can still render."""
        import shutil
        import cli.register as reg_script
        compose_tpl = "docker-compose.template.yml.j2"
        shutil.copy(FIXTURES_DIR / compose_tpl, tmp_path / compose_tpl)
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        sys_argv = [
            "cli/register.py",
            "-u", "dave", "-sn", "myapp",
            "-pr", str(tmp_path),
            "-tc", compose_tpl, "-l", "0",
            "-v", "app_data=/data/dave/app",
            "-v", "db_data=/data/dave/db",
        ]
        with patch.object(sys, "argv", sys_argv):
            reg_script.main()
        assert reg_mod.get_user_service("dave", "myapp", "0") is not None

    def test_two_users_have_isolated_networks(self, tmp_path, mock_docker, monkeypatch):
        """Different users must get different network names in the registry."""
        import shutil
        import cli.register as reg_script
        compose_tpl = "docker-compose.template.yml.j2"
        for user, label in [("alice", "0"), ("bob", "1")]:
            user_root = tmp_path / f"project_{user}"
            user_root.mkdir()
            shutil.copy(FIXTURES_DIR / compose_tpl, user_root / compose_tpl)
            monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
            monkeypatch.setattr("builtins.input", lambda prompt="": "y")
            sys_argv = [
                "cli/register.py",
                "-u", user, "-sn", "myapp",
                "-pr", str(user_root),
                "-tc", compose_tpl,
                "-l", label,
                "-v", f"app_data=/data/{user}/app",
                "-v", f"db_data=/data/{user}/db",
            ]
            with patch.object(sys, "argv", sys_argv):
                reg_script.main()
        e_alice = reg_mod.get_user_service("alice", "myapp", "0")
        e_bob = reg_mod.get_user_service("bob", "myapp", "1")
        assert e_alice["network_name"] != e_bob["network_name"]

    def test_auto_volumes_created_when_no_v_flag(self, mock_docker, monkeypatch, tmp_path):
        """Registering without -v auto-creates volume dirs under USER_DATA_DIR."""
        import shutil
        import cli.register as reg_script
        compose_tpl = "docker-compose.template.yml.j2"
        shutil.copy(FIXTURES_DIR / compose_tpl, tmp_path / compose_tpl)
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
        user_data = tmp_path / "user_data"
        monkeypatch.setattr("cli.register.USER_DATA_DIR", user_data)
        sys_argv = [
            "cli/register.py",
            "-u", "frank", "-sn", "myapp",
            "-pr", str(tmp_path),
            "-tc", compose_tpl, "-l", "0",
            # no -v flags — auto-generation under USER_DATA_DIR
        ]
        with patch.object(sys, "argv", sys_argv):
            reg_script.main()
        entry = reg_mod.get_user_service("frank", "myapp", "0")
        assert entry is not None
        base = user_data / "frank" / "myapp" / "0"
        assert (base / "app_data").is_dir()
        assert (base / "db_data").is_dir()
        assert entry["volumes"]["app_data"] == str(base / "app_data")
        assert entry["volumes"]["db_data"] == str(base / "db_data")

    def test_plain_nginx_conf_proxy_pass_compose_service_name_detected(
        self, tmp_path, mock_docker, monkeypatch,
    ):
        """When a plain nginx.conf uses a compose service name in proxy_pass,
        the converter detects it and rewrites the host to {{ container_prefix }}<name>.
        """
        import shutil
        import cli.register as reg_script

        # --- Write a plain docker-compose.yml with 'web' and 'db' services ---
        compose_yml = tmp_path / "docker-compose.yml"
        compose_yml.write_text("""\
services:
  web:
    image: nginx:alpine
    container_name: web
    expose:
      - "80"
    volumes:
      - app_data:/usr/share/nginx/html:ro
    networks:
      - myapp-net
  db:
    image: postgres:16-alpine
    container_name: db
    environment:
      - POSTGRES_DB=myapp
    volumes:
      - db_data:/var/lib/postgresql/data
    networks:
      - myapp-net

volumes:
  app_data:
  db_data:

networks:
  myapp-net:
""")

        # --- Write a plain nginx.conf that uses compose service names ---
        nginx_conf = tmp_path / "nginx.conf"
        nginx_conf.write_text("""\
server {
    listen 80;
    server_name myapp.example.com;

    location / {
        proxy_pass http://web:80;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
""")

        monkeypatch.setattr("getpass.getpass", lambda prompt="": "secret123")
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")

        sys_argv = [
            "cli/register.py",
            "-u", "alice", "-sn", "myapp",
            "-pr", str(tmp_path),
            "-fc", "docker-compose.yml",
            "-fn", "nginx.conf",
            "-l", "0",
            "-d", "example.com",
            "-v", "app_data=/srv/alice/app",
            "-v", "db_data=/srv/alice/db",
        ]
        with patch.object(sys, "argv", sys_argv):
            reg_script.main()

        entry = reg_mod.get_user_service("alice", "myapp", "0")
        assert entry is not None, "Registration did not write registry entry"

        # --- Verify the generated nginx conf ---
        nginx_out = Path(entry["nginx_conf_path"])
        assert nginx_out.exists(), f"nginx conf not found at {nginx_out}"
        content = nginx_out.read_text()
        # The compose service name "web" should have been replaced
        # Now uses variable-based form: set $upstream_XXXX myapp-user_alice-0-web:80;
        assert "myapp-user_alice-0-web:80" in content, (
            f"Expected rendered container name in proxy_pass, got:\n{content}"
        )
        assert "set $upstream_" in content, (
            f"Expected variable-based proxy_pass (set $upstream_...), got:\n{content}"
        )
        # The raw template variable should NOT be in the rendered output
        assert "{{ container_prefix }}" not in content

        # --- Verify the generated .j2 template (intermediate file) ---
        j2_path = tmp_path / "nginx.conf.j2"
        assert j2_path.exists(), f".j2 template not found at {j2_path}"
        j2_content = j2_path.read_text()
        # The template should have {{ container_prefix }}web (not the literal "web")
        assert "{{ container_prefix }}web:80" in j2_content, (
            f"Expected {{ container_prefix }}web in template, got:\n{j2_content}"
        )
        # The original literal host should NOT remain in the template
        assert "http://web:80" not in j2_content

    def test_plain_nginx_conf_external_host_rejected(
        self, tmp_path, mock_docker, monkeypatch,
    ):
        """A proxy_pass to a host not in compose services is rejected with a clear error."""
        import shutil
        import cli.register as reg_script

        compose_yml = tmp_path / "docker-compose.yml"
        compose_yml.write_text("""\
services:
  web:
    image: nginx:alpine
    container_name: web
    expose:
      - "80"
    networks:
      - myapp-net

networks:
  myapp-net:
""")

        nginx_conf = tmp_path / "nginx.conf"
        nginx_conf.write_text("""\
server {
    listen 80;
    server_name myapp.example.com;

    location / {
        proxy_pass http://external-api:9000;
        proxy_set_header Host $host;
    }
}
""")

        monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")

        sys_argv = [
            "cli/register.py",
            "-u", "alice", "-sn", "myapp",
            "-pr", str(tmp_path),
            "-fc", "docker-compose.yml",
            "-fn", "nginx.conf",
            "-l", "0",
            "-d", "example.com",
        ]
        with patch.object(sys, "argv", sys_argv):
            with pytest.raises(SystemExit) as exc_info:
                reg_script.main()

        assert exc_info.value.code == 1

        # No registry entry should be created (registration was blocked)
        entry = reg_mod.get_user_service("alice", "myapp", "0")
        assert entry is None

class TestE2ESubnetAllocation:
    """Gap 7: the enabled subnet path (SUBNET_POOLS set) renders an ipam block
    with a right-sized subnet, and pool exhaustion surfaces cleanly."""

    @pytest.fixture()
    def registered_with_subnet(self, tmp_path, mock_docker, monkeypatch):
        import shutil
        import cli.register as reg_script
        from lib import subnet_manager

        monkeypatch.setenv("SUBNET_POOLS", "10.0.0.0/16")
        subnet_manager._load_env()

        compose_tpl = "docker-compose.template.yml.j2"
        nginx_tpl = "myapp.template.nginx.conf.j2"
        shutil.copy(FIXTURES_DIR / compose_tpl, tmp_path / compose_tpl)
        shutil.copy(FIXTURES_DIR / nginx_tpl, tmp_path / nginx_tpl)
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "secret123")
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")

        sys_argv = [
            "cli/register.py",
            "-u", "subnetuser",
            "-sn", "myapp",
            "-pr", str(tmp_path),
            "-tc", compose_tpl,
            "-tn", nginx_tpl,
            "-l", "0",
            "-d", "example.com",
            "-v", "app_data=/srv/subnetuser/app",
            "-v", "db_data=/srv/subnetuser/db",
        ]
        with patch.object(sys, "argv", sys_argv):
            reg_script.main()

        entry = reg_mod.get_user_service("subnetuser", "myapp", "0")
        assert entry is not None, "Registration did not write registry entry"
        return entry

    def test_registry_entry_has_allocated_subnet(self, registered_with_subnet):
        entry = registered_with_subnet
        assert entry["subnet"], "subnet should be allocated when SUBNET_POOLS is set"
        assert entry["gateway"].endswith(".1")
        # 2 containers + 2 headroom + 1 gateway = 5 usable → /29 (8 addresses)
        assert entry["subnet"].endswith("/29")

    def test_rendered_compose_has_ipam_block(self, registered_with_subnet):
        compose_path = Path(registered_with_subnet["compose_file_path"])
        data = yaml.safe_load(compose_path.read_text())
        net_name = registered_with_subnet["network_name"]
        net = data["networks"][net_name]
        ipam = net["ipam"]
        assert ipam["config"][0]["subnet"] == registered_with_subnet["subnet"]
        assert ipam["config"][0]["gateway"] == registered_with_subnet["gateway"]

    def test_pool_exhaustion_raises(self, tmp_path, mock_docker, monkeypatch):
        """A fully-allocated /16 pool leaves no free slot; allocation raises
        RuntimeError (design §6/§14) rather than silently falling back."""
        from lib import subnet_manager
        monkeypatch.setenv("SUBNET_POOLS", "10.0.0.0/16")
        subnet_manager._load_env()
        # The whole pool is already allocated → no free slots remain
        with pytest.raises(RuntimeError):
            subnet_manager.allocate_subnet(1, ["10.0.0.0/16"])


class TestE2ERemoval:
    def test_removal_deregisters_user(self, registered_alice, mock_docker):
        import cli.remove as rem_script
        sys_argv = [
            "cli/remove.py",
            "-u", "alice", "-sn", "myapp", "-l", "0",
        ]
        with patch.object(sys, "argv", sys_argv):
            rem_script.main()
        assert reg_mod.get_user_service("alice", "myapp", "0") is None

    def test_removal_calls_compose_down(self, registered_alice, mock_docker):
        import cli.remove as rem_script
        sys_argv = [
            "cli/remove.py",
            "-u", "alice", "-sn", "myapp", "-l", "0",
        ]
        with patch.object(sys, "argv", sys_argv):
            rem_script.main()
        down_calls = [
            c for c in mock_docker.call_args_list
            if "down" in (c.args[0] if c.args else [])
        ]
        assert len(down_calls) >= 1

    def test_removal_nonexistent_user_fails(self, mock_docker):
        import cli.remove as rem_script
        sys_argv = [
            "cli/remove.py",
            "-u", "ghost", "-sn", "myapp", "-l", "0",
        ]
        with patch.object(sys, "argv", sys_argv):
            with pytest.raises(SystemExit) as exc:
                rem_script.main()
        assert exc.value.code != 0


# ---------------------------------------------------------------------------
# E2E: No-password registration
# ---------------------------------------------------------------------------

class TestE2ENoPassword:
    """When passwd is explicitly empty: no htpasswd file, htpasswd_path is None,
    and the rendered nginx conf contains no auth_basic directives."""

    def test_htpasswd_path_is_null(self, registered_alice_no_passwd):
        assert registered_alice_no_passwd["htpasswd_path"] is None

    def test_htpasswd_file_not_created(self, registered_alice_no_passwd, tmp_path):
        # htpasswd_path is None, so no file should exist in the generated dir
        import glob
        htpasswd_files = glob.glob(str(tmp_path / "*.htpasswd"))
        assert htpasswd_files == []

    def test_nginx_conf_has_no_auth_basic(self, registered_alice_no_passwd):
        nginx_path = Path(registered_alice_no_passwd["nginx_conf_path"])
        assert nginx_path.exists()
        content = nginx_path.read_text()
        assert "auth_basic" not in content

    def test_nginx_conf_still_has_proxy_pass(self, registered_alice_no_passwd):
        nginx_path = Path(registered_alice_no_passwd["nginx_conf_path"])
        content = nginx_path.read_text()
        assert "proxy_pass" in content


# ---------------------------------------------------------------------------
# E2E: Rebuild
# ---------------------------------------------------------------------------

class TestE2ERebuild:
    def test_rebuild_calls_build_and_up(self, registered_alice, mock_docker):
        import cli.rebuild as reb_script
        sys_argv = [
            "cli/rebuild.py",
            "-u", "alice", "-sn", "myapp", "-l", "0",
        ]
        with patch.object(sys, "argv", sys_argv):
            reb_script.main()
        cmds = [c.args[0] for c in mock_docker.call_args_list if c.args]
        assert any("build" in cmd for cmd in cmds)
        assert any("up" in cmd for cmd in cmds)

    def test_rebuild_no_cache_flag_passed(self, registered_alice, mock_docker):
        import cli.rebuild as reb_script
        sys_argv = [
            "cli/rebuild.py",
            "-u", "alice", "-sn", "myapp", "-l", "0", "--no-cache",
        ]
        with patch.object(sys, "argv", sys_argv):
            reb_script.main()
        cmds = [c.args[0] for c in mock_docker.call_args_list if c.args]
        build_cmds = [cmd for cmd in cmds if "build" in cmd]
        assert any("--no-cache" in cmd for cmd in build_cmds)

    def test_rebuild_nonexistent_user_fails(self, mock_docker):
        import cli.rebuild as reb_script
        sys_argv = [
            "cli/rebuild.py",
            "-u", "ghost", "-sn", "myapp", "-l", "0",
        ]
        with patch.object(sys, "argv", sys_argv):
            with pytest.raises(SystemExit) as exc:
                reb_script.main()
        assert exc.value.code != 0


# ---------------------------------------------------------------------------
# E2E: Status
# ---------------------------------------------------------------------------

class TestE2EStatus:
    def test_status_all_containers_healthy(
        self, registered_alice, monkeypatch, capsys
    ):
        import cli.status as st_script
        # Mock docker_ps to return both expected containers as "Up"
        monkeypatch.setattr(docker_ops, "docker_ps", lambda: [
            {"name": "myapp-user_alice-0-web", "status": "Up 2 minutes", "image": "nginx:alpine"},
            {"name": "myapp-user_alice-0-db", "status": "Up 2 minutes", "image": "postgres:16-alpine"},
        ])
        with patch.object(sys, "argv", ["cli/status.py", "-u", "alice"]):
            st_script.main()
        out = capsys.readouterr().out
        data = json.loads(out)
        user = data["user_status"][0]
        assert user["user_name"] == "alice"
        assert user["summary"]["healthy_services_#"] == 1
        assert user["summary"]["unhealthy_services_#"] == 0
        svc = user["healthy_services"][0]
        assert "myapp-user_alice-0-web" in svc["healthy_containers"]
        assert "myapp-user_alice-0-db" in svc["healthy_containers"]

    def test_status_missing_containers(
        self, registered_alice, monkeypatch, capsys
    ):
        import cli.status as st_script
        # No containers running
        monkeypatch.setattr(docker_ops, "docker_ps", lambda: [])
        with patch.object(sys, "argv", ["cli/status.py", "-u", "alice"]):
            st_script.main()
        out = capsys.readouterr().out
        data = json.loads(out)
        user = data["user_status"][0]
        assert user["summary"]["healthy_services_#"] == 0
        # Service should appear as missing (all containers absent)
        all_missing = user["missing_services"]
        assert len(all_missing) == 1
        assert "myapp-user_alice-0-web" in all_missing[0]["missing_containers"]

    def test_status_partial_unhealthy(
        self, registered_alice, monkeypatch, capsys
    ):
        import cli.status as st_script
        # Only web is up; db is missing
        monkeypatch.setattr(docker_ops, "docker_ps", lambda: [
            {"name": "myapp-user_alice-0-web", "status": "Up 5 minutes", "image": "nginx:alpine"},
        ])
        with patch.object(sys, "argv", ["cli/status.py", "-u", "alice"]):
            st_script.main()
        out = capsys.readouterr().out
        data = json.loads(out)
        user = data["user_status"][0]
        assert user["summary"]["healthy_services_#"] == 0
        assert user["summary"]["unhealthy_services_#"] == 1

    def test_status_unhealthy_container(
        self, registered_alice, monkeypatch, capsys
    ):
        import cli.status as st_script
        monkeypatch.setattr(docker_ops, "docker_ps", lambda: [
            {"name": "myapp-user_alice-0-web", "status": "Up 1 minute (unhealthy)", "image": "nginx:alpine"},
            {"name": "myapp-user_alice-0-db", "status": "Up 1 minute", "image": "postgres:16-alpine"},
        ])
        with patch.object(sys, "argv", ["cli/status.py", "-u", "alice"]):
            st_script.main()
        out = capsys.readouterr().out
        data = json.loads(out)
        user = data["user_status"][0]
        assert user["summary"]["healthy_services_#"] == 0
        svc = user["unhealthy_services"][0]
        assert "myapp-user_alice-0-web" in svc["unhealthy_containers"]

    def test_status_all_users_returned(
        self, tmp_path, mock_docker, monkeypatch, capsys
    ):
        import cli.register as reg_script
        import cli.status as st_script

        # Register alice and bob
        import shutil
        compose_tpl = "docker-compose.template.yml.j2"
        for user, label in [("alice", "0"), ("bob", "1")]:
            user_root = tmp_path / f"project_{user}"
            user_root.mkdir()
            shutil.copy(FIXTURES_DIR / compose_tpl, user_root / compose_tpl)
            monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
            monkeypatch.setattr("builtins.input", lambda prompt="": "y")
            sys_argv = [
                "cli/register.py",
                "-u", user, "-sn", "myapp",
                "-pr", str(user_root),
                "-tc", compose_tpl,
                "-l", label,
                "-v", f"app_data=/data/{user}/app",
                "-v", f"db_data=/data/{user}/db",
            ]
            with patch.object(sys, "argv", sys_argv):
                reg_script.main()

        capsys.readouterr()  # flush registration output before checking status
        monkeypatch.setattr(docker_ops, "docker_ps", lambda: [])
        with patch.object(sys, "argv", ["cli/status.py"]):
            st_script.main()
        out = capsys.readouterr().out
        data = json.loads(out)
        names = [u["user_name"] for u in data["user_status"]]
        assert "alice" in names
        assert "bob" in names

    def test_status_unknown_user_fails(self, mock_docker, monkeypatch):
        import cli.status as st_script
        monkeypatch.setattr(docker_ops, "docker_ps", lambda: [])
        with patch.object(sys, "argv", ["cli/status.py", "-u", "ghost"]):
            with pytest.raises(SystemExit) as exc:
                st_script.main()
        assert exc.value.code != 0


# ---------------------------------------------------------------------------
# E2E: Full lifecycle  (register → status → rebuild → remove)
# ---------------------------------------------------------------------------

class TestE2EFullLifecycle:
    def test_full_lifecycle(
        self, tmp_path, mock_docker, monkeypatch, capsys
    ):
        import cli.register as reg_script
        import cli.rebuild as reb_script
        import cli.remove as rem_script
        import cli.status as st_script

        # 1. Register
        import shutil
        compose_tpl = "docker-compose.template.yml.j2"
        nginx_tpl = "myapp.template.nginx.conf.j2"
        shutil.copy(FIXTURES_DIR / compose_tpl, tmp_path / compose_tpl)
        shutil.copy(FIXTURES_DIR / nginx_tpl, tmp_path / nginx_tpl)
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "pass1")
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        sys_argv = [
            "cli/register.py",
            "-u", "lifecycle_user", "-sn", "myapp",
            "-pr", str(tmp_path),
            "-tc", compose_tpl,
            "-tn", nginx_tpl,
            "-l", "5",
            "-d", "test.local",
            "-v", "app_data=/data/lc/app",
            "-v", "db_data=/data/lc/db",
        ]
        with patch.object(sys, "argv", sys_argv):
            reg_script.main()

        entry = reg_mod.get_user_service("lifecycle_user", "myapp", "5")
        assert entry is not None
        assert Path(entry["compose_file_path"]).exists()
        assert Path(entry["nginx_conf_path"]).exists()
        capsys.readouterr()  # flush registration output before checking status

        # 2. Status — healthy
        monkeypatch.setattr(docker_ops, "docker_ps", lambda: [
            {"name": "myapp-user_lifecycle_user-5-web", "status": "Up", "image": ""},
            {"name": "myapp-user_lifecycle_user-5-db", "status": "Up", "image": ""},
        ])
        with patch.object(sys, "argv", ["cli/status.py", "-u", "lifecycle_user"]):
            st_script.main()
        out = capsys.readouterr().out
        status_data = json.loads(out)
        assert status_data["user_status"][0]["summary"]["healthy_services_#"] == 1

        # 3. Rebuild with --no-cache
        mock_docker.reset_mock()
        sys_argv = [
            "cli/rebuild.py",
            "-u", "lifecycle_user", "-sn", "myapp", "-l", "5", "--no-cache",
        ]
        with patch.object(sys, "argv", sys_argv):
            reb_script.main()
        cmds = [c.args[0] for c in mock_docker.call_args_list if c.args]
        assert any("build" in cmd and "--no-cache" in cmd for cmd in cmds)

        # 4. Remove
        mock_docker.reset_mock()
        sys_argv = [
            "cli/remove.py",
            "-u", "lifecycle_user", "-sn", "myapp", "-l", "5",
        ]
        with patch.object(sys, "argv", sys_argv):
            rem_script.main()
        assert reg_mod.get_user_service("lifecycle_user", "myapp", "5") is None
        down_calls = [
            c for c in mock_docker.call_args_list
            if "down" in (c.args[0] if c.args else [])
        ]
        assert len(down_calls) >= 1


# ---------------------------------------------------------------------------
# E2E: Converter integration  (-fc / -fn flags)
# ---------------------------------------------------------------------------

class TestE2EConverterIntegration:
    """E2E tests for register.py using -fc (plain compose) and -fn (plain nginx)
    flags that trigger automatic conversion to Jinja2 templates."""

    def test_register_with_fc_flag(self, tmp_path, mock_docker, monkeypatch):
        """-fc converts a plain docker-compose.yml before registering."""
        import shutil
        import cli.register as reg_script

        shutil.copy(FIXTURES_DIR / "docker-compose.plain.yml", tmp_path / "docker-compose.plain.yml")
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        sys_argv = [
            "cli/register.py",
            "-u", "alice", "-sn", "myapp",
            "-pr", str(tmp_path),
            "-fc", "docker-compose.plain.yml",
            "-l", "0",
            "-v", "html=/srv/alice/html",
            "-v", "db=/srv/alice/db",
        ]
        with patch.object(sys, "argv", sys_argv):
            reg_script.main()

        entry = reg_mod.get_user_service("alice", "myapp", "0")
        assert entry is not None
        # A .j2 template must have been generated alongside the plain file
        assert (tmp_path / "docker-compose.plain.yml.j2").exists() or any(
            (tmp_path / p).exists() for p in ["docker-compose.plain.j2", "docker-compose.yml.j2"]
        )

    def test_register_with_fc_generates_valid_compose(self, tmp_path, mock_docker, monkeypatch):
        """The compose file rendered from a -fc conversion is valid YAML with services."""
        import shutil
        import cli.register as reg_script

        shutil.copy(FIXTURES_DIR / "docker-compose.plain.yml", tmp_path / "docker-compose.plain.yml")
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        sys_argv = [
            "cli/register.py",
            "-u", "alice", "-sn", "myapp",
            "-pr", str(tmp_path),
            "-fc", "docker-compose.plain.yml",
            "-l", "0",
            "-v", "html=/srv/alice/html",
            "-v", "db=/srv/alice/db",
        ]
        with patch.object(sys, "argv", sys_argv):
            reg_script.main()

        entry = reg_mod.get_user_service("alice", "myapp", "0")
        compose_path = Path(entry["compose_file_path"])
        assert compose_path.exists()
        data = yaml.safe_load(compose_path.read_text())
        assert "services" in data
        assert data["services"]["web"]["container_name"] == "myapp-user_alice-0-web"

    def test_register_with_fn_flag(self, tmp_path, mock_docker, monkeypatch):
        """-fn converts a plain nginx.conf before registering."""
        import shutil
        import cli.register as reg_script

        shutil.copy(FIXTURES_DIR / "docker-compose.template.yml.j2", tmp_path / "docker-compose.template.yml.j2")
        shutil.copy(FIXTURES_DIR / "myapp.plain.nginx.conf", tmp_path / "myapp.plain.nginx.conf")
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        sys_argv = [
            "cli/register.py",
            "-u", "alice", "-sn", "myapp",
            "-pr", str(tmp_path),
            "-tc", "docker-compose.template.yml.j2",
            "-fn", "myapp.plain.nginx.conf",
            "-l", "0",
            "-d", "example.com",
            "-v", "app_data=/srv/alice/app",
            "-v", "db_data=/srv/alice/db",
        ]
        with patch.object(sys, "argv", sys_argv):
            reg_script.main()

        entry = reg_mod.get_user_service("alice", "myapp", "0")
        assert entry is not None
        # A .j2 template should exist for the nginx conf
        assert (tmp_path / "myapp.plain.nginx.conf.j2").exists()

    def test_register_with_fn_generates_valid_nginx_conf(self, tmp_path, mock_docker, monkeypatch):
        """The nginx conf rendered from a -fn conversion has correct server_name."""
        import shutil
        import cli.register as reg_script

        shutil.copy(FIXTURES_DIR / "docker-compose.template.yml.j2", tmp_path / "docker-compose.template.yml.j2")
        shutil.copy(FIXTURES_DIR / "myapp.plain.nginx.conf", tmp_path / "myapp.plain.nginx.conf")
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        sys_argv = [
            "cli/register.py",
            "-u", "alice", "-sn", "myapp",
            "-pr", str(tmp_path),
            "-tc", "docker-compose.template.yml.j2",
            "-fn", "myapp.plain.nginx.conf",
            "-l", "0",
            "-d", "example.com",
            "-v", "app_data=/srv/alice/app",
            "-v", "db_data=/srv/alice/db",
        ]
        with patch.object(sys, "argv", sys_argv):
            reg_script.main()

        entry = reg_mod.get_user_service("alice", "myapp", "0")
        nginx_path = Path(entry["nginx_conf_path"])
        assert nginx_path.exists()
        content = nginx_path.read_text()
        assert "server_name myapp-alice-0.example.com;" in content

    def test_fc_and_tc_are_mutually_exclusive(self, tmp_path, mock_docker, monkeypatch):
        """Passing both -fc and -tc must fail with non-zero exit."""
        import cli.register as reg_script
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
        sys_argv = [
            "cli/register.py",
            "-u", "alice", "-sn", "myapp",
            "-pr", str(tmp_path),
            "-tc", "compose.yml.j2",
            "-fc", "compose.yml",
            "-l", "0",
        ]
        with patch.object(sys, "argv", sys_argv):
            with pytest.raises(SystemExit) as exc:
                reg_script.main()
        assert exc.value.code != 0

    def test_fn_and_tn_are_mutually_exclusive(self, tmp_path, mock_docker, monkeypatch):
        """Passing both -fn and -tn must fail with non-zero exit."""
        import cli.register as reg_script
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
        sys_argv = [
            "cli/register.py",
            "-u", "alice", "-sn", "myapp",
            "-pr", str(tmp_path),
            "-tc", "compose.yml.j2",
            "-tn", "nginx.conf.j2",
            "-fn", "nginx.conf",
            "-l", "0",
        ]
        with patch.object(sys, "argv", sys_argv):
            with pytest.raises(SystemExit) as exc:
                reg_script.main()
        assert exc.value.code != 0

    def test_fc_missing_file_exits_nonzero(self, tmp_path, mock_docker, monkeypatch):
        """-fc pointing to a non-existent file exits with non-zero code."""
        import cli.register as reg_script
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
        sys_argv = [
            "cli/register.py",
            "-u", "alice", "-sn", "myapp",
            "-pr", str(tmp_path),
            "-fc", "does-not-exist.yml",
            "-l", "0",
        ]
        with patch.object(sys, "argv", sys_argv):
            with pytest.raises(SystemExit) as exc:
                reg_script.main()
        assert exc.value.code != 0


# ---------------------------------------------------------------------------
# E2E: Project-root bare-name resolution  (-pr myapp → SOURCE_PROJECTS_DIR/myapp)
# ---------------------------------------------------------------------------

class TestE2EProjectRootResolution:
    """E2E tests for '-pr bare_name' resolution in cli.register."""

    def test_bare_pr_resolves_via_source_projects_dir(
        self, tmp_path, mock_docker, monkeypatch
    ):
        """-pr 'myapp' resolves to SOURCE_PROJECTS_DIR/myapp when that dir exists."""
        import shutil
        import cli.register as reg_script

        source_projects = tmp_path / "source_projects"
        project_dir = source_projects / "myapp"
        project_dir.mkdir(parents=True)
        shutil.copy(FIXTURES_DIR / "docker-compose.template.yml.j2", project_dir)
        shutil.copy(FIXTURES_DIR / "myapp.template.nginx.conf.j2", project_dir)

        # Patch the module-level SOURCE_PROJECTS_DIR so bare-name lookup uses tmp_path
        monkeypatch.setattr(reg_script, "SOURCE_PROJECTS_DIR", source_projects)
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "secret123")
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")

        sys_argv = [
            "cli/register.py",
            "-u", "barename",
            "-sn", "myapp",
            "-pr", "myapp",
            "-tc", "docker-compose.template.yml.j2",
            "-tn", "myapp.template.nginx.conf.j2",
            "-l", "0",
            "-d", "localhost",
            "-v", "app_data=/srv/barename/app",
            "-v", "db_data=/srv/barename/db",
        ]
        with patch.object(sys, "argv", sys_argv):
            reg_script.main()

        entry = reg_mod.get_user_service("barename", "myapp", "0")
        assert entry is not None, "Registration with bare -pr did not create registry entry"

    def test_bare_pr_nonexistent_exits_nonzero(self, tmp_path, mock_docker, monkeypatch):
        """-pr bare name with no matching dir in SOURCE_PROJECTS_DIR exits non-zero."""
        import cli.register as reg_script

        source_projects = tmp_path / "source_projects"
        source_projects.mkdir()
        monkeypatch.setattr(reg_script, "SOURCE_PROJECTS_DIR", source_projects)
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "secret")

        sys_argv = [
            "cli/register.py",
            "-u", "barename",
            "-sn", "myapp",
            "-pr", "nonexistent",
            "-tc", "docker-compose.template.yml.j2",
            "-l", "0",
        ]
        with patch.object(sys, "argv", sys_argv):
            with pytest.raises(SystemExit) as exc:
                reg_script.main()
        assert exc.value.code != 0

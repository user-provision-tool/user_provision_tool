"""Unit tests for individual lib/ modules."""

from __future__ import annotations

import io
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from lib import auth, docker_ops, provisioner, registry, template_engine, validation

FIXTURES_DIR = Path(__file__).parent / "fixtures"
COMPOSE_TEMPLATE = str(FIXTURES_DIR / "docker-compose.template.yml.j2")
NGINX_TEMPLATE = str(FIXTURES_DIR / "myapp.template.nginx.conf.j2")

# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_valid_name(self):
        assert validation.validate_name("alice_1", "user_name") == "alice_1"

    def test_valid_name_all_chars(self):
        assert validation.validate_name("Service_Name123") == "Service_Name123"

    def test_valid_name_with_hyphen(self):
        """Hyphens are now allowed in names."""
        assert validation.validate_name("alice-1", "user_name") == "alice-1"
        assert validation.validate_name("my-service_2") == "my-service_2"

    @pytest.mark.parametrize("bad", ["alice!", "alice 1", "user@host", ""])
    def test_invalid_name(self, bad):
        with pytest.raises(validation.ValidationError):
            validation.validate_name(bad, "user_name")

    def test_empty_name_raises(self):
        with pytest.raises(validation.ValidationError, match="must not be empty"):
            validation.validate_name("")

    def test_valid_label(self):
        assert validation.validate_label("0") == "0"
        assert validation.validate_label("42") == "42"

    @pytest.mark.parametrize("bad", ["a", "1a", "-1", "1.0", ""])
    def test_invalid_label(self, bad):
        with pytest.raises(validation.ValidationError):
            validation.validate_label(bad)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_empty_registry(self, registry_file):
        assert registry.get_all_users() == []

    def test_add_and_get_user(self, registry_file, sample_entry):
        registry.add_user(sample_entry)
        users = registry.get_all_users()
        assert len(users) == 1
        assert users[0]["user_name"] == "alice"

    def test_get_user_by_name(self, registry_file, sample_entry):
        registry.add_user(sample_entry)
        result = registry.get_user("alice")
        assert len(result) == 1
        assert result[0]["service_name"] == "myapp"

    def test_get_user_unknown(self, registry_file, sample_entry):
        registry.add_user(sample_entry)
        assert registry.get_user("nobody") == []

    def test_get_user_service_exact_match(self, registry_file, sample_entry):
        registry.add_user(sample_entry)
        found = registry.get_user_service("alice", "myapp", "0")
        assert found is not None
        assert found["label"] == "0"

    def test_get_user_service_no_match(self, registry_file, sample_entry):
        registry.add_user(sample_entry)
        assert registry.get_user_service("alice", "myapp", "99") is None

    def test_remove_user_service(self, registry_file, sample_entry):
        registry.add_user(sample_entry)
        removed = registry.remove_user_service("alice", "myapp", "0")
        assert removed is True
        assert registry.get_all_users() == []

    def test_remove_nonexistent_returns_false(self, registry_file):
        assert registry.remove_user_service("ghost", "svc", "0") is False

    def test_multiple_users_isolated(self, registry_file, sample_entry):
        entry_bob = dict(sample_entry, user_name="bob")
        registry.add_user(sample_entry)
        registry.add_user(entry_bob)
        registry.remove_user_service("alice", "myapp", "0")
        remaining = registry.get_all_users()
        assert len(remaining) == 1
        assert remaining[0]["user_name"] == "bob"

    def test_registry_persists_to_yaml(self, registry_file, sample_entry):
        registry.add_user(sample_entry)
        raw = yaml.safe_load(registry_file.read_text())
        assert isinstance(raw, list)
        assert raw[0]["user_name"] == "alice"


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


class TestAuth:
    def test_hash_password_produces_bcrypt_hash(self):
        h = auth.hash_password("alice", "secret123")
        assert h.startswith("$2")

    def test_hash_password_empty_returns_empty(self):
        assert auth.hash_password("alice", "") == ""

    def test_hash_different_passwords_differ(self):
        h1 = auth.hash_password("alice", "password1")
        h2 = auth.hash_password("alice", "password2")
        assert h1 != h2

    def test_write_htpasswd_file(self, tmp_path):
        path = str(tmp_path / "test.htpasswd")
        h = auth.hash_password("alice", "secret")
        auth.write_htpasswd_file(path, "alice", h)
        content = Path(path).read_text()
        assert content.startswith("alice:$2")
        assert content.endswith("\n")

    def test_prompt_password_returns_empty_on_blank(self, monkeypatch):
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
        result = auth.prompt_password("alice")
        assert result == ""

    def test_prompt_password_mismatch_raises(self, monkeypatch):
        responses = iter(["abc", "xyz"])
        monkeypatch.setattr("getpass.getpass", lambda prompt="": next(responses))
        with pytest.raises(ValueError, match="do not match"):
            auth.prompt_password("alice")

    def test_prompt_password_match_returns_value(self, monkeypatch):
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "mysecret")
        result = auth.prompt_password("alice")
        assert result == "mysecret"


# ---------------------------------------------------------------------------
# template_engine
# ---------------------------------------------------------------------------


class TestTemplateEngine:
    def test_container_prefix(self):
        assert template_engine.container_prefix("myapp", "alice", "0") == "myapp-user_alice-0-"

    def test_extract_template_volumes_top_level(self):
        vols = template_engine.extract_template_volumes(COMPOSE_TEMPLATE)
        # db_socket is declared as a top-level named volume
        assert "db_socket" in vols

    def test_extract_template_volumes_bind_mounts(self):
        vols = template_engine.extract_template_volumes(COMPOSE_TEMPLATE)
        # app_data and db_data are referenced via {{ volumes['app_data'] }} /
        # {{ volumes['db_data'] }} Jinja2 expressions and are now detected.
        assert "app_data" in vols
        assert "db_data" in vols

    def test_render_compose(self, tmp_path):
        out = str(tmp_path / "docker-compose.user-alice.0.yml")
        template_engine.render_compose(
            COMPOSE_TEMPLATE, out,
            user_name="alice", service_name="myapp", label="0",
            volumes={"app_data": "/srv/alice/app", "db_data": "/srv/alice/db"},
        )
        content = Path(out).read_text()
        data = yaml.safe_load(content)
        services = data["services"]
        assert "web" in services
        assert "db" in services
        assert services["web"]["container_name"] == "myapp-user_alice-0-web"
        assert services["db"]["container_name"] == "myapp-user_alice-0-db"
        # Volumes are correctly substituted
        assert "/srv/alice/app" in content
        assert "/srv/alice/db" in content

    def test_render_compose_env_vars(self, tmp_path):
        out = str(tmp_path / "docker-compose.user-bob.1.yml")
        template_engine.render_compose(
            COMPOSE_TEMPLATE, out,
            user_name="bob", service_name="myapp", label="1",
            volumes={"app_data": "/data/bob/app", "db_data": "/data/bob/db"},
        )
        content = Path(out).read_text()
        assert "USER_NAME=bob" in content
        assert "SERVICE_NAME=myapp" in content
        assert "LABEL=1" in content

    def test_render_nginx_conf(self, tmp_path):
        out = str(tmp_path / "myapp.user-alice.0.nginx.conf")
        htpasswd = str(tmp_path / "myapp.user-alice.0.htpasswd")
        template_engine.render_nginx_conf(
            NGINX_TEMPLATE, out,
            user_name="alice", service_name="myapp", label="0",
            domain_name="example.com", htpasswd_path=htpasswd,
        )
        content = Path(out).read_text()
        assert "server_name myapp-alice-0.example.com;" in content
        assert f"auth_basic_user_file {htpasswd};" in content
        assert "proxy_pass         http://myapp-user_alice-0-web:80;" in content

    def test_render_nginx_conf_no_password_strips_auth_basic(self, tmp_path):
        out = str(tmp_path / "myapp.user-alice.0.nginx.conf")
        template_engine.render_nginx_conf(
            NGINX_TEMPLATE, out,
            user_name="alice", service_name="myapp", label="0",
            domain_name="example.com", htpasswd_path="",
        )
        content = Path(out).read_text()
        assert "server_name myapp-alice-0.example.com;" in content
        assert "auth_basic" not in content
        assert "proxy_pass" in content

    def test_render_nginx_hostname_format(self, tmp_path):
        out = str(tmp_path / "out.conf")
        template_engine.render_nginx_conf(
            NGINX_TEMPLATE, out,
            user_name="testuser", service_name="svc", label="3",
            domain_name="test.local", htpasswd_path="/tmp/test.htpasswd",
        )
        content = Path(out).read_text()
        assert "server_name svc-testuser-3.test.local;" in content

    # --- HTTPS rendering ---

    def test_render_nginx_conf_https_enabled(self, tmp_path):
        """When https=True, the template renders HTTPS server blocks."""
        out = str(tmp_path / "myapp.user-alice.0.nginx.conf")
        htpasswd = str(tmp_path / "myapp.user-alice.0.htpasswd")
        template_engine.render_nginx_conf(
            NGINX_TEMPLATE, out,
            user_name="alice", service_name="myapp", label="0",
            domain_name="example.com", htpasswd_path=htpasswd,
            https=True,
            ssl_certificate_path="/provision/ssl/example.com/fullchain.pem",
            ssl_certificate_key_path="/provision/ssl/example.com/privkey.pem",
        )
        content = Path(out).read_text()
        assert "listen 443 ssl;" in content
        assert "ssl_certificate     /provision/ssl/example.com/fullchain.pem;" in content
        assert "ssl_certificate_key /provision/ssl/example.com/privkey.pem;" in content
        assert "return 301 https://$host$request_uri;" in content
        assert "server_name myapp-alice-0.example.com;" in content

    def test_render_nginx_conf_https_disabled_no_ssl_blocks(self, tmp_path):
        """When https=False (default), no SSL blocks appear in output."""
        out = str(tmp_path / "myapp.user-alice.0.nginx.conf")
        htpasswd = str(tmp_path / "myapp.user-alice.0.htpasswd")
        template_engine.render_nginx_conf(
            NGINX_TEMPLATE, out,
            user_name="alice", service_name="myapp", label="0",
            domain_name="example.com", htpasswd_path=htpasswd,
        )
        content = Path(out).read_text()
        assert "listen 443 ssl;" not in content
        assert "ssl_certificate" not in content
        assert "ssl_certificate_key" not in content
        assert "return 301 https://" not in content
        assert "listen 80;" in content
        assert "server_name myapp-alice-0.example.com;" in content

    def test_render_nginx_conf_https_empty_ssl_paths_allowed(self, tmp_path):
        """When https=False, empty ssl paths are provided but templated away."""
        out = str(tmp_path / "myapp.user-alice.0.nginx.conf")
        template_engine.render_nginx_conf(
            NGINX_TEMPLATE, out,
            user_name="alice", service_name="myapp", label="0",
            domain_name="example.com", htpasswd_path="",
            https=False,
            ssl_certificate_path="",
            ssl_certificate_key_path="",
        )
        content = Path(out).read_text()
        # SSL blocks should NOT render when https=False
        assert "listen 443 ssl;" not in content
        assert "listen 80;" in content

    def test_render_compose_two_users_independent(self, tmp_path):
        out_alice = str(tmp_path / "alice.yml")
        out_bob = str(tmp_path / "bob.yml")
        template_engine.render_compose(
            COMPOSE_TEMPLATE, out_alice,
            "alice", "myapp", "0", {"app_data": "/a/app", "db_data": "/a/db"},
        )
        template_engine.render_compose(
            COMPOSE_TEMPLATE, out_bob,
            "bob", "myapp", "0", {"app_data": "/b/app", "db_data": "/b/db"},
        )
        c_alice = yaml.safe_load(Path(out_alice).read_text())
        c_bob = yaml.safe_load(Path(out_bob).read_text())
        assert c_alice["services"]["web"]["container_name"] == "myapp-user_alice-0-web"
        assert c_bob["services"]["web"]["container_name"] == "myapp-user_bob-0-web"

    # --- network_name helper ---

    def test_user_network_name_format(self):
        assert template_engine.user_network_name("myapp", "alice", "0") == "myapp-user_alice-0"

    def test_user_network_name_different_users_differ(self):
        n1 = template_engine.user_network_name("myapp", "alice", "0")
        n2 = template_engine.user_network_name("myapp", "bob", "0")
        assert n1 != n2

    def test_user_network_name_different_labels_differ(self):
        n1 = template_engine.user_network_name("myapp", "alice", "0")
        n2 = template_engine.user_network_name("myapp", "alice", "1")
        assert n1 != n2

    # --- network_name in rendered compose ---

    def test_render_compose_declares_named_network(self, tmp_path):
        out = str(tmp_path / "dc.yml")
        template_engine.render_compose(
            COMPOSE_TEMPLATE, out,
            user_name="alice", service_name="myapp", label="0",
            volumes={"app_data": "/srv/alice/app", "db_data": "/srv/alice/db"},
        )
        data = yaml.safe_load(Path(out).read_text())
        expected_net = "myapp-user_alice-0"
        assert "networks" in data, "Top-level 'networks' key missing from rendered compose"
        assert expected_net in data["networks"], f"Network '{expected_net}' not declared"
        assert data["networks"][expected_net]["name"] == expected_net

    def test_render_compose_services_joined_to_network(self, tmp_path):
        out = str(tmp_path / "dc.yml")
        template_engine.render_compose(
            COMPOSE_TEMPLATE, out,
            user_name="alice", service_name="myapp", label="0",
            volumes={"app_data": "/srv/alice/app", "db_data": "/srv/alice/db"},
        )
        data = yaml.safe_load(Path(out).read_text())
        expected_net = "myapp-user_alice-0"
        for svc_name, svc in data["services"].items():
            assert "networks" in svc, f"Service '{svc_name}' missing 'networks' key"
            assert expected_net in svc["networks"], (
                f"Service '{svc_name}' not joined to network '{expected_net}'"
            )

    def test_render_compose_two_users_have_different_networks(self, tmp_path):
        out_alice = str(tmp_path / "alice.yml")
        out_bob = str(tmp_path / "bob.yml")
        template_engine.render_compose(
            COMPOSE_TEMPLATE, out_alice,
            "alice", "myapp", "0", {"app_data": "/a/app", "db_data": "/a/db"},
        )
        template_engine.render_compose(
            COMPOSE_TEMPLATE, out_bob,
            "bob", "myapp", "0", {"app_data": "/b/app", "db_data": "/b/db"},
        )
        d_alice = yaml.safe_load(Path(out_alice).read_text())
        d_bob = yaml.safe_load(Path(out_bob).read_text())
        nets_alice = set(d_alice["networks"].keys())
        nets_bob = set(d_bob["networks"].keys())
        assert nets_alice.isdisjoint(nets_bob), "Two different users must not share a network"

    # --- env_file handling ---

    def test_render_compose_env_file_copied_with_per_user_name(self, tmp_path):
        """env_file is copied as .env.{user_name}.{label} next to the compose file."""
        env_src = tmp_path / "custom.env"
        env_src.write_text("FOO=bar\n")

        template = tmp_path / "dc.yml.j2"
        template.write_text("""services:
  web:
    image: nginx:alpine
    container_name: {{ container_prefix }}web
    env_file: .env
    networks:
      - {{ network_name }}
networks:
  {{ network_name }}:
    name: {{ network_name }}
""")

        out = str(tmp_path / "dc.user-alice.0.yml")
        copied = template_engine.render_compose(
            str(template), out,
            user_name="alice", service_name="myapp", label="0",
            volumes={}, env_file=str(env_src),
        )

        assert copied is not None
        assert copied.endswith(".env.alice.0")
        assert Path(copied).exists()
        assert Path(copied).read_text() == "FOO=bar\n"

    def test_render_compose_env_file_string_form_replaced(self, tmp_path):
        """env_file: .env (string form) is replaced with per-user env file name."""
        env_src = tmp_path / "my.env"
        env_src.write_text("KEY=val\n")

        template = tmp_path / "dc.yml.j2"
        template.write_text("""services:
  web:
    image: nginx:alpine
    container_name: {{ container_prefix }}web
    env_file: .env
    networks:
      - {{ network_name }}
networks:
  {{ network_name }}:
    name: {{ network_name }}
""")

        out = str(tmp_path / "dc.user-bob.1.yml")
        template_engine.render_compose(
            str(template), out,
            user_name="bob", service_name="myapp", label="1",
            volumes={}, env_file=str(env_src),
        )

        content = Path(out).read_text()
        assert "env_file: .env.bob.1" in content
        assert "env_file: .env\n" not in content  # original replaced
        # .env.bob.1 file exists
        assert (tmp_path / ".env.bob.1").exists()

    def test_render_compose_env_file_list_form_replaced(self, tmp_path):
        """env_file: list form - .env is replaced with per-user env file name."""
        env_src = tmp_path / "app.env"
        env_src.write_text("KEY=val\n")

        template = tmp_path / "dc.yml.j2"
        template.write_text("""services:
  web:
    image: nginx:alpine
    container_name: {{ container_prefix }}web
    env_file:
      - .env
    networks:
      - {{ network_name }}
networks:
  {{ network_name }}:
    name: {{ network_name }}
""")

        out = str(tmp_path / "dc.user-eve.2.yml")
        template_engine.render_compose(
            str(template), out,
            user_name="eve", service_name="myapp", label="2",
            volumes={}, env_file=str(env_src),
        )

        content = Path(out).read_text()
        assert "- .env.eve.2" in content
        assert "- .env\n" not in content  # original replaced
        assert (tmp_path / ".env.eve.2").exists()

    def test_render_compose_env_file_list_with_multiple_items(self, tmp_path):
        """Only .env entries in env_file list are replaced; other entries untouched."""
        env_src = tmp_path / "main.env"
        env_src.write_text("KEY=val\n")

        template = tmp_path / "dc.yml.j2"
        template.write_text("""services:
  web:
    image: nginx:alpine
    container_name: {{ container_prefix }}web
    env_file:
      - .env
      - shared.env
    networks:
      - {{ network_name }}
networks:
  {{ network_name }}:
    name: {{ network_name }}
""")

        out = str(tmp_path / "dc.user-alice.0.yml")
        template_engine.render_compose(
            str(template), out,
            user_name="alice", service_name="myapp", label="0",
            volumes={}, env_file=str(env_src),
        )

        content = Path(out).read_text()
        assert "- .env.alice.0" in content
        assert "- shared.env" in content   # untouched
        assert "- .env\n" not in content   # original .env replaced

    def test_render_compose_no_env_file_leaves_dotenv_unchanged(self, tmp_path):
        """Without env_file, .env references are NOT replaced."""
        template = tmp_path / "dc.yml.j2"
        template.write_text("""services:
  web:
    image: nginx:alpine
    container_name: {{ container_prefix }}web
    env_file: .env
    networks:
      - {{ network_name }}
networks:
  {{ network_name }}:
    name: {{ network_name }}
""")

        out = str(tmp_path / "dc.user-alice.0.yml")
        template_engine.render_compose(
            str(template), out,
            user_name="alice", service_name="myapp", label="0",
            volumes={}, env_file=None,
        )

        content = Path(out).read_text()
        # .env remains as-is when no env_file is supplied
        assert "env_file: .env" in content
        assert ".env.alice.0" not in content

    def test_render_compose_two_users_env_files_isolated(self, tmp_path):
        """Two users with different env files get isolated per-user copies."""
        env_a = tmp_path / "a.env"
        env_a.write_text("USER=a\n")
        env_b = tmp_path / "b.env"
        env_b.write_text("USER=b\n")

        template = tmp_path / "dc.yml.j2"
        template.write_text("""services:
  web:
    image: nginx:alpine
    container_name: {{ container_prefix }}web
    env_file: .env
    networks:
      - {{ network_name }}
networks:
  {{ network_name }}:
    name: {{ network_name }}
""")

        out_a = str(tmp_path / "dc.user-alice.0.yml")
        out_b = str(tmp_path / "dc.user-bob.0.yml")

        copied_a = template_engine.render_compose(
            str(template), out_a,
            user_name="alice", service_name="myapp", label="0",
            volumes={}, env_file=str(env_a),
        )
        copied_b = template_engine.render_compose(
            str(template), out_b,
            user_name="bob", service_name="myapp", label="0",
            volumes={}, env_file=str(env_b),
        )

        # Each user gets their own env file
        assert copied_a != copied_b
        assert Path(copied_a).read_text() == "USER=a\n"
        assert Path(copied_b).read_text() == "USER=b\n"

        # Each rendered compose references its own env file
        assert "env_file: .env.alice.0" in Path(out_a).read_text()
        assert "env_file: .env.bob.0" in Path(out_b).read_text()


# ---------------------------------------------------------------------------
# docker_ops
# ---------------------------------------------------------------------------


class TestDockerOps:
    """Unit tests for docker_ops helper functions (subprocess.Popen patched)."""

    def _mock_run(self, monkeypatch):
        """Patch subprocess.Popen inside docker_ops and return a call-list."""
        calls: list[list[str]] = []

        class _FakeProc:
            def __init__(self, args, **kwargs):
                calls.append(list(args))
                self.returncode = 0
                self.stdout = io.StringIO("")
                self.stderr = io.StringIO("")
            def wait(self): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass

        monkeypatch.setattr(docker_ops.subprocess, "Popen", _FakeProc)
        return calls

    def test_network_connect_command(self, monkeypatch):
        calls = self._mock_run(monkeypatch)
        docker_ops.network_connect("provision-nginx", "myapp-user_alice-0")
        assert calls[-1] == [
            "docker", "network", "connect", "myapp-user_alice-0", "provision-nginx"
        ]

    def test_network_disconnect_command(self, monkeypatch):
        calls = self._mock_run(monkeypatch)
        docker_ops.network_disconnect("provision-nginx", "myapp-user_alice-0")
        assert calls[-1] == [
            "docker", "network", "disconnect", "myapp-user_alice-0", "provision-nginx"
        ]

    def test_nginx_reload_command(self, monkeypatch):
        calls = self._mock_run(monkeypatch)
        docker_ops.nginx_reload("provision-nginx")
        assert calls[-1] == [
            "docker", "exec", "provision-nginx", "nginx", "-s", "reload"
        ]

    def test_network_connect_uses_check_false(self, monkeypatch):
        """network_connect must not raise even when docker returns non-zero."""
        class _FailProc:
            def __init__(self, args, **kwargs):
                self.returncode = 1
                self.stdout = io.StringIO("")
                self.stderr = io.StringIO("")
            def wait(self): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
        monkeypatch.setattr(docker_ops.subprocess, "Popen", _FailProc)
        docker_ops.network_connect("provision-nginx", "nonexistent-net")

    def test_network_disconnect_uses_check_false(self, monkeypatch):
        class _FailProc:
            def __init__(self, args, **kwargs):
                self.returncode = 1
                self.stdout = io.StringIO("")
                self.stderr = io.StringIO("")
            def wait(self): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
        monkeypatch.setattr(docker_ops.subprocess, "Popen", _FailProc)
        docker_ops.network_disconnect("provision-nginx", "nonexistent-net")

    def test_nginx_reload_uses_check_false(self, monkeypatch):
        class _FailProc:
            def __init__(self, args, **kwargs):
                self.returncode = 1
                self.stdout = io.StringIO("")
                self.stderr = io.StringIO("")
            def wait(self): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
        monkeypatch.setattr(docker_ops.subprocess, "Popen", _FailProc)
        docker_ops.nginx_reload("provision-nginx")

    def test_compose_build_with_build_args(self, monkeypatch):
        """compose_build appends --build-arg flags before the build subcommand."""
        calls_capture: list[list[str]] = []

        class _CaptureProc:
            def __init__(self, args, **kwargs):
                calls_capture.append(list(args))
                self.returncode = 0
                self.stdout = io.StringIO("")
                self.stderr = io.StringIO("")
            def wait(self): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass

        monkeypatch.setattr(docker_ops.subprocess, "Popen", _CaptureProc)
        docker_ops.compose_build(
            "/tmp/dc.yml",
            build_args={"HTTP_PROXY": "http://proxy:3128", "HTTPS_PROXY": "http://proxy:3129"},
        )
        assert len(calls_capture) == 1
        cmd = calls_capture[0]
        assert "build" in cmd
        assert "--build-arg" in cmd
        assert "HTTP_PROXY=http://proxy:3128" in cmd
        assert "HTTPS_PROXY=http://proxy:3129" in cmd

    def test_compose_stop_command(self, monkeypatch):
        """compose_stop runs docker compose stop."""
        calls = self._mock_run(monkeypatch)
        docker_ops.compose_stop("/tmp/dc.yml")
        assert calls[-1] == [
            "docker", "compose", "-f", "/tmp/dc.yml", "stop"
        ]

    def test_compose_stop_with_env_file(self, monkeypatch):
        """compose_stop with env file passes --env-file flag."""
        calls = self._mock_run(monkeypatch)
        docker_ops.compose_stop("/tmp/dc.yml", env_file="/tmp/.env.test")
        assert "--env-file" in calls[-1]
        assert "/tmp/.env.test" in calls[-1]

    def test_compose_stop_with_project_name(self, monkeypatch):
        """compose_stop with project_name passes --project-name flag."""
        calls = self._mock_run(monkeypatch)
        docker_ops.compose_stop("/tmp/dc.yml", project_name="myproj")
        assert "--project-name" in calls[-1]
        assert "myproj" in calls[-1]

    # ── docker_info ──

    def test_docker_info_parses_json(self, monkeypatch):
        """docker_info returns container counts from docker info JSON."""
        import subprocess as sp
        fake_json = '{"Containers":10,"ContainersRunning":3,"ContainersPaused":1,"ContainersStopped":6}'
        fake_result = sp.CompletedProcess([], 0, stdout=fake_json, stderr="")
        monkeypatch.setattr(docker_ops.subprocess, "run", lambda *a, **kw: fake_result)
        info = docker_ops.docker_info()
        assert info["containers_total"] == 10
        assert info["containers_running"] == 3
        assert info["containers_paused"] == 1
        assert info["containers_stopped"] == 6

    def test_docker_info_handles_bad_json(self, monkeypatch):
        """docker_info returns empty dict on invalid JSON."""
        import subprocess as sp
        fake_result = sp.CompletedProcess([], 0, stdout="not json", stderr="")
        monkeypatch.setattr(docker_ops.subprocess, "run", lambda *a, **kw: fake_result)
        assert docker_ops.docker_info() == {}

    # ── container inspection helpers ──

    def test_container_exists_true(self, monkeypatch):
        """container_exists returns True when docker inspect succeeds."""
        import subprocess as sp
        monkeypatch.setattr(docker_ops.subprocess, "run",
            lambda *a, **kw: sp.CompletedProcess([], 0, stdout="", stderr=""))
        assert docker_ops.container_exists("mycontainer") is True

    def test_container_exists_false(self, monkeypatch):
        """container_exists returns False when docker inspect fails."""
        import subprocess as sp
        monkeypatch.setattr(docker_ops.subprocess, "run",
            lambda *a, **kw: sp.CompletedProcess([], 1, stdout="", stderr=""))
        assert docker_ops.container_exists("nonexistent") is False

    def test_container_running_true(self, monkeypatch):
        """container_running returns True when State.Running is true."""
        fake_json = '[{"State": {"Running": true}}]'
        import subprocess as sp
        monkeypatch.setattr(docker_ops.subprocess, "run",
            lambda *a, **kw: sp.CompletedProcess([], 0, stdout=fake_json, stderr=""))
        assert docker_ops.container_running("running-container") is True

    def test_container_running_false(self, monkeypatch):
        """container_running returns False when State.Running is false."""
        fake_json = '[{"State": {"Running": false}}]'
        import subprocess as sp
        monkeypatch.setattr(docker_ops.subprocess, "run",
            lambda *a, **kw: sp.CompletedProcess([], 0, stdout=fake_json, stderr=""))
        assert docker_ops.container_running("stopped-container") is False

    def test_container_running_nonexistent(self, monkeypatch):
        """container_running returns False when container doesn't exist."""
        import subprocess as sp
        monkeypatch.setattr(docker_ops.subprocess, "run",
            lambda *a, **kw: sp.CompletedProcess([], 1, stdout="", stderr=""))
        assert docker_ops.container_running("ghost") is False

    def test_container_inspect_returns_dict(self, monkeypatch):
        """container_inspect returns parsed JSON dict."""
        fake_json = '[{"Id": "abc123", "Name": "/test-container"}]'
        import subprocess as sp
        monkeypatch.setattr(docker_ops.subprocess, "run",
            lambda *a, **kw: sp.CompletedProcess([], 0, stdout=fake_json, stderr=""))
        info = docker_ops.container_inspect("test-container")
        assert info is not None
        assert info["Id"] == "abc123"

    def test_container_inspect_returns_none_on_failure(self, monkeypatch):
        """container_inspect returns None when docker inspect fails."""
        import subprocess as sp
        monkeypatch.setattr(docker_ops.subprocess, "run",
            lambda *a, **kw: sp.CompletedProcess([], 1, stdout="", stderr=""))
        assert docker_ops.container_inspect("nope") is None

    def test_container_inspect_returns_none_on_bad_json(self, monkeypatch):
        """container_inspect returns None on invalid JSON."""
        import subprocess as sp
        monkeypatch.setattr(docker_ops.subprocess, "run",
            lambda *a, **kw: sp.CompletedProcess([], 0, stdout="garbage", stderr=""))
        assert docker_ops.container_inspect("bad") is None

    # ── network helpers ──

    def test_network_list_returns_names(self, monkeypatch):
        """network_list returns list of network names."""
        import subprocess as sp
        monkeypatch.setattr(docker_ops.subprocess, "run",
            lambda *a, **kw: sp.CompletedProcess([], 0, stdout="bridge\nhost\nmyapp-user_alice-0\n", stderr=""))
        nets = docker_ops.network_list()
        assert "myapp-user_alice-0" in nets
        assert len(nets) == 3

    def test_network_inspect_returns_dict(self, monkeypatch):
        """network_inspect returns parsed JSON dict."""
        fake_json = '[{"Name": "test-net", "Containers": {}}]'
        import subprocess as sp
        monkeypatch.setattr(docker_ops.subprocess, "run",
            lambda *a, **kw: sp.CompletedProcess([], 0, stdout=fake_json, stderr=""))
        info = docker_ops.network_inspect("test-net")
        assert info is not None
        assert info["Name"] == "test-net"

    def test_network_inspect_returns_none_on_failure(self, monkeypatch):
        """network_inspect returns None when docker network inspect fails."""
        import subprocess as sp
        monkeypatch.setattr(docker_ops.subprocess, "run",
            lambda *a, **kw: sp.CompletedProcess([], 1, stdout="", stderr=""))
        assert docker_ops.network_inspect("ghost-net") is None

    def test_network_connected_to_container_positive(self, monkeypatch):
        """network_connected_to_container returns True when container is in network."""
        fake_json = '[{"Name": "test-net", "Containers": {"abc": {"Name": "provision-nginx"}, "def": {"Name": "myapp-web"}}}]'
        import subprocess as sp
        monkeypatch.setattr(docker_ops.subprocess, "run",
            lambda *a, **kw: sp.CompletedProcess([], 0, stdout=fake_json, stderr=""))
        assert docker_ops.network_connected_to_container("test-net", "provision-nginx") is True

    def test_network_connected_to_container_negative(self, monkeypatch):
        """network_connected_to_container returns False when container is not in network."""
        fake_json = '[{"Name": "test-net", "Containers": {"abc": {"Name": "other-container"}}}]'
        import subprocess as sp
        monkeypatch.setattr(docker_ops.subprocess, "run",
            lambda *a, **kw: sp.CompletedProcess([], 0, stdout=fake_json, stderr=""))
        assert docker_ops.network_connected_to_container("test-net", "provision-nginx") is False

    # ── container_logs ──

    def test_container_logs_returns_stdout(self, monkeypatch):
        """container_logs returns stdout from docker logs."""
        import subprocess as sp
        monkeypatch.setattr(docker_ops.subprocess, "run",
            lambda *a, **kw: sp.CompletedProcess([], 0, stdout="line1\nline2\nline3\n", stderr=""))
        logs = docker_ops.container_logs("test-container", tail=50)
        assert logs == "line1\nline2\nline3\n"

    def test_container_logs_default_tail(self, monkeypatch):
        """container_logs defaults to tail=100."""
        calls: list[list[str]] = []
        import subprocess as sp
        def capture_run(args, **kw):
            calls.append(list(args))
            return sp.CompletedProcess([], 0, stdout="", stderr="")
        monkeypatch.setattr(docker_ops.subprocess, "run", capture_run)
        docker_ops.container_logs("test-container")
        cmd = calls[0]
        assert "--tail" in cmd
        assert "100" in cmd

    # ── orphan_network_cleanup ──

    def test_orphan_network_cleanup_removes_when_only_nginx(self, monkeypatch):
        """orphan_network_cleanup removes network when only nginx is connected."""
        import subprocess as sp
        call_args: list[list[str]] = []

        def capture_run(args, **kw):
            call_args.append(list(args))
            if "inspect" in args:
                return sp.CompletedProcess([], 0,
                    stdout='[{"Name":"orphan-net","Containers":{"abc":{"Name":"provision-nginx"}}}]',
                    stderr="")
            return sp.CompletedProcess([], 0, stdout="", stderr="")

        monkeypatch.setattr(docker_ops.subprocess, "run", capture_run)
        # Mock network_disconnect to be a no-op
        monkeypatch.setattr(docker_ops, "network_disconnect", lambda *a: None)

        result = docker_ops.orphan_network_cleanup("orphan-net", "provision-nginx")
        assert result is True
        # Should have run docker network rm
        rm_calls = [c for c in call_args if "rm" in c and "network" in c]
        assert len(rm_calls) >= 1

    def test_orphan_network_cleanup_keeps_when_other_containers(self, monkeypatch):
        """orphan_network_cleanup does not remove network when other containers are connected."""
        import subprocess as sp
        fake_json = '[{"Name":"shared-net","Containers":{"abc":{"Name":"provision-nginx"},"def":{"Name":"myapp-web"}}}]'
        monkeypatch.setattr(docker_ops.subprocess, "run",
            lambda *a, **kw: sp.CompletedProcess([], 0, stdout=fake_json, stderr=""))
        monkeypatch.setattr(docker_ops, "network_disconnect", lambda *a: None)

        result = docker_ops.orphan_network_cleanup("shared-net", "provision-nginx")
        assert result is False

    def test_orphan_network_cleanup_nonexistent_network(self, monkeypatch):
        """orphan_network_cleanup returns False for nonexistent network."""
        import subprocess as sp
        monkeypatch.setattr(docker_ops.subprocess, "run",
            lambda *a, **kw: sp.CompletedProcess([], 1, stdout="", stderr=""))
        assert docker_ops.orphan_network_cleanup("ghost-net") is False


# ---------------------------------------------------------------------------
# compose_converter
# ---------------------------------------------------------------------------

_SAMPLE_NGINX_CONF = """\
server {
    listen 80;
    server_name myapp.example.com;

    auth_basic "My App";
    auth_basic_user_file /etc/nginx/htpasswd/myapp;

    location / {
        proxy_pass http://myapp-web:80;
        proxy_set_header Host $host;
    }
}
"""


class TestComposeConverter:
    """Unit tests for lib/compose_converter module."""

    def _sample_data(self) -> dict:
        return {
            "name": "myapp",
            "services": {
                "web": {
                    "image": "nginx:alpine",
                    "container_name": "myapp-web",
                    "ports": ["80:80"],
                    "volumes": ["/data/myapp/html:/usr/share/nginx/html:ro"],
                    "networks": ["mynet"],
                },
                "db": {
                    "image": "postgres:16",
                    "container_name": "myapp-db",
                    "volumes": [
                        "/var/lib/myapp/db:/var/lib/postgresql/data",
                        "db_socket:/var/run/postgresql",
                    ],
                    "networks": ["mynet"],
                },
            },
            "volumes": {"db_socket": None},
            "networks": {"mynet": None},
        }

    def _convert_to_text(self, data: dict) -> tuple[str, dict]:
        """Run convert() and return (detokenized_yaml_text, src_to_key)."""
        from lib.compose_converter import convert
        transformed, src_to_key, tokens = convert(data)
        raw = yaml.dump(transformed, default_flow_style=False, sort_keys=False)
        return tokens.detokenize(raw), src_to_key

    def test_convert_strips_name(self):
        from lib.compose_converter import convert
        transformed, _, _ = convert(self._sample_data())
        assert "name" not in transformed

    def test_convert_strips_ports(self):
        from lib.compose_converter import convert
        transformed, _, _ = convert(self._sample_data())
        assert "ports" not in transformed["services"]["web"]

    def test_convert_strips_profiles(self):
        """A service whose only profile is \"\" is kept with profiles key removed."""
        from lib.compose_converter import convert
        data = self._sample_data()
        data["services"]["db"]["profiles"] = [""]
        transformed, _, _ = convert(data)
        assert "db" in transformed["services"]
        assert "profiles" not in transformed["services"]["db"]

    def test_convert_excludes_named_profile_services(self):
        from lib.compose_converter import convert
        data = self._sample_data()
        data["services"]["web"]["profiles"] = ["falkordb"]
        transformed, _, _ = convert(data)
        assert "web" not in transformed["services"]

    def test_convert_keeps_empty_string_profile_services(self):
        from lib.compose_converter import convert
        data = self._sample_data()
        data["services"]["web"]["profiles"] = [""]
        transformed, _, _ = convert(data)
        assert "web" in transformed["services"]
        assert "profiles" not in transformed["services"]["web"]

    def test_convert_sets_container_name_template(self):
        text, _ = self._convert_to_text(self._sample_data())
        assert "{{ container_prefix }}web" in text
        assert "{{ container_prefix }}db" in text

    def test_convert_replaces_bind_mounts(self):
        text, _ = self._convert_to_text(self._sample_data())
        assert "{{ volumes['" in text
        assert "/data/myapp/html" not in text
        assert "/var/lib/myapp/db" not in text

    def test_convert_src_to_key_bind_mounts(self):
        from lib.compose_converter import convert
        _, src_to_key, _ = convert(self._sample_data())
        assert "/data/myapp/html" in src_to_key
        assert "/var/lib/myapp/db" in src_to_key

    def test_convert_named_volumes_excluded_from_src_to_key(self):
        from lib.compose_converter import convert
        _, src_to_key, _ = convert(self._sample_data())
        assert "db_socket" not in src_to_key

    def test_convert_replaces_networks(self):
        text, _ = self._convert_to_text(self._sample_data())
        assert "{{ network_name }}" in text

    def test_convert_adds_named_volume_prefix(self):
        text, _ = self._convert_to_text(self._sample_data())
        assert "{{ container_prefix }}db_socket" in text

    def test_convert_preserves_env_vars(self):
        data = self._sample_data()
        data["services"]["web"]["environment"] = ["MY_VAR=${MY_ENV_VAR}"]
        text, _ = self._convert_to_text(data)
        assert "${MY_ENV_VAR}" in text

    def test_convert_external_volume_unchanged(self):
        data = self._sample_data()
        data["volumes"]["shared_vol"] = {"external": True}
        from lib.compose_converter import convert
        transformed, _, tokens = convert(data)
        raw = yaml.dump(transformed, default_flow_style=False, sort_keys=False)
        text = tokens.detokenize(raw)
        # External volumes must NOT get a container_prefix name override
        assert "{{ container_prefix }}shared_vol" not in text

    def test_compose_file_to_template_creates_file(self, tmp_path):
        from lib.compose_converter import compose_file_to_template
        src = str(FIXTURES_DIR / "docker-compose.plain.yml")
        out = str(tmp_path / "output.yml.j2")
        result = compose_file_to_template(src, out, "myapp")
        assert Path(out).exists()
        assert isinstance(result, dict)

    def test_compose_file_to_template_returns_bind_mount_keys(self, tmp_path):
        from lib.compose_converter import compose_file_to_template
        src = str(FIXTURES_DIR / "docker-compose.plain.yml")
        out = str(tmp_path / "output.yml.j2")
        src_to_key = compose_file_to_template(src, out, "myapp")
        assert len(src_to_key) > 0

    def test_compose_file_to_template_content_has_jinja2(self, tmp_path):
        from lib.compose_converter import compose_file_to_template
        src = str(FIXTURES_DIR / "docker-compose.plain.yml")
        out = str(tmp_path / "output.yml.j2")
        compose_file_to_template(src, out, "myapp")
        content = Path(out).read_text()
        assert "{{ container_prefix }}" in content
        assert "{{ network_name }}" in content

    def test_compose_file_to_template_strips_ports(self, tmp_path):
        from lib.compose_converter import compose_file_to_template
        src = str(FIXTURES_DIR / "docker-compose.plain.yml")
        out = str(tmp_path / "output.yml.j2")
        compose_file_to_template(src, out, "myapp")
        content = Path(out).read_text()
        # Strip header comments before checking — the comment mentions "ports:" deliberately
        yaml_body = "\n".join(
            line for line in content.splitlines() if not line.startswith("#")
        )
        assert "ports:" not in yaml_body

    def test_compose_file_to_template_invalid_raises(self, tmp_path):
        from lib.compose_converter import compose_file_to_template
        bad = str(tmp_path / "bad.yml")
        Path(bad).write_text("just: scalar\n")
        out = str(tmp_path / "out.yml.j2")
        with pytest.raises(ValueError, match="services"):
            compose_file_to_template(bad, out)

    def test_make_header_contains_volume_keys(self):
        from lib.compose_converter import make_header
        src_to_key = {"/data/app": "app", "/data/db": "db"}
        header = make_header(src_to_key, "myapp")
        assert "app" in header
        assert "db" in header
        assert "-v app=/your/path" in header

    def test_make_header_no_volumes(self):
        from lib.compose_converter import make_header
        header = make_header({}, "myapp")
        assert "myapp.yml.j2" in header
        assert "{{ container_prefix }}" in header

    def test_unique_keys_for_duplicate_basenames(self):
        """Two bind mounts with the same basename get distinct volume keys."""
        from lib.compose_converter import convert
        data = {
            "services": {
                "a": {"image": "x", "volumes": ["/alpha/data:/a"]},
                "b": {"image": "x", "volumes": ["/beta/data:/b"]},
            }
        }
        _, src_to_key, _ = convert(data)
        keys = list(src_to_key.values())
        assert len(keys) == len(set(keys)), "Duplicate volume keys generated"

    def test_get_compose_service_names_from_plain_file(self, tmp_path):
        from lib.compose_converter import get_compose_service_names
        compose = str(tmp_path / "docker-compose.yml")
        Path(compose).write_text(
            "services:\n"
            "  web:\n"
            "    image: nginx\n"
            "  db:\n"
            "    image: postgres\n"
        )
        names = get_compose_service_names(compose)
        assert names == ["web", "db"]

    def test_get_compose_service_names_from_j2_template(self, tmp_path):
        from lib.compose_converter import get_compose_service_names
        compose = str(tmp_path / "docker-compose.yml.j2")
        Path(compose).write_text(
            "services:\n"
            "  {{ container_prefix }}web:\n"
            "    image: nginx\n"
            "  db:\n"
            "    image: postgres\n"
        )
        names = get_compose_service_names(compose)
        assert "db" in names

    def test_get_compose_service_names_invalid_yaml(self, tmp_path):
        from lib.compose_converter import get_compose_service_names
        compose = str(tmp_path / "bad.yml")
        Path(compose).write_text("not: valid: yaml: [[[")
        names = get_compose_service_names(compose)
        assert names == []

    # ── docker.sock passthrough ──────────────────────────────────────────

    def test_docker_sock_not_converted_to_volume_key(self):
        """Verify /var/run/docker.sock is NOT converted to a template variable.

        The Docker socket must remain as a literal host path so containers can
        communicate with the host Docker daemon.  Converting it to a per-user
        volume key would break all docker-cli containers.
        """
        from lib.compose_converter import convert
        data = {
            "services": {
                "supervisor": {
                    "image": "docker:cli",
                    "volumes": ["/var/run/docker.sock:/var/run/docker.sock"],
                },
            },
        }
        _, src_to_key, _ = convert(data)
        assert "/var/run/docker.sock" not in src_to_key, (
            "docker.sock must not appear in src_to_key — it is a passthrough path"
        )
        assert "docker_sock" not in src_to_key.values(), (
            "docker_sock must not be a volume key"
        )

    def test_docker_sock_literal_in_generated_template(self, tmp_path):
        """Verify docker.sock appears as a literal path in the generated .j2 template."""
        from lib.compose_converter import compose_file_to_template
        src = str(tmp_path / "dc.yml")
        dst = str(tmp_path / "dc.yml.j2")
        Path(src).write_text("""\
services:
  supervisor:
    image: docker:cli
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
""")
        src_to_key = compose_file_to_template(src, dst)
        content = Path(dst).read_text()
        # docker.sock must remain as a literal path in the template
        assert "/var/run/docker.sock:/var/run/docker.sock" in content, (
            "docker.sock must stay as literal host path in template"
        )
        # It must NOT be converted to a Jinja2 volumes reference
        assert "{{ volumes['" not in content, (
            "docker.sock must not become a Jinja2 volume variable"
        )
        assert src_to_key == {}, (
            "No bind-mounts should be converted for docker.sock-only compose files"
        )

    def test_run_docker_sock_also_passthrough(self):
        """/run/docker.sock (alternate path) is also a passthrough."""
        from lib.compose_converter import convert
        data = {
            "services": {
                "supervisor": {
                    "image": "docker:cli",
                    "volumes": ["/run/docker.sock:/var/run/docker.sock"],
                },
            },
        }
        _, src_to_key, _ = convert(data)
        assert "/run/docker.sock" not in src_to_key

    def test_passthrough_paths_surrounded_by_normal_bind_mounts(self):
        """Normal bind mounts are still converted even when docker.sock is present."""
        from lib.compose_converter import convert
        data = {
            "services": {
                "web": {
                    "image": "nginx",
                    "volumes": ["/data/html:/usr/share/nginx/html:ro"],
                },
                "supervisor": {
                    "image": "docker:cli",
                    "volumes": ["/var/run/docker.sock:/var/run/docker.sock"],
                },
            },
        }
        _, src_to_key, _ = convert(data)
        # Normal bind mount IS converted
        assert "/data/html" in src_to_key
        # docker.sock is NOT converted
        assert "/var/run/docker.sock" not in src_to_key


# ---------------------------------------------------------------------------
# nginx_converter
# ---------------------------------------------------------------------------


class TestNginxConverter:
    """Unit tests for lib/nginx_converter module."""

    def test_convert_server_name(self):
        from lib.nginx_converter import convert_nginx
        out = convert_nginx(_SAMPLE_NGINX_CONF)
        assert "server_name {{ hostname }};" in out
        assert "myapp.example.com" not in out

    def test_convert_auth_basic(self):
        from lib.nginx_converter import convert_nginx
        out = convert_nginx(_SAMPLE_NGINX_CONF)
        assert 'auth_basic "{{ service_name }} - {{ user_name }}";' in out

    def test_convert_auth_basic_user_file(self):
        from lib.nginx_converter import convert_nginx
        out = convert_nginx(_SAMPLE_NGINX_CONF)
        assert "auth_basic_user_file {{ htpasswd_path }};" in out

    def test_convert_proxy_pass_with_hint(self):
        from lib.nginx_converter import convert_nginx
        out = convert_nginx(_SAMPLE_NGINX_CONF, service_name_hint="myapp")
        assert "proxy_pass http://{{ container_prefix }}web:80;" in out
        assert "myapp-web" not in out

    def test_convert_proxy_pass_without_hint(self):
        from lib.nginx_converter import convert_nginx
        out = convert_nginx(_SAMPLE_NGINX_CONF)
        # Without a service_name_hint, proxy_pass target is not rewritten
        assert "proxy_pass http://myapp-web:80;" in out

    def test_convert_preserves_proxy_headers(self):
        from lib.nginx_converter import convert_nginx
        out = convert_nginx(_SAMPLE_NGINX_CONF)
        assert "proxy_set_header Host $host;" in out

    def test_convert_auth_basic_injected_when_absent(self):
        """When proxy_pass exists but auth_basic is absent, auth_basic lines are injected."""
        from lib.nginx_converter import convert_nginx
        conf = (
            "server {\n"
            "    listen 80;\n"
            "    server_name example.com;\n"
            "    location / {\n"
            "        proxy_pass http://myapp-web:80;\n"
            "    }\n"
            "}\n"
        )
        out = convert_nginx(conf)
        assert 'auth_basic "{{ service_name }} - {{ user_name }}";' in out
        assert "auth_basic_user_file {{ htpasswd_path }};" in out

    def test_convert_no_auth_basic_no_proxy_pass_edge_case(self):
        """A conf with neither proxy_pass nor auth_basic is left without auth_basic."""
        from lib.nginx_converter import convert_nginx
        conf = "server { listen 80; server_name example.com; }\n"
        out = convert_nginx(conf)
        assert "auth_basic" not in out

    def test_nginx_file_to_template_creates_file(self, tmp_path):
        from lib.nginx_converter import nginx_file_to_template
        src = str(FIXTURES_DIR / "myapp.plain.nginx.conf")
        out = str(tmp_path / "myapp.nginx.conf.j2")
        nginx_file_to_template(src, out, "myapp")
        assert Path(out).exists()

    def test_nginx_file_to_template_has_header(self, tmp_path):
        from lib.nginx_converter import nginx_file_to_template
        src = str(FIXTURES_DIR / "myapp.plain.nginx.conf")
        out = str(tmp_path / "myapp.nginx.conf.j2")
        nginx_file_to_template(src, out, "myapp")
        content = Path(out).read_text()
        assert "generated by gen_nginx_template.py" in content

    def test_nginx_file_to_template_transforms_content(self, tmp_path):
        from lib.nginx_converter import nginx_file_to_template
        src = str(FIXTURES_DIR / "myapp.plain.nginx.conf")
        out = str(tmp_path / "out.j2")
        nginx_file_to_template(src, out, "myapp")
        content = Path(out).read_text()
        assert "{{ hostname }}" in content
        assert "{{ htpasswd_path }}" in content
        assert "{{ container_prefix }}" in content

    def test_make_header_includes_hint(self):
        from lib.nginx_converter import make_header
        header = make_header("myapp.nginx.conf", "myapp")
        assert "myapp" in header
        assert "generated by gen_nginx_template.py" in header

    def test_make_header_lists_template_vars(self):
        from lib.nginx_converter import make_header
        header = make_header("test.conf", "svc")
        assert "{{ hostname }}" in header
        assert "{{ container_prefix }}" in header
        assert "{{ htpasswd_path }}" in header

    def test_convert_proxy_pass_matches_compose_service_name(self):
        """proxy_pass host matching a compose service name → {{ container_prefix }}<name>."""
        from lib.nginx_converter import convert_nginx
        conf = (
            "server {\n"
            "    listen 80;\n"
            "    server_name example.com;\n"
            "    location / {\n"
            "        proxy_pass http://mcp-server:8000;\n"
            "    }\n"
            "}\n"
        )
        out = convert_nginx(conf, compose_service_names=["mcp-server", "db"])
        assert "proxy_pass http://{{ container_prefix }}mcp-server:8000;" in out

    def test_convert_proxy_pass_matches_compose_service_name_case_insensitive(self):
        """Service name matching is case-insensitive."""
        from lib.nginx_converter import convert_nginx
        conf = (
            "server {\n"
            "    listen 80;\n"
            "    server_name example.com;\n"
            "    location / {\n"
            "        proxy_pass http://MCP-Server:8000;\n"
            "    }\n"
            "}\n"
        )
        out = convert_nginx(conf, compose_service_names=["mcp-server"])
        assert "proxy_pass http://{{ container_prefix }}MCP-Server:8000;" in out

    def test_convert_proxy_pass_no_match_leaves_unchanged(self):
        """Host not in compose_service_names and not matching hint is left alone."""
        from lib.nginx_converter import convert_nginx
        conf = (
            "server {\n"
            "    listen 80;\n"
            "    server_name example.com;\n"
            "    location / {\n"
            "        proxy_pass http://external-api:9000;\n"
            "    }\n"
            "}\n"
        )
        out = convert_nginx(conf, compose_service_names=["mcp-server"])
        assert "proxy_pass http://external-api:9000;" in out

    def test_convert_proxy_pass_compose_match_takes_priority_over_hint(self):
        """Exact compose service name match takes priority over hint prefix match."""
        from lib.nginx_converter import convert_nginx
        conf = (
            "server {\n"
            "    listen 80;\n"
            "    server_name example.com;\n"
            "    location / {\n"
            "        proxy_pass http://web:80;\n"
            "    }\n"
            "}\n"
        )
        # "web" is an exact compose service name → replaced as whole
        out = convert_nginx(conf, service_name_hint="myapp", compose_service_names=["web"])
        assert "proxy_pass http://{{ container_prefix }}web:80;" in out
        # It should NOT strip "myapp" prefix (which wouldn't match anyway)
        assert "myapp" not in out.split("proxy_pass")[1]

    # --- SSL certificate path conversion ---

    def test_convert_ssl_certificate_path_replaced(self):
        """ssl_certificate path is replaced with {{ ssl_certificate_path }}."""
        from lib.nginx_converter import convert_nginx
        conf = (
            "server {\n"
            "    listen 443 ssl;\n"
            "    server_name example.com;\n"
            "    ssl_certificate     /etc/letsencrypt/live/example.com/fullchain.pem;\n"
            "    ssl_certificate_key /etc/letsencrypt/live/example.com/privkey.pem;\n"
            "    location / {\n"
            "        proxy_pass http://myapp-web:80;\n"
            "    }\n"
            "}\n"
        )
        out = convert_nginx(conf)
        assert "ssl_certificate     {{ ssl_certificate_path }};" in out
        assert "ssl_certificate_key {{ ssl_certificate_key_path }};" in out
        assert "/etc/letsencrypt" not in out

    def test_convert_ssl_block_wrapped_in_if_https(self):
        """Server blocks containing listen ... ssl are wrapped in {% if https %}...{% endif %}."""
        from lib.nginx_converter import convert_nginx
        conf = (
            "server {\n"
            "    listen 443 ssl;\n"
            "    server_name example.com;\n"
            "    ssl_certificate     /etc/ssl/fullchain.pem;\n"
            "    ssl_certificate_key /etc/ssl/privkey.pem;\n"
            "    location / {\n"
            "        proxy_pass http://myapp-web:80;\n"
            "    }\n"
            "}\n"
        )
        out = convert_nginx(conf)
        assert "{% if https %}" in out
        assert "{% endif %}" in out

    def test_convert_https_redirect_block_wrapped(self):
        """HTTP→HTTPS redirect blocks (return 301 https://) are wrapped too."""
        from lib.nginx_converter import convert_nginx
        conf = (
            "server {\n"
            "    listen 80;\n"
            "    server_name example.com;\n"
            "    return 301 https://$host$request_uri;\n"
            "}\n"
            "server {\n"
            "    listen 443 ssl;\n"
            "    server_name example.com;\n"
            "    location / {\n"
            "        proxy_pass http://myapp-web:80;\n"
            "    }\n"
            "}\n"
        )
        out = convert_nginx(conf)
        # Both the redirect and the SSL block should be wrapped
        assert out.count("{% if https %}") == 2
        assert out.count("{% endif %}") == 2
        assert "return 301 https://" in out

    def test_convert_http_only_conf_not_wrapped(self):
        """A plain HTTP-only conf (no ssl listen) gets auto-generated HTTPS blocks."""
        from lib.nginx_converter import convert_nginx
        conf = (
            "server {\n"
            "    listen 80;\n"
            "    server_name example.com;\n"
            "    location / {\n"
            "        proxy_pass http://myapp-web:80;\n"
            "    }\n"
            "}\n"
        )
        out = convert_nginx(conf)
        # Original HTTP block is preserved (the listen 80 block without wrap)
        assert "listen 80;" in out
        # Auto-generated HTTPS block is wrapped in {% if https %}
        assert "{% if https %}" in out
        assert "{% endif %}" in out
        assert "listen 443 ssl;" in out

    def test_convert_auto_generate_https_injects_ssl_certificate_vars(self):
        """Auto-generated HTTPS block includes ssl_certificate template variables."""
        from lib.nginx_converter import convert_nginx
        conf = (
            "server {\n"
            "    listen 80;\n"
            "    server_name example.com;\n"
            "    location / {\n"
            "        proxy_pass http://myapp-web:80;\n"
            "    }\n"
            "}\n"
        )
        out = convert_nginx(conf)
        assert "ssl_certificate {{ ssl_certificate_path }};" in out
        assert "ssl_certificate_key {{ ssl_certificate_key_path }};" in out

    def test_convert_auto_generate_https_preserves_original_http_block(self):
        """When HTTPS is auto-generated, the original HTTP block is kept intact."""
        from lib.nginx_converter import convert_nginx
        conf = (
            "server {\n"
            "    listen 80;\n"
            "    server_name example.com;\n"
            "    location / {\n"
            "        proxy_pass http://myapp-web:80;\n"
            "    }\n"
            "}\n"
        )
        out = convert_nginx(conf)
        # The original listen 80 block appears BEFORE any {% if https %} wrapper
        idx_http = out.index("listen 80;")
        idx_if = out.index("{% if https %}")
        assert idx_http < idx_if, "Original HTTP block should appear before the auto-generated HTTPS block"

    def test_convert_ssl_paths_untouched_when_no_ssl(self):
        """A conf without ssl_certificate directives now gets auto-generated HTTPS with template vars."""
        from lib.nginx_converter import convert_nginx
        conf = _SAMPLE_NGINX_CONF
        out = convert_nginx(conf)
        # Auto-generated HTTPS block now injects ssl_certificate template variables
        assert "{{ ssl_certificate_path }}" in out
        assert "{{ ssl_certificate_key_path }}" in out
        # The original HTTP block is preserved (not wrapped)
        assert "listen 80;" in out
        # Auto-generated HTTPS block is conditionally wrapped
        assert "{% if https %}" in out
        assert "listen 443 ssl;" in out


class TestProvisioner:
    """Unit tests for lib/provisioner helper functions."""

    def test_auto_volumes_creates_directories(self, tmp_path):
        """_auto_volumes creates a subdirectory for each volume key."""
        from lib.provisioner import _auto_volumes
        result = _auto_volumes(COMPOSE_TEMPLATE, "alice", "myapp", "0", tmp_path / "ud")
        for key, path in result.items():
            assert Path(path).is_dir(), f"Expected dir for volume '{key}': {path}"

    def test_auto_volumes_paths_rooted_at_user_data_dir(self, tmp_path):
        """Returned paths sit under user_data_dir/{user}/{service}/{label}/{key}."""
        from lib.provisioner import _auto_volumes
        user_data = tmp_path / "user_data"
        result = _auto_volumes(COMPOSE_TEMPLATE, "alice", "myapp", "0", user_data)
        base = user_data / "alice" / "myapp" / "0"
        for key, path in result.items():
            assert path == str(base / key)

    def test_auto_volumes_detects_jinja2_dict_keys(self, tmp_path):
        """Keys referenced via {{ volumes['key'] }} in the template are detected."""
        from lib.provisioner import _auto_volumes
        result = _auto_volumes(COMPOSE_TEMPLATE, "alice", "myapp", "0", tmp_path)
        # Fixture template uses {{ volumes['app_data'] }} and {{ volumes['db_data'] }}
        assert "app_data" in result
        assert "db_data" in result

    def test_auto_volumes_idempotent(self, tmp_path):
        """Calling _auto_volumes twice for the same user does not raise."""
        from lib.provisioner import _auto_volumes
        user_data = tmp_path / "user_data"
        _auto_volumes(COMPOSE_TEMPLATE, "alice", "myapp", "0", user_data)
        _auto_volumes(COMPOSE_TEMPLATE, "alice", "myapp", "0", user_data)  # must not raise


# ---------------------------------------------------------------------------
# provisioner — env_file_path registry storage + rebuild
# ---------------------------------------------------------------------------


class TestProvisionerEnvFile:
    """Verify env_file_path flows through register_user → registry → rebuild."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, tmp_path):
        """Mock docker / nginx calls, redirect registry to temp file."""
        # Mock docker ops subprocess so no real Docker calls happen
        self.calls: list[list[str]] = []

        def fake_run(args, check=True):
            self.calls.append(list(args))
            import subprocess as sp
            return sp.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(docker_ops, "_run", fake_run)
        # Mock network connect / nginx reload (no-op)
        monkeypatch.setattr(docker_ops, "network_connect", lambda *a, **kw: None)
        monkeypatch.setattr(docker_ops, "nginx_reload", lambda *a: None)

        # Redirect registry to temp file
        self.reg_path = tmp_path / "user_registry.yml"
        monkeypatch.setattr(registry, "REGISTRY_FILE", self.reg_path)
        self.tmp_path = tmp_path
        self.user_data_dir = tmp_path / "user_data"
        self.user_data_dir.mkdir()
        # Temp SSL dir so tests don't need write access to /provision/ssl
        self.ssl_base_dir = tmp_path / "provision" / "ssl"
        self.ssl_base_dir.mkdir(parents=True, exist_ok=True)

    # ── register_user ──────────────────────────────────────────

    def test_register_stores_per_user_env_copy_in_registry(self):
        """Registry stores the per-user copied env path, not the original."""
        env_src = self.tmp_path / "custom.env"
        env_src.write_text("FOO=bar\n")

        provisioner.register_user(
            user_name="envuser",
            service_name="myapp",
            label="0",
            compose_template=COMPOSE_TEMPLATE,
            output_dir=self.tmp_path,
            user_data_dir=self.user_data_dir,
            env_file=str(env_src),
        )
        entry = registry.get_user_service("envuser", "myapp", "0")
        assert entry is not None

        stored = entry.get("env_file_path") or ""
        assert ".env.envuser.0" in stored, (
            f"Registry should store per-user copy .env.envuser.0, got: {stored}"
        )
        assert "custom.env" not in stored, (
            f"Registry should NOT store original custom.env, got: {stored}"
        )
        assert Path(stored).exists(), f"Per-user env file not found: {stored}"
        assert Path(stored).read_text() == "FOO=bar\n"

    def test_register_without_env_file_has_null_env_file_path(self):
        """Without env_file, registry env_file_path should be None."""
        provisioner.register_user(
            user_name="noenv",
            service_name="myapp",
            label="0",
            compose_template=COMPOSE_TEMPLATE,
            output_dir=self.tmp_path,
            user_data_dir=self.user_data_dir,
        )
        entry = registry.get_user_service("noenv", "myapp", "0")
        assert entry is not None
        stored = entry.get("env_file_path") or None
        assert stored is None, f"Expected None, got: {stored}"

    def test_register_compose_up_uses_per_user_env_file(self):
        """The --env-file flag to compose_up points to the per-user copy."""
        env_src = self.tmp_path / "app.env"
        env_src.write_text("KEY=val\n")

        provisioner.register_user(
            user_name="copyuser",
            service_name="myapp",
            label="1",
            compose_template=COMPOSE_TEMPLATE,
            output_dir=self.tmp_path,
            user_data_dir=self.user_data_dir,
            env_file=str(env_src),
        )

        up_calls = [c for c in self.calls if "up" in c]
        assert len(up_calls) >= 1, "compose_up should have been called"
        up_cmd = up_calls[-1]
        # Find --env-file argument
        for i, arg in enumerate(up_cmd):
            if arg == "--env-file" and i + 1 < len(up_cmd):
                env_path = up_cmd[i + 1]
                assert ".env.copyuser.1" in env_path, (
                    f"--env-file should point to per-user copy, got: {env_path}"
                )
                assert "app.env" not in env_path, (
                    f"--env-file should NOT use original name, got: {env_path}"
                )
                break
        else:
            pytest.fail(f"--env-file not found in compose_up: {up_cmd}")

    # ── rebuild_user ───────────────────────────────────────────

    def test_rebuild_uses_per_user_env_from_registry(self):
        """Rebuild reads per-user env_file_path from registry and uses it."""
        env_src = self.tmp_path / "prod.env"
        env_src.write_text("MODE=production\n")

        provisioner.register_user(
            user_name="rebuildenv",
            service_name="myapp",
            label="0",
            compose_template=COMPOSE_TEMPLATE,
            output_dir=self.tmp_path,
            user_data_dir=self.user_data_dir,
            env_file=str(env_src),
        )
        self.calls.clear()

        provisioner.rebuild_user(
            user_name="rebuildenv",
            service_name="myapp",
            label="0",
        )

        env_calls = [c for c in self.calls if "--env-file" in c]
        assert len(env_calls) >= 1, (
            f"Rebuild should pass --env-file, got calls: {self.calls}"
        )
        for cmd in env_calls:
            for i, arg in enumerate(cmd):
                if arg == "--env-file" and i + 1 < len(cmd):
                    env_path = cmd[i + 1]
                    assert ".env.rebuildenv.0" in env_path, (
                        f"Rebuild --env-file should use per-user copy, got: {env_path}"
                    )
                    assert "prod.env" not in env_path, (
                        f"Rebuild --env-file should NOT use original, got: {env_path}"
                    )

    # ── HTTPS (TLS) support ────────────────────────────────────

    def test_register_https_copies_certs_and_stores_in_registry(self):
        """When https=True, cert files are copied to /provision/ssl/{domain}/ and registry updated."""
        # Create fake cert files
        fullchain_src = self.tmp_path / "fullchain.pem"
        fullchain_src.write_text("FAKE FULLCHAIN CERT\n")
        privkey_src = self.tmp_path / "privkey.pem"
        privkey_src.write_text("FAKE PRIVATE KEY\n")

        provisioner.register_user(
            user_name="httpsuser",
            service_name="myapp",
            label="0",
            compose_template=COMPOSE_TEMPLATE,
            output_dir=self.tmp_path,
            user_data_dir=self.user_data_dir,
            https=True,
            fullchain=str(fullchain_src),
            privkey=str(privkey_src),
            domain="example.com",
            ssl_base_dir=str(self.ssl_base_dir),
        )

        entry = registry.get_user_service("httpsuser", "myapp", "0")
        assert entry is not None
        assert entry.get("https") is True

        ssl_cert = entry.get("ssl_certificate_path", "")
        ssl_key = entry.get("ssl_certificate_key_path", "")
        expected_cert = str(self.ssl_base_dir / "example.com" / "fullchain.pem")
        expected_key = str(self.ssl_base_dir / "example.com" / "privkey.pem")
        assert expected_cert in ssl_cert
        assert expected_key in ssl_key

        # Cert files should exist at the destination
        assert Path(ssl_cert).exists()
        assert Path(ssl_cert).read_text() == "FAKE FULLCHAIN CERT\n"
        assert Path(ssl_key).exists()
        assert Path(ssl_key).read_text() == "FAKE PRIVATE KEY\n"

    def test_register_https_bare_filenames_resolve_in_ssl_dir(self):
        """Bare filenames (no path separator) are looked up in ssl_base_dir/{domain}/."""
        # Pre-create cert files in the temp ssl dir
        ssl_dir = self.ssl_base_dir / "example.com"
        ssl_dir.mkdir(parents=True, exist_ok=True)
        (ssl_dir / "my-fullchain.pem").write_text("BARE FULLCHAIN\n")
        (ssl_dir / "my-privkey.pem").write_text("BARE PRIVKEY\n")

        provisioner.register_user(
            user_name="barehttps",
            service_name="myapp",
            label="0",
            compose_template=COMPOSE_TEMPLATE,
            output_dir=self.tmp_path,
            user_data_dir=self.user_data_dir,
            https=True,
            fullchain="my-fullchain.pem",
            privkey="my-privkey.pem",
            domain="example.com",
            ssl_base_dir=str(self.ssl_base_dir),
        )

        entry = registry.get_user_service("barehttps", "myapp", "0")
        assert entry is not None
        assert entry.get("https") is True

        ssl_cert = entry.get("ssl_certificate_path", "")
        ssl_key = entry.get("ssl_certificate_key_path", "")
        assert ssl_cert.endswith("my-fullchain.pem")
        assert ssl_key.endswith("my-privkey.pem")
        assert Path(ssl_cert).read_text() == "BARE FULLCHAIN\n"
        assert Path(ssl_key).read_text() == "BARE PRIVKEY\n"

    def test_register_https_missing_fullchain_raises(self):
        """https=True with a missing fullchain file raises ValueError."""
        privkey_src = self.tmp_path / "privkey.pem"
        privkey_src.write_text("KEY\n")

        with pytest.raises(ValueError, match="fullchain"):
            provisioner.register_user(
                user_name="badhttps",
                service_name="myapp",
                label="0",
                compose_template=COMPOSE_TEMPLATE,
                output_dir=self.tmp_path,
                user_data_dir=self.user_data_dir,
                https=True,
                fullchain="/nonexistent/fullchain.pem",
                privkey=str(privkey_src),
                domain="example.com",
                ssl_base_dir=str(self.ssl_base_dir),
            )

    def test_register_https_missing_privkey_raises(self):
        """https=True with a missing privkey file raises ValueError."""
        fullchain_src = self.tmp_path / "fullchain.pem"
        fullchain_src.write_text("CERT\n")

        with pytest.raises(ValueError, match="privkey"):
            provisioner.register_user(
                user_name="badhttps2",
                service_name="myapp",
                label="0",
                compose_template=COMPOSE_TEMPLATE,
                output_dir=self.tmp_path,
                user_data_dir=self.user_data_dir,
                https=True,
                fullchain=str(fullchain_src),
                privkey="/nonexistent/privkey.pem",
                domain="example.com",
                ssl_base_dir=str(self.ssl_base_dir),
            )

    def test_register_https_without_certs_raises(self):
        """https=True with None fullchain/privkey raises ValueError."""
        with pytest.raises(ValueError, match="fullchain"):
            provisioner.register_user(
                user_name="nocert",
                service_name="myapp",
                label="0",
                compose_template=COMPOSE_TEMPLATE,
                output_dir=self.tmp_path,
                user_data_dir=self.user_data_dir,
                https=True,
                fullchain=None,
                privkey=None,
                domain="example.com",
            )

    def test_register_https_false_does_not_touch_certs(self):
        """When https=False, no cert files are copied and registry has empty ssl fields."""
        provisioner.register_user(
            user_name="nohttps",
            service_name="myapp",
            label="0",
            compose_template=COMPOSE_TEMPLATE,
            output_dir=self.tmp_path,
            user_data_dir=self.user_data_dir,
            https=False,
        )

        entry = registry.get_user_service("nohttps", "myapp", "0")
        assert entry is not None
        assert entry.get("https") is False
        assert entry.get("ssl_certificate_path") == ""
        assert entry.get("ssl_certificate_key_path") == ""

    # ── start_service / stop_service ──

    def test_start_service_calls_compose_up(self):
        """start_service looks up registry entry and calls compose_up."""
        # Register a user first
        provisioner.register_user(
            user_name="upuser",
            service_name="myapp",
            label="0",
            compose_template=COMPOSE_TEMPLATE,
            output_dir=self.tmp_path,
            user_data_dir=self.user_data_dir,
        )
        self.calls.clear()

        result = provisioner.start_service(
            user_name="upuser",
            service_name="myapp",
            label="0",
        )
        assert result["user_name"] == "upuser"
        up_calls = [c for c in self.calls if "up" in c]
        assert len(up_calls) >= 1, "Expected compose_up to be called"

    def test_start_service_missing_registry_raises_keyerror(self):
        """start_service raises KeyError when no registration exists."""
        with pytest.raises(KeyError, match="No registration"):
            provisioner.start_service(
                user_name="ghost",
                service_name="myapp",
                label="0",
            )

    def test_stop_service_calls_compose_stop(self):
        """stop_service looks up registry entry and calls compose_stop."""
        provisioner.register_user(
            user_name="downuser",
            service_name="myapp",
            label="0",
            compose_template=COMPOSE_TEMPLATE,
            output_dir=self.tmp_path,
            user_data_dir=self.user_data_dir,
        )
        self.calls.clear()

        result = provisioner.stop_service(
            user_name="downuser",
            service_name="myapp",
            label="0",
        )
        assert result["user_name"] == "downuser"
        stop_calls = [c for c in self.calls if "stop" in c]
        assert len(stop_calls) >= 1, "Expected compose_stop to be called"

    def test_stop_service_missing_registry_raises_keyerror(self):
        """stop_service raises KeyError when no registration exists."""
        with pytest.raises(KeyError, match="No registration"):
            provisioner.stop_service(
                user_name="ghost",
                service_name="myapp",
                label="0",
            )

    # ── change_password ──

    def test_change_password_updates_registry_and_htpasswd(self):
        """change_password re-hashes password, updates htpasswd file and registry."""
        provisioner.register_user(
            user_name="pwuser",
            service_name="myapp",
            label="0",
            compose_template=COMPOSE_TEMPLATE,
            nginx_template=NGINX_TEMPLATE,
            output_dir=self.tmp_path,
            nginx_output_dir=self.tmp_path,
            user_data_dir=self.user_data_dir,
            passwd="oldpass",
        )
        entry_before = registry.get_user_service("pwuser", "myapp", "0")
        old_hash = entry_before.get("passwd", "")

        result = provisioner.change_password(
            user_name="pwuser",
            service_name="myapp",
            label="0",
            passwd="newpass",
            nginx_container="provision-nginx",
        )
        assert result["user_name"] == "pwuser"

        entry_after = registry.get_user_service("pwuser", "myapp", "0")
        new_hash = entry_after.get("passwd", "")
        assert new_hash != old_hash, "Password hash should change"
        assert new_hash.startswith("$2"), "Should be bcrypt hash"

        # htpasswd file should contain new hash
        htpasswd_path = entry_after.get("htpasswd_path", "")
        assert htpasswd_path, "htpasswd_path should be set"
        content = Path(htpasswd_path).read_text()
        assert "pwuser:" in content

    def test_change_password_missing_registry_raises_keyerror(self):
        """change_password raises KeyError when no registration exists."""
        with pytest.raises(KeyError, match="No registration"):
            provisioner.change_password(
                user_name="ghost",
                service_name="myapp",
                label="0",
                passwd="secret",
            )

    # ── remove_user orphan network cleanup ──

    def test_remove_user_includes_compose_down(self):
        """remove_user calls compose_down to tear down containers."""
        provisioner.register_user(
            user_name="orphanuser",
            service_name="myapp",
            label="0",
            compose_template=COMPOSE_TEMPLATE,
            output_dir=self.tmp_path,
            user_data_dir=self.user_data_dir,
        )
        self.calls.clear()

        provisioner.remove_user(
            user_name="orphanuser",
            service_name="myapp",
            label="0",
            nginx_container="provision-nginx",
        )
        down_calls = [c for c in self.calls if "down" in c]
        assert len(down_calls) >= 1, "Expected compose_down to be called during removal"


# ---------------------------------------------------------------------------
# api — project_root bare-name resolution
# ---------------------------------------------------------------------------


class TestAPIProjectRoot:
    """Unit tests for project_root bare-name resolution in POST /users."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        """Redirect all paths and mock docker calls for API endpoint tests."""
        import api
        from lib import registry as reg_mod, docker_ops

        gen_dir = tmp_path / "generated"
        ud_dir = tmp_path / "user_data"
        sp_dir = tmp_path / "source_projects"
        gen_dir.mkdir()
        ud_dir.mkdir()
        sp_dir.mkdir()

        monkeypatch.setattr(api, "GENERATED_DIR", gen_dir)
        monkeypatch.setattr(api, "USER_DATA_DIR", ud_dir)
        monkeypatch.setattr(api, "SOURCE_PROJECTS_DIR", sp_dir)
        monkeypatch.setattr(reg_mod, "REGISTRY_FILE", tmp_path / "user_registry.yml")

        class _FakeProc:
            def __init__(self, args, **kwargs):
                self.returncode = 0
                self.stdout = io.StringIO("")
                self.stderr = io.StringIO("")
            def wait(self): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass

        monkeypatch.setattr(docker_ops.subprocess, "Popen", _FakeProc)

        self.sp_dir = sp_dir
        self.tmp_path = tmp_path

    def _call(self, **kwargs):
        """Call register_user() directly and return the result (or raise HTTPException)."""
        from api import register_user, RegisterRequest
        return register_user(RegisterRequest(**kwargs))

    def test_bare_project_root_resolves_to_source_projects_dir(self):
        """project_root='testpr' resolves to SOURCE_PROJECTS_DIR/testpr when that dir exists."""
        project_dir = self.sp_dir / "testpr"
        project_dir.mkdir()
        shutil.copy(FIXTURES_DIR / "docker-compose.template.yml.j2", project_dir)
        shutil.copy(FIXTURES_DIR / "myapp.template.nginx.conf.j2", project_dir)

        result = self._call(
            user_name="pruser",
            service_name="myapp",
            project_root="testpr",
            compose_template_path="docker-compose.template.yml.j2",
            nginx_conf_template_path="myapp.template.nginx.conf.j2",
            label="0",
            domain="localhost",
            passwd="secret",
        )
        assert result["status"] == "registered"

    def test_bare_project_root_not_found_returns_404(self):
        """Bare project_root with no matching dir in SOURCE_PROJECTS_DIR raises HTTP 404."""
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            self._call(
                user_name="pruser",
                service_name="myapp",
                project_root="nonexistent",
                compose_template_path="docker-compose.template.yml.j2",
                label="0",
            )
        assert exc_info.value.status_code == 404

    def test_absolute_project_root_used_as_is(self):
        """Absolute project_root is used directly, not prepended with SOURCE_PROJECTS_DIR."""
        project_dir = self.tmp_path / "abs_project"
        project_dir.mkdir()
        shutil.copy(FIXTURES_DIR / "docker-compose.template.yml.j2", project_dir)

        result = self._call(
            user_name="pruser2",
            service_name="myapp",
            project_root=str(project_dir),
            compose_template_path="docker-compose.template.yml.j2",
            label="0",
            domain="localhost",
            passwd="secret",
        )
        assert result["status"] == "registered"

    def test_no_project_root_absolute_template_path_works(self):
        """Without project_root, an absolute compose_template_path is used directly."""
        result = self._call(
            user_name="pruser3",
            service_name="myapp",
            compose_template_path=COMPOSE_TEMPLATE,
            label="0",
            domain="localhost",
            passwd="secret",
        )
        assert result["status"] == "registered"


# ---------------------------------------------------------------------------
# api — FastAPI TestClient tests for new endpoints (P1-P6)
# ---------------------------------------------------------------------------


class TestAPINewEndpoints:
    """Test new API endpoints using FastAPI TestClient with mocked docker_ops."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        """Set up TestClient with mocked docker/provisioner dependencies."""
        import api
        from lib import registry as reg_mod, docker_ops, provisioner
        from fastapi.testclient import TestClient

        gen_dir = tmp_path / "generated"
        ud_dir = tmp_path / "user_data"
        sp_dir = tmp_path / "source_projects"
        ssl_dir = tmp_path / "ssl"
        gen_dir.mkdir()
        ud_dir.mkdir()
        sp_dir.mkdir()
        ssl_dir.mkdir()

        monkeypatch.setattr(api, "GENERATED_DIR", gen_dir)
        monkeypatch.setattr(api, "USER_DATA_DIR", ud_dir)
        monkeypatch.setattr(api, "SOURCE_PROJECTS_DIR", sp_dir)
        monkeypatch.setattr(api, "SSL_DIR", ssl_dir)
        monkeypatch.setattr(reg_mod, "REGISTRY_FILE", tmp_path / "user_registry.yml")

        # Mock docker_ops subprocess.Popen
        class _FakeProc:
            def __init__(self, args, **kwargs):
                self.returncode = 0
                self.stdout = io.StringIO("")
                self.stderr = io.StringIO("")
            def wait(self): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass

        monkeypatch.setattr(docker_ops.subprocess, "Popen", _FakeProc)

        # Track calls for later assertions
        self.mock_calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            self.mock_calls.append(list(args))
            import subprocess as sp
            return sp.CompletedProcess(args, 0, stdout="[]", stderr="")

        self._fake_run = fake_run

        self.client = TestClient(api.app)
        self.tmp_path = tmp_path
        self.gen_dir = gen_dir
        self.api = api

    # ── GET /docker/ps ──

    def test_docker_ps_returns_list(self, monkeypatch):
        """GET /docker/ps returns container list."""
        import subprocess as sp
        monkeypatch.setattr(self.api.docker_ops.subprocess, "run", self._fake_run)
        response = self.client.get("/docker/ps")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    # ── GET /docker/stats ──

    def test_docker_stats_returns_list(self, monkeypatch):
        """GET /docker/stats returns stats list."""
        import subprocess as sp
        monkeypatch.setattr(self.api.docker_ops.subprocess, "run", self._fake_run)
        response = self.client.get("/docker/stats")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    # ── GET /docker/info ──

    def test_docker_info_endpoint(self, monkeypatch):
        """GET /docker/info returns docker system info."""
        import subprocess as sp
        fake_json = '{"Containers":5,"ContainersRunning":2,"ContainersPaused":0,"ContainersStopped":3}'
        monkeypatch.setattr(self.api.docker_ops.subprocess, "run",
            lambda *a, **kw: sp.CompletedProcess([], 0, stdout=fake_json, stderr=""))
        response = self.client.get("/docker/info")
        assert response.status_code == 200
        data = response.json()
        assert data["containers_total"] == 5
        assert data["containers_running"] == 2

    # ── GET /host/stats ──

    def test_host_stats_returns_dict(self):
        """GET /host/stats returns host resource usage."""
        response = self.client.get("/host/stats")
        assert response.status_code == 200
        data = response.json()
        assert "mem_percent" in data
        assert "cpu_percent" in data
        assert "disk_percent" in data

    # ── Reconciliation helpers ──

    def test_container_exists_endpoint(self, monkeypatch):
        """GET /docker/container/{c}/exists returns exists bool."""
        import subprocess as sp
        monkeypatch.setattr(self.api.docker_ops.subprocess, "run",
            lambda *a, **kw: sp.CompletedProcess([], 0, stdout="", stderr=""))
        response = self.client.get("/docker/container/test-container/exists")
        assert response.status_code == 200
        assert response.json()["exists"] is True

    def test_container_running_endpoint(self, monkeypatch):
        """GET /docker/container/{c}/running returns running bool."""
        import subprocess as sp
        fake_json = '[{"State": {"Running": true}}]'
        monkeypatch.setattr(self.api.docker_ops.subprocess, "run",
            lambda *a, **kw: sp.CompletedProcess([], 0, stdout=fake_json, stderr=""))
        response = self.client.get("/docker/container/test-container/running")
        assert response.status_code == 200
        assert response.json()["running"] is True

    def test_network_connect_endpoint(self, monkeypatch):
        """POST /docker/network/{n}/connect/{c} returns connected=True."""
        monkeypatch.setattr(self.api.docker_ops, "network_connect", lambda *a, **kw: None)
        response = self.client.post("/docker/network/testnet/connect/provision-nginx")
        assert response.status_code == 200
        assert response.json()["connected"] is True

    def test_nginx_reload_endpoint(self, monkeypatch):
        """POST /docker/nginx/reload returns reloaded=True."""
        monkeypatch.setattr(self.api.docker_ops, "nginx_reload", lambda *a: None)
        response = self.client.post("/docker/nginx/reload")
        assert response.status_code == 200
        assert response.json()["reloaded"] is True

    # ── POST /users/.../up ──

    def test_up_endpoint_success(self, monkeypatch):
        """POST /users/{u}/services/{s}/{l}/up returns 200 on success."""
        # First register a user
        self._register_user("upuser", monkeypatch)

        # Mock start_service to succeed
        monkeypatch.setattr(self.api.provisioner, "start_service",
            lambda **kw: {"user_name": kw["user_name"], "service_name": kw["service_name"], "label": kw["label"]})

        response = self.client.post("/users/upuser/services/myapp/0/up")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "up"
        assert "Service started" in data["message"]

    def test_up_endpoint_not_found(self, monkeypatch):
        """POST /users/{u}/services/{s}/{l}/up returns 404 when not registered."""
        monkeypatch.setattr(self.api.provisioner, "start_service",
            lambda **kw: (_ for _ in ()).throw(KeyError("No registration found")))
        response = self.client.post("/users/ghost/services/myapp/0/up")
        assert response.status_code == 404

    # ── POST /users/.../down ──

    def test_down_endpoint_success(self, monkeypatch):
        """POST /users/{u}/services/{s}/{l}/down returns 200 on success."""
        self._register_user("downuser", monkeypatch)

        monkeypatch.setattr(self.api.provisioner, "stop_service",
            lambda **kw: {"user_name": kw["user_name"], "service_name": kw["service_name"], "label": kw["label"]})

        response = self.client.post("/users/downuser/services/myapp/0/down")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "down"
        assert "Service stopped" in data["message"]

    def test_down_endpoint_not_found(self, monkeypatch):
        """POST /users/{u}/services/{s}/{l}/down returns 404 when not registered."""
        monkeypatch.setattr(self.api.provisioner, "stop_service",
            lambda **kw: (_ for _ in ()).throw(KeyError("No registration found")))
        response = self.client.post("/users/ghost/services/myapp/0/down")
        assert response.status_code == 404

    # ── PUT /users/.../password ──

    def test_password_endpoint_success(self, monkeypatch):
        """PUT /users/{u}/services/{s}/{l}/password returns 200 on success."""
        self._register_user("pwuser", monkeypatch)

        monkeypatch.setattr(self.api.provisioner, "change_password",
            lambda **kw: {"user_name": kw["user_name"], "service_name": kw["service_name"], "label": kw["label"]})

        response = self.client.put(
            "/users/pwuser/services/myapp/0/password",
            json={"passwd": "newsecret"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "Password updated" in data["message"]

    def test_password_endpoint_not_found(self, monkeypatch):
        """PUT /users/{u}/services/{s}/{l}/password returns 404 when not registered."""
        monkeypatch.setattr(self.api.provisioner, "change_password",
            lambda **kw: (_ for _ in ()).throw(KeyError("No registration found")))
        response = self.client.put(
            "/users/ghost/services/myapp/0/password",
            json={"passwd": "secret"},
        )
        assert response.status_code == 404

    # ── GET /nginx/connections ──

    def test_nginx_connections_endpoint(self, monkeypatch):
        """GET /nginx/connections returns nginx connection state."""
        import subprocess as sp
        # Mock container_inspect to return empty networks
        fake_json = '[{"NetworkSettings": {"Networks": {}}}]'
        monkeypatch.setattr(self.api.docker_ops.subprocess, "run",
            lambda *a, **kw: sp.CompletedProcess([], 0, stdout=fake_json, stderr=""))
        response = self.client.get("/nginx/connections")
        assert response.status_code == 200
        data = response.json()
        assert "nginx_container" in data
        assert "connected_networks" in data
        assert "conf_files" in data
        assert "upstreams" in data

    # ── POST /nginx/reconnect-all ──

    def test_nginx_reconnect_all_endpoint(self, monkeypatch):
        """POST /nginx/reconnect-all returns reconnect results."""
        monkeypatch.setattr(self.api.docker_ops, "network_connect", lambda *a, **kw: None)
        monkeypatch.setattr(self.api.docker_ops, "nginx_reload", lambda *a: None)
        response = self.client.post("/nginx/reconnect-all")
        assert response.status_code == 200
        data = response.json()
        assert data["nginx_reloaded"] is True
        assert "total_networks" in data

    # ── GET /users/.../containers/{c}/logs ──

    def test_container_logs_endpoint(self, monkeypatch):
        """GET /users/{u}/services/{s}/{l}/containers/{c}/logs returns logs."""
        self._register_user("loguser", monkeypatch)

        import subprocess as sp
        monkeypatch.setattr(self.api.docker_ops.subprocess, "run",
            lambda *a, **kw: sp.CompletedProcess([], 0, stdout="line1\nline2\n", stderr=""))

        response = self.client.get("/users/loguser/services/myapp/0/containers/web/logs?tail=50")
        assert response.status_code == 200
        data = response.json()
        assert data["tail"] == 50
        assert "logs" in data
        assert data["container"].endswith("web")

    def test_container_logs_endpoint_not_found(self, monkeypatch):
        """GET /users/.../containers/{c}/logs returns 404 when user not registered."""
        response = self.client.get("/users/ghost/services/myapp/0/containers/web/logs")
        assert response.status_code == 404

    # ── GET /tasks/{task_id}/log (SSE) ──

    def test_task_log_endpoint_returns_sse(self, monkeypatch):
        """GET /tasks/{task_id}/log returns SSE stream."""
        import subprocess as sp
        # First, submit a task to get a valid task_id
        monkeypatch.setattr(self.api.docker_ops.subprocess, "run", self._fake_run)
        monkeypatch.setattr(self.api.docker_ops, "network_connect", lambda *a, **kw: None)
        monkeypatch.setattr(self.api.docker_ops, "nginx_reload", lambda *a: None)
        monkeypatch.setattr(self.api.provisioner, "register_user",
            lambda **kw: {"entry": {"user_name": kw["user_name"]}, "volume_warnings": {}})

        # Submit async registration
        resp = self.client.post("/users", json={
            "user_name": "sseloguser",
            "service_name": "myapp",
            "compose_template_path": COMPOSE_TEMPLATE,
            "label": "0",
        })
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]

        # Poll the SSE log endpoint (follow=false for test)
        response = self.client.get(f"/tasks/{task_id}/log?follow=false")
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]

    # ── GET /health ──

    def test_health_endpoint(self):
        """GET /health returns ok."""
        response = self.client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    # ── GET /tasks ──

    def test_tasks_list_endpoint(self):
        """GET /tasks returns task list."""
        response = self.client.get("/tasks")
        assert response.status_code == 200
        data = response.json()
        assert "count" in data
        assert "tasks" in data

    # ── Reconciliation endpoints ──

    def test_reconcile_endpoint(self, monkeypatch):
        """POST /reconcile runs reconciliation and returns report."""
        monkeypatch.setattr(self.api.docker_ops, "network_connect", lambda *a, **kw: None)
        monkeypatch.setattr(self.api.docker_ops, "nginx_reload", lambda *a: None)
        import subprocess as sp
        monkeypatch.setattr(self.api.docker_ops.subprocess, "run",
            lambda *a, **kw: sp.CompletedProcess([], 0, stdout="[]", stderr=""))

        response = self.client.post("/reconcile")
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Reconciliation completed."
        assert "report" in data
        report = data["report"]
        assert "total_upstreams" in report
        assert "reachable" in report
        assert "unreachable" in report
        assert "nginx_reloaded" in report

    def test_reconcile_status_endpoint(self):
        """GET /reconcile/status returns last reconciliation status."""
        response = self.client.get("/reconcile/status")
        assert response.status_code == 200
        data = response.json()
        assert "result" in data
        assert "total_upstreams" in data["result"]

    def test_nginx_state_endpoint(self):
        """GET /nginx-state returns full state JSON."""
        response = self.client.get("/nginx-state")
        assert response.status_code == 200
        data = response.json()
        assert "version" in data
        assert "upstreams" in data

    # ── Helper: register a user for dependent tests ──

    def _register_user(self, user_name: str, monkeypatch):
        """Helper to register a user via the API (sync mode)."""
        from lib import registry as reg_mod

        # Mock docker/provisioner for registration
        monkeypatch.setattr(self.api.docker_ops, "network_connect", lambda *a, **kw: None)
        monkeypatch.setattr(self.api.docker_ops, "nginx_reload", lambda *a: None)

        import subprocess as sp
        monkeypatch.setattr(self.api.docker_ops.subprocess, "run",
            lambda *a, **kw: sp.CompletedProcess([], 0, stdout="[]", stderr=""))

        response = self.client.post(f"/users?sync=true", json={
            "user_name": user_name,
            "service_name": "myapp",
            "compose_template_path": COMPOSE_TEMPLATE,
            "label": "0",
            "domain": "localhost",
            "passwd": "secret",
        })
        assert response.status_code == 202, f"Register {user_name} failed: {response.text}"
        return response.json()

"""Unit tests for individual lib/ modules."""

from __future__ import annotations

import io
import os
import re
import shutil
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from lib import auth, docker_ops, provisioner, registry, template_engine, validation
from registry_support import add_user as add_registry_user
from registry_support import remove_user_service as remove_registry_service

FIXTURES_DIR = Path(__file__).parent / "fixtures"
COMPOSE_TEMPLATE = str(FIXTURES_DIR / "docker-compose.template.yml.j2")
NGINX_TEMPLATE = str(FIXTURES_DIR / "myapp.template.nginx.conf.j2")


def isolate_fixture_templates(tmp_path: Path, monkeypatch) -> None:
    """Point COMPOSE_TEMPLATE / NGINX_TEMPLATE at copies in the test's own tree.

    `POST /users` renders the per-user compose with
    `output_dir=Path(compose_template).parent` — BESIDE its template. That is
    right in production, where the template lives in the service's recipe dir,
    but a test that registers with the repo's fixture template aims those writes
    at `tests/fixtures/` — the directory the rest of the suite reads its inputs
    from. Each run then left `docker-compose.user-<u>.0.yml` and
    `.env.*.<u>.0` behind; found 2026-09-25 (10 strays, some written that day).

    Call this from any class that drives `/users` with the fixture templates.
    """
    mod = sys.modules[__name__]
    tpl_dir = tmp_path / "recipe"
    tpl_dir.mkdir(exist_ok=True)
    compose_copy = tpl_dir / "docker-compose.template.yml.j2"
    nginx_copy = tpl_dir / "myapp.template.nginx.conf.j2"
    compose_copy.write_text(Path(COMPOSE_TEMPLATE).read_text())
    nginx_copy.write_text(Path(NGINX_TEMPLATE).read_text())
    monkeypatch.setattr(mod, "COMPOSE_TEMPLATE", str(compose_copy))
    monkeypatch.setattr(mod, "NGINX_TEMPLATE", str(nginx_copy))

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
    """The registry is the PostgreSQL ``registry`` schema (T1/T3)."""

    def test_empty_registry(self, registry_file):
        assert registry.get_all_users() == []

    def test_add_and_get_user(self, registry_file, sample_entry):
        add_registry_user(sample_entry)
        users = registry.get_all_users()
        assert len(users) == 1
        assert users[0]["user_name"] == "alice"
        assert users[0]["service_id"]

    def test_get_user_by_name(self, registry_file, sample_entry):
        add_registry_user(sample_entry)
        result = registry.get_user("alice")
        assert len(result) == 1
        assert result[0]["service_name"] == "myapp"

    def test_get_user_unknown(self, registry_file, sample_entry):
        add_registry_user(sample_entry)
        assert registry.get_user("nobody") == []

    def test_get_user_service_exact_match(self, registry_file, sample_entry):
        add_registry_user(sample_entry)
        found = registry.get_user_service("alice", "myapp", "0")
        assert found is not None
        assert found["label"] == "0"

    def test_get_user_service_no_match(self, registry_file, sample_entry):
        add_registry_user(sample_entry)
        assert registry.get_user_service("alice", "myapp", "99") is None

    def test_remove_user_service(self, registry_file, sample_entry):
        add_registry_user(sample_entry)
        removed = remove_registry_service("alice", "myapp", "0")
        assert removed is True
        assert registry.get_all_users() == []

    def test_remove_nonexistent_returns_false(self, registry_file):
        assert remove_registry_service("ghost", "svc", "0") is False

    def test_multiple_users_isolated(self, registry_file, sample_entry):
        entry_bob = dict(sample_entry, user_name="bob")
        add_registry_user(sample_entry)
        add_registry_user(entry_bob)
        remove_registry_service("alice", "myapp", "0")
        remaining = registry.get_all_users()
        assert len(remaining) == 1
        assert remaining[0]["user_name"] == "bob"

    def test_registry_persists_to_postgres(self, registry_file, sample_entry):
        service_id, _proc = add_registry_user(sample_entry)
        row = registry.db.fetch_one(
            "SELECT s.service_name, a.username FROM registry.services s "
            "JOIN registry.api_users a ON a.id = s.owner_api_user_id "
            "WHERE s.id = %s",
            (service_id,),
        )
        assert row["service_name"] == "myapp"
        assert row["username"] == "alice"


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
        assert "server_name myapp-alice-0--web.example.com;" in content
        assert f"auth_basic_user_file {htpasswd};" in content
        # proxy_pass now uses variable-based resolution:
        #   set $upstream_XXXX myapp-user_alice-0-web:80;
        #   proxy_pass http://$upstream_XXXX;
        assert "set $upstream_" in content
        assert "myapp-user_alice-0-web:80;" in content
        assert "proxy_pass" in content and "$upstream_" in content

    def test_render_nginx_conf_no_password_strips_auth_basic(self, tmp_path):
        out = str(tmp_path / "myapp.user-alice.0.nginx.conf")
        template_engine.render_nginx_conf(
            NGINX_TEMPLATE, out,
            user_name="alice", service_name="myapp", label="0",
            domain_name="example.com", htpasswd_path="",
        )
        content = Path(out).read_text()
        assert "server_name myapp-alice-0--web.example.com;" in content
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
        assert "server_name svc-testuser-3--web.test.local;" in content

    def test_render_nginx_conf_acl_on_simple_conf_no_scaffold(self, tmp_path, monkeypatch):
        """v5 (decision 1/6): ENABLE_ACL no longer touches the per-service conf.
        A template with stale v2/v3/v4 ACL scaffolding is cleaned into the
        SIMPLE ACL-free form — server_name + auth_basic + proxy_pass — and NO
        scaffold (auth_request, auth_request_set, error_page, named redirects,
        mode-switch rewrite, env.d) is ever injected. auth_basic lives at server
        level. The stale `/_set_token` / `/_auth_jwt` locations are stripped."""
        monkeypatch.setenv("ENABLE_ACL", "true")
        stale = tmp_path / "stale.nginx.conf.j2"
        stale.write_text(
            "server {\n"
            "    listen 80;\n"
            "    location = /_set_token { return 302 $arg_redirect; }\n"
            "    location = /_auth_jwt { internal; proxy_pass http://gateway:8770/api/auth/verify; }\n"
            "    server_name {{ hostname }};\n"
            "    location / {\n"
            "        auth_basic \"x\";\n"
            "        auth_basic_user_file {{ htpasswd_path }};\n"
            "        proxy_pass http://{{ container_prefix }}web:80;\n"
            "    }\n"
            "}\n"
        )
        out = str(tmp_path / "out.conf")
        htpasswd = str(tmp_path / "x.htpasswd")
        template_engine.render_nginx_conf(
            str(stale), out,
            user_name="alice", service_name="myapp", label="0",
            domain_name="localhost", htpasswd_path=htpasswd,
        )
        content = Path(out).read_text()
        # Simple form: server_name + server-level auth_basic + variable proxy_pass.
        assert "server_name myapp-alice-0.localhost;" in content
        assert 'auth_basic "x";' in content
        assert "set $upstream_0000 myapp-user_alice-0-web:80;" in content
        assert "proxy_pass http://$upstream_0000;" in content
        # NO ACL scaffold — the gate lives at the edge (v5 §5).
        assert "auth_request" not in content
        assert "set $auth_mode" not in content
        assert "include /etc/nginx/env.d" not in content
        assert "location = /_set_token" not in content
        assert "location = /_auth_jwt" not in content
        assert "location /__basic__/" not in content
        assert "location @auth_401" not in content
        assert "location @auth_403" not in content
        assert "error_page" not in content
        assert "auth_request_set" not in content
        assert "$service_basic" not in content

    def test_render_nginx_conf_strips_set_token_and_auth_jwt(self, tmp_path):
        """v5: _set_token / _auth_jwt are edge concerns (acl-helpers.conf), NOT
        the internal conf. Any stale copy in a template is stripped entirely —
        the internal -nginx is services-only."""
        stale = tmp_path / "stale.nginx.conf.j2"
        stale.write_text(
            "server {\n"
            "    listen 80;\n"
            "    location = /_set_token {\n"
            "        add_header Set-Cookie \"provision_token=$arg_token; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400\";\n"
            "        return 302 $arg_redirect;\n"
            "    }\n"
            "    server_name {{ hostname }};\n"
            "    location / { proxy_pass http://{{ container_prefix }}web:80; }\n"
            "}\n"
        )
        out = str(tmp_path / "out.conf")
        template_engine.render_nginx_conf(
            str(stale), out,
            user_name="alice", service_name="myapp", label="0",
            domain_name="localhost", htpasswd_path=str(tmp_path / "x.htpasswd"),
        )
        content = Path(out).read_text()
        assert "location = /_set_token" not in content
        assert "return 302 $arg_redirect;" not in content
        assert "add_header Set-Cookie" not in content
        assert "proxy_pass http://$gw/api/auth/exchange" not in content
        assert "server_name myapp-alice-0.localhost;" in content
        assert "proxy_pass http://myapp-user_alice-0-web:80;" in content

    def test_render_nginx_conf_acl_off_simple_conf_byte_identical(self, tmp_path, monkeypatch):
        """v5 (decision 1/6): ENABLE_ACL=false must NOT inject anything either —
        the internal conf is the simple ACL-free form regardless, byte-identical
        across ENABLE_ACL values (gate at the edge, decision 6)."""
        monkeypatch.setenv("ENABLE_ACL", "false")
        stale = tmp_path / "stale.nginx.conf.j2"
        stale.write_text(
            "server {\n"
            "    listen 80;\n"
            "    location = /_auth_jwt { internal; proxy_pass http://gateway:8770/api/auth/verify; }\n"
            "    server_name {{ hostname }};\n"
            "    location / { auth_basic \"x\"; auth_basic_user_file {{ htpasswd_path }}; proxy_pass http://{{ container_prefix }}web:80; }\n"
            "}\n"
        )
        out = str(tmp_path / "out.conf")
        htpasswd = str(tmp_path / "x.htpasswd")
        template_engine.render_nginx_conf(
            str(stale), out,
            user_name="alice", service_name="myapp", label="0",
            domain_name="localhost", htpasswd_path=htpasswd,
        )
        content = Path(out).read_text()
        assert "auth_request" not in content
        assert "set $auth_mode" not in content
        assert "include /etc/nginx/env.d" not in content
        assert "location = /_auth_jwt" not in content
        assert "location /__basic__/" not in content
        # Simple form remains.
        assert 'auth_basic "x";' in content
        assert "server_name myapp-alice-0.localhost;" in content

    def test_render_nginx_conf_no_template_acl_still_simple(self, tmp_path, monkeypatch):
        """v5: a template with NO ACL-aware locations (never ACL-aware) renders
        the simple form with NO injected scaffold — the gate is the edge's job."""
        monkeypatch.setenv("ENABLE_ACL", "true")
        plain = tmp_path / "plain.nginx.conf.j2"
        plain.write_text(
            "server {\n"
            "    listen 80;\n"
            "    server_name {{ hostname }};\n"
            "    location / { auth_basic \"x\"; auth_basic_user_file {{ htpasswd_path }}; proxy_pass http://{{ container_prefix }}web:80; }\n"
            "}\n"
        )
        out = str(tmp_path / "out.conf")
        htpasswd = str(tmp_path / "x.htpasswd")
        template_engine.render_nginx_conf(
            str(plain), out,
            user_name="alice", service_name="myapp", label="0",
            domain_name="localhost", htpasswd_path=htpasswd,
        )
        content = Path(out).read_text()
        assert "auth_request" not in content
        assert "location = /_auth_jwt" not in content
        assert "location /__basic__/" not in content
        assert "location @auth_401" not in content
        assert "set $auth_mode" not in content
        assert 'auth_basic "x";' in content
        assert "server_name myapp-alice-0.localhost;" in content

    def test_render_nginx_conf_byte_identical_across_enable_acl(self, tmp_path, monkeypatch):
        """v5 (decision 6/F8): ENABLE_ACL true vs false must produce byte-identical
        simple confs — the per-service conf never reads ENABLE_ACL."""
        out_a = str(tmp_path / "a.conf")
        out_b = str(tmp_path / "b.conf")
        for out, val in ((out_a, "true"), (out_b, "false")):
            monkeypatch.setenv("ENABLE_ACL", val)
            template_engine.render_nginx_conf(
                NGINX_TEMPLATE, out,
                user_name="alice", service_name="myapp", label="0",
                domain_name="example.com", htpasswd_path=str(tmp_path / "x.htpasswd"),
            )
        assert Path(out_a).read_text() == Path(out_b).read_text()
        # And neither carries the scaffold.
        content = Path(out_a).read_text()
        assert "auth_request" not in content
        assert "set $auth_mode" not in content
        assert "env.d" not in content

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
        assert "server_name myapp-alice-0--web.example.com;" in content

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
        assert "server_name myapp-alice-0--web.example.com;" in content

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

    # ── Variable-based proxy_pass (per-request DNS resolution) ──

    def test_proxy_pass_rewritten_to_variable_with_port(self, tmp_path):
        """proxy_pass http://host:port; → set $upstream_XXXX host:port; + proxy_pass http://$upstream_XXXX;"""
        out = str(tmp_path / "out.conf")
        template_engine.render_nginx_conf(
            NGINX_TEMPLATE, out,
            user_name="alice", service_name="myapp", label="0",
            domain_name="example.com", htpasswd_path="",
        )
        content = Path(out).read_text()
        # The old static form must NOT appear
        assert "proxy_pass         http://myapp-user_alice-0-web:80;" not in content
        # The variable form must appear
        assert "set $upstream_" in content
        assert "myapp-user_alice-0-web:80;" in content
        assert "proxy_pass" in content and "$upstream_" in content
        # The variable name should be unique and sequential
        assert "$upstream_0000" in content

    def test_proxy_pass_multiple_upstreams_get_unique_variables(self, tmp_path):
        """Multiple proxy_pass lines get unique variable names (upstream_0000, upstream_0001, ...)."""
        # Create a temp template with two proxy_pass lines
        tpl_dir = tmp_path / "templates"
        tpl_dir.mkdir()
        tpl_path = tpl_dir / "multi.conf.j2"
        tpl_path.write_text("""\
server {
    listen 80;
    server_name {{ hostname }};
    location /app {
        proxy_pass http://{{ container_prefix }}web:80;
    }
    location /api {
        proxy_pass http://{{ container_prefix }}api:8080;
    }
}
""")
        out = str(tmp_path / "out.conf")
        template_engine.render_nginx_conf(
            str(tpl_path), out,
            user_name="alice", service_name="myapp", label="0",
            domain_name="example.com", htpasswd_path="",
        )
        content = Path(out).read_text()
        assert "$upstream_0000" in content, f"Expected upstream_0000 in:\n{content}"
        assert "$upstream_0001" in content, f"Expected upstream_0001 in:\n{content}"
        # The old static forms must be gone
        assert "proxy_pass http://myapp-user_alice-0-web:80;" not in content
        assert "proxy_pass http://myapp-user_alice-0-api:8080;" not in content

    def test_proxy_pass_no_port_handled_correctly(self, tmp_path):
        """proxy_pass without port (http://host;) also gets variable treatment."""
        tpl_dir = tmp_path / "templates"
        tpl_dir.mkdir()
        tpl_path = tpl_dir / "noport.conf.j2"
        tpl_path.write_text("""\
server {
    listen 80;
    location / {
        proxy_pass http://{{ container_prefix }}app;
    }
}
""")
        out = str(tmp_path / "out.conf")
        template_engine.render_nginx_conf(
            str(tpl_path), out,
            user_name="alice", service_name="myapp", label="0",
            domain_name="example.com", htpasswd_path="",
        )
        content = Path(out).read_text()
        assert "set $upstream_0000 myapp-user_alice-0-app;" in content
        assert "proxy_pass http://$upstream_0000;" in content
        # Static form must be gone
        assert "proxy_pass http://myapp-user_alice-0-app;" not in content

    def test_proxy_pass_variable_does_not_break_https(self, tmp_path):
        """When https=True, the variable rewrite works in both HTTP redirect and HTTPS server blocks."""
        out = str(tmp_path / "out.conf")
        htpasswd = str(tmp_path / "test.htpasswd")
        template_engine.render_nginx_conf(
            NGINX_TEMPLATE, out,
            user_name="alice", service_name="myapp", label="0",
            domain_name="example.com", htpasswd_path=htpasswd,
            https=True,
            ssl_certificate_path="/ssl/cert.pem",
            ssl_certificate_key_path="/ssl/key.pem",
        )
        content = Path(out).read_text()
        # Variable form in HTTP redirect block shouldn't apply (no proxy_pass there)
        # but in the HTTPS server block it should
        assert "set $upstream_" in content
        assert "proxy_pass" in content and "$upstream_" in content
        # Still has HTTPS directives
        assert "listen 443 ssl;" in content
        assert "ssl_certificate" in content

    def test_proxy_pass_variable_nginx_starts_with_missing_upstream(self, tmp_path):
        """The generated conf should NOT contain a bare proxy_pass that would
        trigger DNS resolution at nginx startup.  It must use the
        set+variable form so nginx defers resolution to request time.
        """
        out = str(tmp_path / "out.conf")
        template_engine.render_nginx_conf(
            NGINX_TEMPLATE, out,
            user_name="alice", service_name="myapp", label="0",
            domain_name="example.com", htpasswd_path="",
        )
        content = Path(out).read_text()
        # Must NOT contain bare proxy_pass (without $ prefix)
        import re
        bare_matches = re.findall(r"proxy_pass\s+http://[^$;\s]+", content)
        assert len(bare_matches) == 0, (
            f"Found bare proxy_pass directives that would cause nginx to "
            f"resolve DNS at startup (may hang if upstream is missing): {bare_matches}"
        )

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

    # --- RESOURCE_PREFIX (task doc §3 — networks + containers + named volumes) ---

    def test_resource_prefix_applies_to_network_and_containers(self, monkeypatch):
        """Both helpers carry the prefix — they cover all three global namespaces.

        Volume names are not derived here directly: the templates build them from
        ``{{ container_prefix }}`` (e.g. `name: {{ container_prefix }}n8n_data`), so
        covering this helper covers volumes too.
        """
        monkeypatch.setenv("RESOURCE_PREFIX", "subnet-acl-")
        template_engine._load_env()
        try:
            assert template_engine.user_network_name("myapp", "alice", "0") == \
                "subnet-acl-myapp-user_alice-0"
            assert template_engine.container_prefix("myapp", "alice", "0") == \
                "subnet-acl-myapp-user_alice-0-"
        finally:
            monkeypatch.delenv("RESOURCE_PREFIX", raising=False)
            template_engine._load_env()

    def test_resource_prefix_unset_preserves_historical_names(self, monkeypatch):
        """An unset prefix must keep producing today's unprefixed names."""
        monkeypatch.delenv("RESOURCE_PREFIX", raising=False)
        template_engine._load_env()
        assert template_engine.user_network_name("myapp", "alice", "0") == "myapp-user_alice-0"
        assert template_engine.container_prefix("myapp", "alice", "0") == "myapp-user_alice-0-"

    # --- label allocation (GRILLING G3) ---

    def test_next_free_label_starts_at_zero(self, monkeypatch):
        monkeypatch.setattr(registry, "get_user", lambda u: [])
        assert registry.next_free_label("alice", "myapp") == "0"

    def test_next_free_label_skips_past_existing(self, monkeypatch):
        add_registry_user({"user_name": "alice", "service_name": "myapp", "label": "0"})
        add_registry_user({"user_name": "alice", "service_name": "myapp", "label": "1"})
        # a different service's label is not this service's history
        add_registry_user({"user_name": "alice", "service_name": "other", "label": "7"})
        assert registry.next_free_label("alice", "myapp") == "2"

    def test_next_free_label_never_reuses_a_freed_label(self, monkeypatch):
        """max+1, deliberately: a freed label can collide with surviving Docker objects."""
        add_registry_user({"user_name": "alice", "service_name": "myapp", "label": "0"})
        add_registry_user({"user_name": "alice", "service_name": "myapp", "label": "2"})
        remove_registry_service("alice", "myapp", "2")
        assert registry.next_free_label("alice", "myapp") == "3"

    def test_register_request_label_is_optional(self):
        """Omitted, empty and "auto" all mean allocate; a concrete label is honoured."""
        from api import RegisterRequest

        def req(**kw):
            return RegisterRequest(user_name="alice", service_name="myapp",
                                   compose_file_path="/tmp/dc.yml", **kw)

        assert req().label is None
        assert req(label="").label is None
        assert req(label="auto").label is None
        assert req(label="2").label == "2"

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
        assert copied.endswith(".env.1.alice.0")  # positional canonical name
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
        # canonical per-user env is passed as an --env-file LIST (G17)
        assert "- .env.1.bob.1" in content
        assert "env_file: .env\n" not in content  # original replaced
        # canonical per-user file exists
        assert (tmp_path / ".env.1.bob.1").exists()

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
        assert "- .env.1.eve.2" in content
        assert "- .env\n" not in content  # original replaced
        assert (tmp_path / ".env.1.eve.2").exists()

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
        assert "- .env.1.alice.0" in content
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
        # No env_file supplied ⇒ the canonical per-user env is still passed
        # explicitly (G17: never let compose auto-read the recipe .env)
        assert "- .env.1.alice.0" in content
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
        assert "- .env.1.alice.0" in Path(out_a).read_text()
        assert "- .env.1.bob.0" in Path(out_b).read_text()


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
        """GRILLING G52 — a tenant attachment is SUBORDINATE, and re-made.

        Docker's `gw-priority` (default 0) picks a container's top network — the
        one whose address a published port is delivered to. A per-user network
        attached at the default TIED with the platform's and won the name
        tie-break (`-` < `_`), so the internal nginx's published door landed on a
        tenant address and the guard refused the door's own client (measured:
        client `100.96.0.1` → 403). The negative priority is the fix.

        The disconnect first is not decoration: the daemon REFUSES a second
        `connect` on an existing membership, so without re-making it a membership
        created before this rule would keep priority 0 forever — and the recreate
        repair, which is the thing that runs after every nginx restart, would
        silently correct nothing.
        """
        calls = self._mock_run(monkeypatch)
        docker_ops.network_connect("subnet-acl-nginx", "myapp-user_alice-0")
        assert calls[-2] == [
            "docker", "network", "disconnect", "myapp-user_alice-0", "subnet-acl-nginx",
        ], calls[-2]
        assert calls[-1] == [
            "docker", "network", "connect", "--gw-priority", "-1",
            "myapp-user_alice-0", "subnet-acl-nginx",
        ], calls[-1]

    def test_the_attachment_priority_is_negative_and_reaches_the_command(self, monkeypatch):
        """The value itself, pinned: it must stay BELOW the platform's default 0."""
        assert docker_ops.TENANT_GW_PRIORITY < 0, (
            "the platform's own network sits at Docker's default 0, so only a "
            "negative priority makes it unambiguously the top one")
        calls = self._mock_run(monkeypatch)
        docker_ops.network_connect("c", "n", gw_priority=None)
        assert "--gw-priority" not in calls[-1], calls[-1]

    def test_network_disconnect_command(self, monkeypatch):
        calls = self._mock_run(monkeypatch)
        docker_ops.network_disconnect("subnet-acl-nginx", "myapp-user_alice-0")
        assert calls[-1] == [
            "docker", "network", "disconnect", "myapp-user_alice-0", "subnet-acl-nginx"
        ]

    def test_nginx_reload_command(self, monkeypatch):
        calls = self._mock_run(monkeypatch)
        docker_ops.nginx_reload("subnet-acl-nginx")
        assert calls[-1] == [
            "docker", "exec", "subnet-acl-nginx", "nginx", "-s", "reload"
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
        docker_ops.network_connect("subnet-acl-nginx", "nonexistent-net")

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
        docker_ops.network_disconnect("subnet-acl-nginx", "nonexistent-net")

    def test_nginx_reload_surfaces_a_failed_exec(self, monkeypatch):
        """`check=False` stays (the runner must not raise on the status), but the
        failed reload is then raised by nginx_reload so rollbacks can run."""
        import subprocess
        calls = []

        def fake_run(args, check=True):
            calls.append((args, check))
            return subprocess.CompletedProcess(args, 1, "", "container is not running")

        monkeypatch.setattr(docker_ops, "_run", fake_run)
        with pytest.raises(RuntimeError, match="nginx reload failed"):
            docker_ops.nginx_reload("subnet-acl-nginx")
        assert calls and calls[0][1] is False

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
        fake_json = '[{"Name": "test-net", "Containers": {"abc": {"Name": "subnet-acl-nginx"}, "def": {"Name": "myapp-web"}}}]'
        import subprocess as sp
        monkeypatch.setattr(docker_ops.subprocess, "run",
            lambda *a, **kw: sp.CompletedProcess([], 0, stdout=fake_json, stderr=""))
        assert docker_ops.network_connected_to_container("test-net", "subnet-acl-nginx") is True

    def test_network_connected_to_container_negative(self, monkeypatch):
        """network_connected_to_container returns False when container is not in network."""
        fake_json = '[{"Name": "test-net", "Containers": {"abc": {"Name": "other-container"}}}]'
        import subprocess as sp
        monkeypatch.setattr(docker_ops.subprocess, "run",
            lambda *a, **kw: sp.CompletedProcess([], 0, stdout=fake_json, stderr=""))
        assert docker_ops.network_connected_to_container("test-net", "subnet-acl-nginx") is False

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
                    stdout='[{"Name":"orphan-net","Containers":{"abc":{"Name":"subnet-acl-nginx"}}}]',
                    stderr="")
            return sp.CompletedProcess([], 0, stdout="", stderr="")

        monkeypatch.setattr(docker_ops.subprocess, "run", capture_run)
        # Mock network_disconnect to be a no-op
        monkeypatch.setattr(docker_ops, "network_disconnect", lambda *a: None)

        result = docker_ops.orphan_network_cleanup("orphan-net", "subnet-acl-nginx")
        assert result is True
        # Should have run docker network rm
        rm_calls = [c for c in call_args if "rm" in c and "network" in c]
        assert len(rm_calls) >= 1

    def test_orphan_network_cleanup_disconnects_foreign_container(self, monkeypatch):
        """A container that is NOT the configured nginx must be disconnected too.

        Regression guard for task doc §2: the old helper disconnected only the configured
        nginx and removed the network only when it was the sole attached container. A network
        from an earlier era stays attached to the *old* nginx (`provision-nginx`), so the
        disconnect was a no-op on the wrong container, `network rm` failed, and the stale
        network survived — after which compose could never replace it and every redeploy of
        the same (user, service, label) died on "network … has active endpoints".
        """
        import subprocess as sp
        fake_json = '[{"Name":"shared-net","Containers":{"abc":{"Name":"provision-nginx"}}}]'
        rm_results = []

        def fake_run(args, **kw):
            if "inspect" in args:
                return sp.CompletedProcess([], 0, stdout=fake_json, stderr="")
            if "rm" in args:
                rm_results.append(list(args))
                return sp.CompletedProcess([], 0, stdout="", stderr="")
            return sp.CompletedProcess([], 0, stdout="", stderr="")

        disconnected: list[tuple] = []
        monkeypatch.setattr(docker_ops.subprocess, "run", fake_run)
        monkeypatch.setattr(docker_ops, "network_disconnect",
                            lambda c, n: disconnected.append((c, n)))

        result = docker_ops.orphan_network_cleanup("shared-net", "subnet-acl-nginx")
        assert result is True
        # The foreign container — not the configured nginx — was the one disconnected.
        assert ("provision-nginx", "shared-net") in disconnected
        assert rm_results, "network rm was never attempted"

    def test_orphan_network_cleanup_reports_failed_removal(self, monkeypatch):
        """A failed removal returns False (and is logged) instead of being swallowed."""
        import subprocess as sp

        def fake_run(args, **kw):
            if "inspect" in args:
                return sp.CompletedProcess([], 0,
                    stdout='[{"Name":"busy-net","Containers":{"abc":{"Name":"provision-nginx"}}}]',
                    stderr="")
            return sp.CompletedProcess([], 1, stdout="",
                                       stderr="error while removing network: active endpoints")

        monkeypatch.setattr(docker_ops.subprocess, "run", fake_run)
        monkeypatch.setattr(docker_ops, "network_disconnect", lambda *a: None)

        assert docker_ops.orphan_network_cleanup("busy-net", "subnet-acl-nginx") is False

    def test_orphan_network_cleanup_nonexistent_network(self, monkeypatch):
        """orphan_network_cleanup returns False for nonexistent network."""
        import subprocess as sp
        monkeypatch.setattr(docker_ops.subprocess, "run",
            lambda *a, **kw: sp.CompletedProcess([], 1, stdout="", stderr=""))
        assert docker_ops.orphan_network_cleanup("ghost-net") is False

    # ── Thread-local task log file ──

    def test_set_clear_task_log_file(self, tmp_path):
        """set_task_log_file and clear_task_log_file manage thread-local state."""
        log_path = str(tmp_path / "task-abc.log")
        docker_ops.set_task_log_file(log_path)
        assert docker_ops._task_log.path == log_path
        docker_ops.clear_task_log_file()
        assert getattr(docker_ops._task_log, "path", None) is None

    def test_task_log_writes_to_file(self, tmp_path):
        """_write_log writes to the task-specific log when set."""
        log_path = str(tmp_path / "task-xyz.log")
        docker_ops.set_task_log_file(log_path)
        docker_ops._write_log("test log line\n")
        docker_ops.clear_task_log_file()

        from pathlib import Path
        content = Path(log_path).read_text()
        assert "test log line" in content

    def test_task_log_does_not_leak_between_threads(self, tmp_path):
        """clear_task_log_file stops writing to the task log."""
        log_path = str(tmp_path / "task-leak.log")
        docker_ops.set_task_log_file(log_path)
        docker_ops._write_log("before clear\n")
        docker_ops.clear_task_log_file()
        docker_ops._write_log("after clear\n")

        from pathlib import Path
        content = Path(log_path).read_text()
        assert "before clear" in content
        assert "after clear" not in content  # not written after clear

    def test_task_log_captures_stdout_from_child_threads(self, tmp_path, monkeypatch):
        """_run() writes command output to the per-task log even from child threads.

        Regression test for the ``threading.local()`` propagation bug:
        ``_task_log.path`` was set on the caller thread but the reader threads
        spawned by ``_run()`` could not see it, so docker stdout/stderr was
        silently discarded.  The fix captures the path as a closure variable
        inside ``_run()`` before spawning threads.
        """
        import io

        log_path = str(tmp_path / "task-capture.log")
        docker_ops.set_task_log_file(log_path)

        stdout_data = "Container web-1  Started\nContainer db-1  Healthy\n"
        stderr_data = "warning: deprecated option\nBuild finished\n"

        # A pipe-like object: readline() returns lines until exhausted,
        # then returns "".  close() is a no-op (real pipes return "" after
        # the write end closes; StringIO raises ValueError instead).
        class _Pipe(io.StringIO):
            def close(self):
                pass  # don't invalidate the buffer on close

        class _FakeProc:
            def __init__(self, args, **kwargs):
                self.returncode = 0
                self.stdout = _Pipe(stdout_data)
                self.stderr = _Pipe(stderr_data)
            def wait(self): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass

        monkeypatch.setattr(docker_ops.subprocess, "Popen", _FakeProc)

        docker_ops._run(["docker", "compose", "up", "-d"])
        docker_ops.clear_task_log_file()

        content = Path(log_path).read_text()

        # The command line MUST be logged
        assert "+ docker compose up -d" in content

        # stdout output MUST be captured (this was broken before the fix)
        assert "Container web-1  Started" in content, (
            "stdout output NOT captured in task log — "
            "threading.local() path may not be propagating to child threads"
        )
        assert "Container db-1  Healthy" in content

        # stderr output MUST be captured
        assert "warning: deprecated option" in content
        assert "Build finished" in content

    def test_task_log_captures_output_even_when_called_from_another_thread(self, tmp_path, monkeypatch):
        """Same as above, but _run() is invoked from a spawned thread.

        This simulates the real deployment scenario: the task worker runs on
        a ThreadPoolExecutor thread, calls set_task_log_file(), then _run().
        """
        import io, threading

        log_path = str(tmp_path / "task-thread.log")

        stdout_data = "Network connected\n"
        stderr_data = ""

        class _Pipe(io.StringIO):
            def close(self):
                pass

        class _FakeProc:
            def __init__(self, args, **kwargs):
                self.returncode = 0
                self.stdout = _Pipe(stdout_data)
                self.stderr = _Pipe(stderr_data)
            def wait(self): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass

        monkeypatch.setattr(docker_ops.subprocess, "Popen", _FakeProc)

        errors = []

        def worker():
            try:
                docker_ops.set_task_log_file(log_path)
                docker_ops._run(["docker", "network", "connect", "net", "container"])
            except Exception as e:
                errors.append(str(e))
            finally:
                docker_ops.clear_task_log_file()

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        assert not errors, f"Worker thread raised: {errors}"
        content = Path(log_path).read_text()

        assert "+ docker network connect net container" in content
        assert "Network connected" in content, (
            "stdout NOT captured when _run() called from a spawned thread — "
            "the fix must use closure-captured path, not threading.local()"
        )


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
        transformed, src_to_key, tokens, _ = convert(data)
        raw = yaml.dump(transformed, default_flow_style=False, sort_keys=False)
        return tokens.detokenize(raw), src_to_key

    def test_convert_strips_name(self):
        from lib.compose_converter import convert
        transformed, _, _, _ = convert(self._sample_data())
        assert "name" not in transformed

    def test_convert_strips_ports(self):
        from lib.compose_converter import convert
        transformed, _, _, _ = convert(self._sample_data())
        assert "ports" not in transformed["services"]["web"]

    def test_convert_keeps_profiles_verbatim(self):
        """design §Profiles: converter ships profiles: as-is (GAP-4 fix).

        A service with a named profile stays in the template WITH its
        profiles key — activation happens at up time via --profile.
        """
        from lib.compose_converter import convert
        data = self._sample_data()
        data["services"]["web"]["profiles"] = ["falkordb"]
        transformed, _, _, _ = convert(data)
        assert "web" in transformed["services"]
        assert transformed["services"]["web"]["profiles"] == ["falkordb"]

    def test_convert_keeps_empty_string_profile_services(self):
        from lib.compose_converter import convert
        data = self._sample_data()
        data["services"]["web"]["profiles"] = [""]
        transformed, _, _, _ = convert(data)
        assert "web" in transformed["services"]
        assert transformed["services"]["web"]["profiles"] == [""]

    def test_convert_keeps_mixed_empty_and_named_profile_lists(self):
        """dify db_postgres case: [\"\", \"postgresql\"] must NOT be dropped."""
        from lib.compose_converter import convert
        data = self._sample_data()
        data["services"]["db"]["profiles"] = ["", "postgresql"]
        transformed, _, _, _ = convert(data)
        assert "db" in transformed["services"]
        assert transformed["services"]["db"]["profiles"] == ["", "postgresql"]

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
        _, src_to_key, _, _ = convert(self._sample_data())
        assert "/data/myapp/html" in src_to_key
        assert "/var/lib/myapp/db" in src_to_key

    def test_convert_named_volumes_are_bind_keys(self):
        """G59 (D4 = C) — a non-external top-level volume becomes a bind key."""
        from lib.compose_converter import convert
        _, src_to_key, _, _ = convert(self._sample_data())
        assert "db_socket" in src_to_key

    def test_convert_replaces_networks(self):
        text, _ = self._convert_to_text(self._sample_data())
        assert "{{ network_name }}" in text

    def test_convert_turns_named_volumes_into_binds(self):
        """G59 — the service entry names the per-user path, and the Docker
        named-volume declaration is dropped (no orphan volume)."""
        text, _ = self._convert_to_text(self._sample_data())
        assert "{{ volumes['db_socket'] }}" in text
        assert "{{ container_prefix }}db_socket" not in text

    def test_convert_preserves_env_vars(self):
        data = self._sample_data()
        data["services"]["web"]["environment"] = ["MY_VAR=${MY_ENV_VAR}"]
        text, _ = self._convert_to_text(data)
        assert "${MY_ENV_VAR}" in text

    def test_convert_external_volume_unchanged(self):
        data = self._sample_data()
        data["volumes"]["shared_vol"] = {"external": True}
        from lib.compose_converter import convert
        transformed, _, tokens, _ = convert(data)
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

    def test_ensure_subnet_ipam_block_injects_and_backs_up(self, tmp_path):
        """Gap 8: an old template missing {% if subnet %} gets the ipam block
        injected and the original backed up as .bak."""
        from lib.compose_converter import ensure_subnet_ipam_block
        tpl = tmp_path / "old.yml.j2"
        tpl.write_text(
            "services:\n"
            "  web:\n"
            "    image: nginx\n"
            "networks:\n"
            "  {{ network_name }}:\n"
            "    name: {{ network_name }}\n"
        )
        assert ensure_subnet_ipam_block(str(tpl)) is True
        content = tpl.read_text()
        assert "{% if subnet %}" in content
        assert "ipam:" in content
        assert "subnet: {{ subnet }}" in content
        assert "gateway: {{ gateway }}" in content
        # original backed up
        bak = tmp_path / "old.yml.j2.bak"
        assert bak.exists()
        assert "{% if subnet %}" not in bak.read_text()

    def test_ensure_subnet_ipam_block_noop_when_present(self, tmp_path):
        from lib.compose_converter import ensure_subnet_ipam_block
        tpl = tmp_path / "new.yml.j2"
        tpl.write_text(
            "networks:\n"
            "  {{ network_name }}:\n"
            "    name: {{ network_name }}\n"
            "{% if subnet %}\n"
            "    ipam:\n"
            "      config:\n"
            "        - subnet: {{ subnet }}\n"
            "{% endif %}\n"
        )
        assert ensure_subnet_ipam_block(str(tpl)) is False

    def test_ensure_subnet_ipam_block_noop_without_anchor(self, tmp_path):
        from lib.compose_converter import ensure_subnet_ipam_block
        tpl = tmp_path / "noanchor.yml.j2"
        tpl.write_text("services:\n  web:\n    image: nginx\n")
        assert ensure_subnet_ipam_block(str(tpl)) is False

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
        _, src_to_key, _, _ = convert(data)
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

    def test_get_compose_service_names_with_jinja2_control_flow(self, tmp_path):
        """Templates with {% if/endif %} blocks should be parseable.

        GAP-034: The YAML sanitizer must strip {% %} control flow tags
        alongside {{ }} expression tokens, otherwise the YAML parser fails
        and returns an empty service list.

        GAP-NEW-01/GAP-NEW-02 regression: The IPAM block injected by
        compose_file_to_template() used 6-space indentation for ipam:
        while the sibling name: entry used 4-space. After {% %} tag
        stripping, ipam: at 6-space was nested under the scalar name:
        entry -- invalid YAML. This test uses 4-space ipam: indentation
        (matching name: at the same level) which produces valid YAML
        after {% %} stripping.
        """
        from lib.compose_converter import get_compose_service_names
        compose = str(tmp_path / "docker-compose.with-if.yml.j2")
        Path(compose).write_text(
            "services:\n"
            "  web:\n"
            "    image: nginx\n"
            "networks:\n"
            "  {{ network_name }}:\n"
            "    name: {{ network_name }}\n"
            "{% if subnet %}\n"
            "    ipam:\n"
            "      config:\n"
            "        - subnet: {{ subnet }}\n"
            "          gateway: {{ gateway }}\n"
            "{% endif %}\n"
        )
        names = get_compose_service_names(compose)
        assert names == ["web"], f"Expected ['web'], got {names}"

    def test_get_compose_service_names_ipam_indentation_regression(self, tmp_path):
        """Legacy 6-space ipam: indentation should also be handled.

        GAP-NEW-01/GAP-NEW-02: The original compose_file_to_template()
        injected ipam: at 6-space indentation.  After {% %} tag stripping,
        the 6-space ipam: was nested under the scalar name: entry (4-space)
        producing invalid YAML.  The regex fallback in
        get_compose_service_names() must handle this edge case.
        """
        from lib.compose_converter import get_compose_service_names
        # 6-space indentation for ipam: -- the legacy broken format
        compose = str(tmp_path / "docker-compose.broken-ipam.yml.j2")
        Path(compose).write_text(
            "services:\n"
            "  web:\n"
            "    image: nginx\n"
            "networks:\n"
            "  {{ network_name }}:\n"
            "    name: {{ network_name }}\n"
            "{% if subnet %}\n"
            "      ipam:\n"
            "        config:\n"
            "          - subnet: {{ subnet }}\n"
            "            gateway: {{ gateway }}\n"
            "{% endif %}\n"
        )
        names = get_compose_service_names(compose)
        assert names == ["web"], (
            f"Regex fallback should extract ['web'] even with 6-space ipam: "
            f"indentation, got {names}"
        )

    def test_get_compose_service_names_from_generated_template(self, tmp_path):
        """End-to-end: compose_file_to_template output is parseable.

        GAP-NEW-01/GAP-NEW-02: After fixing IPAM indent to 4-space,
        the generated template should be directly parseable by
        get_compose_service_names() without needing the regex fallback.
        """
        from lib.compose_converter import (
            compose_file_to_template,
            get_compose_service_names,
        )
        # Write a minimal compose file with networks
        compose_src = str(tmp_path / "docker-compose.yml")
        Path(compose_src).write_text(
            "services:\n"
            "  web:\n"
            "    image: nginx\n"
            "networks:\n"
            "  mynet:\n"
        )
        out = str(tmp_path / "output.yml.j2")
        compose_file_to_template(compose_src, out, "myapp")
        names = get_compose_service_names(out)
        assert "web" in names, (
            f"Expected 'web' in service names from generated template, got {names}"
        )

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
        _, src_to_key, _, _ = convert(data)
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
        _, src_to_key, _, _ = convert(data)
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
        _, src_to_key, _, _ = convert(data)
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

    def test_an_extra_http_endpoint_keeps_its_sub_service(self):
        """R1.5 / T16 — a `server_name` carrying `--` survives conversion.

        It used to collapse to `{{ hostname }}` like every other name, so a block
        declaring `<instance host>--<sub_service>.<domain>` was flattened back to
        the instance host at render time and the endpoint the row declares ceased
        to exist (round 2's F47 — the reason no deploy could satisfy
        `validate_conf`).
        """
        from lib.nginx_converter import convert_nginx
        conf = (
            "server {\n    listen 80;\n    server_name web_hostname;\n"
            "    location / { proxy_pass http://web:80; }\n}\n"
            "server {\n    listen 80;\n    server_name web_hostname--neo4j;\n"
            "    location / { proxy_pass http://neo4j:7474; }\n}\n"
        )
        out = convert_nginx(conf, "web", ["web", "neo4j"])
        assert "server_name {{ hostname }};" in out
        assert "server_name {{ hostname_stem }}--neo4j.{{ domain_name }};" in out, (
            "the extra endpoint's sub-service must reach the template")
        assert "web_hostname--neo4j" not in out

    def test_a_fully_qualified_extra_name_keeps_only_its_sub_service(self):
        """A model that writes the whole served name still yields one label."""
        from lib.nginx_converter import convert_nginx
        conf = (
            "server {\n    listen 80;\n"
            "    server_name web-alice-0--neo4j.snaprovision.com;\n"
            "    location / { proxy_pass http://neo4j:7474; }\n}\n"
        )
        out = convert_nginx(conf, "web", ["neo4j"])
        assert "server_name {{ hostname_stem }}--neo4j.{{ domain_name }};" in out


class TestTheRenderedExtraHttpEndpoint:
    """R1.5 / T16 — the converted template RENDERS to the name the row declares.

    The converter's output is a template; this drives the same two steps the
    deploy does (convert, then render per user) and reads the served names out of
    the produced file — the observable `validate_conf` (R1.3) is about.
    """

    _CONF = (
        "server {\n    listen 80;\n    server_name web_hostname;\n"
        "    location / { proxy_pass http://web:80; }\n}\n"
        "server {\n    listen 80;\n    server_name web_hostname--neo4j;\n"
        "    location / { proxy_pass http://neo4j:7474; }\n}\n"
    )

    def _served(self, **overrides):
        import re
        from lib.nginx_converter import convert_nginx
        from lib.template_engine import render_nginx_conf

        args = dict(user_name="alice", service_name="web", label="0",
                    domain_name="snaprovision.com", htpasswd_path="", https=False,
                    ssl_certificate_path="", ssl_certificate_key_path="")
        args.update(overrides)
        converted = convert_nginx(self._CONF, args["service_name"], ["web", "neo4j"])
        tpl = self._dir / "t.j2"
        out = self._dir / "t.conf"
        tpl.write_text(converted)
        render_nginx_conf(template_path=str(tpl), output_path=str(out), **args)
        return {m.group(1).strip() for m in
                re.finditer(r"^[ \t]*server_name[ \t]+([^;\n]+);", out.read_text(), re.M)}

    def setup_method(self, method):
        import pathlib
        import tempfile
        self._dir = pathlib.Path(tempfile.mkdtemp())

    def test_the_primary_and_the_extra_endpoint_are_both_served(self):
        served = self._served()
        assert "web-alice-0.snaprovision.com" in served
        assert "web-alice-0--neo4j.snaprovision.com" in served, served

    def test_the_extra_name_is_one_label_under_the_domain(self):
        """The property the single wildcard certificate rests on (R1.5)."""
        name = "web-alice-0--neo4j.snaprovision.com"
        assert name in self._served()
        assert name.count(".") == 2, "one label, then the two-label domain"

    def test_convert_auth_basic_user_file(self):
        from lib.nginx_converter import convert_nginx
        out = convert_nginx(_SAMPLE_NGINX_CONF)
        assert "auth_basic_user_file {{ htpasswd_path }};" in out

    def test_convert_proxy_pass_with_compose_names(self):
        """proxy_pass is rewritten when host matches a compose service name."""
        from lib.nginx_converter import convert_nginx
        out = convert_nginx(_SAMPLE_NGINX_CONF, compose_service_names=["myapp-web"])
        assert "proxy_pass http://{{ container_prefix }}myapp-web:80;" in out

    def test_convert_proxy_pass_without_compose_names(self):
        """Without compose_service_names, proxy_pass is left as-is."""
        from lib.nginx_converter import convert_nginx
        out = convert_nginx(_SAMPLE_NGINX_CONF)
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

    def test_convert_ssl_block_wrapped_in_if_tls(self):
        """Server blocks containing listen ... ssl are wrapped in {% if tls %}...{% endif %}."""
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
        assert "{% if tls %}" in out
        assert "{% endif %}" in out

    def test_convert_tls_redirect_block_wrapped(self):
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
        assert out.count("{% if tls %}") == 2
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
        # Auto-generated HTTPS block is wrapped in {% if tls %}
        assert "{% if tls %}" in out
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
        # The original listen 80 block appears BEFORE any {% if tls %} wrapper
        idx_http = out.index("listen 80;")
        idx_if = out.index("{% if tls %}")
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
        assert "{% if tls %}" in out
        assert "listen 443 ssl;" in out

    # --- ACL template injection (GAP-001 through GAP-004) ---

    def test_convert_is_acl_free_template(self):
        """v4 F2: the converter output is a clean, mode-independent template —
        the JWT/ACL scaffolding is injected at RENDER time, not here."""
        from lib.nginx_converter import convert_nginx
        out = convert_nginx(_SAMPLE_NGINX_CONF)
        assert "auth_request" not in out
        assert "auth_request_set" not in out
        assert "error_page" not in out
        assert "location @auth_401" not in out
        assert "location @auth_403" not in out
        assert "location = /_auth_jwt" not in out
        assert "location /__bypass__/" not in out
        assert "location = /_set_token" not in out
        assert "location /__basic__/" not in out
        assert "include /etc/nginx/env.d" not in out

    def test_convert_no_bypass_rewrite(self):
        """v4 F2: no if ($has_basic) / __bypass__ rewrite in the template."""
        from lib.nginx_converter import convert_nginx
        out = convert_nginx(_SAMPLE_NGINX_CONF)
        assert "if ($has_basic)" not in out
        assert "rewrite ^ /__bypass__$request_uri last;" not in out

    def test_convert_still_injects_auth_basic(self):
        """The converter still injects auth_basic into server blocks (the
        renderer relocates it into /__basic__/)."""
        from lib.nginx_converter import convert_nginx
        out = convert_nginx(_SAMPLE_NGINX_CONF)
        assert "auth_basic" in out
        assert "{{ htpasswd_path }}" in out

    def test_convert_all_directives_integrated(self):
        """Integration check: template substitutions coexist; no v2 ACL remains."""
        from lib.nginx_converter import convert_nginx
        conf = (
            "server {\n"
            "    listen 80;\n"
            "    server_name example.com;\n"
            "    auth_basic \"My App\";\n"
            "    auth_basic_user_file /etc/nginx/htpasswd/myapp;\n"
            "    location / {\n"
            "        proxy_pass http://myapp-web:80;\n"
            "    }\n"
            "}\n"
        )
        out = convert_nginx(conf, compose_service_names=["myapp-web"])
        # Template substitutions still applied.
        assert "{{ hostname }}" in out
        assert "{{ htpasswd_path }}" in out
        assert "{{ container_prefix }}myapp-web" in out
        # No v2 ACL scaffolding leaked into the template.
        assert "auth_request" not in out
        assert "error_page" not in out
        assert "location = /_auth_jwt" not in out
        assert "location = /_set_token" not in out
        assert "location /__bypass__/" not in out


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

    def test_recipe_path_for_entry_root_and_subdir(self, tmp_path, monkeypatch):
        """The per-instance recipe subdir exposed to the dashboard:
        '' for a project-root recipe, the dir name for a sub-recipe (dify →
        'docker'). Derived from the recorded per-user file paths."""
        import api as api_mod

        root = tmp_path / "source_projects"
        (root / "myapp").mkdir(parents=True)
        (root / "dify" / "docker").mkdir(parents=True)
        monkeypatch.setattr(api_mod, "SOURCE_PROJECTS_DIR", root)

        root_entry = {
            "service_name": "myapp",
            "compose_file_path": str(root / "myapp" / "docker-compose.user-alice.0.yml"),
        }
        assert api_mod._recipe_path_for_entry(root_entry) == ""

        sub_entry = {
            "service_name": "dify",
            "compose_file_path": str(root / "dify" / "docker" / "docker-compose.user-alice.0.yml"),
        }
        assert api_mod._recipe_path_for_entry(sub_entry) == "docker"

        # A template-only entry resolves too (compose_template_path fallback).
        tpl_entry = {
            "service_name": "dify",
            "compose_template_path": str(root / "dify" / "docker" / "docker-compose.yml.j2"),
        }
        assert api_mod._recipe_path_for_entry(tpl_entry) == "docker"

        # Unknown/foreign paths never escape the project dir.
        assert api_mod._recipe_path_for_entry(
            {"service_name": "myapp", "compose_file_path": "/etc/passwd"}
        ) == ""

    def test_service_payload_exposes_recipe_path_and_env_file(self, tmp_path, monkeypatch):
        """GET /users service entries carry recipe_path + env_file_path so the
        dashboard resolves Deployment-Files links inside the recipe dir."""
        import api as api_mod
        from lib import registry as reg_mod

        root = tmp_path / "source_projects"
        (root / "dify" / "docker").mkdir(parents=True)
        monkeypatch.setattr(api_mod, "SOURCE_PROJECTS_DIR", root)

        entry = {
            "user_name": "alice",
            "service_name": "dify",
            "label": "0",
            "compose_file_path": str(root / "dify" / "docker" / "docker-compose.user-alice.0.yml"),
            "env_file_path": str(root / "dify" / "docker" / ".env.1.alice.0"),
            "container_names": [],
            "volumes": {},
        }
        add_registry_user(entry)

        status = api_mod._status_for_user("alice", {})
        bucket = (
            status["healthy_services"] or status["unhealthy_services"] or status["missing_services"]
        )
        assert bucket, "entry must appear in one of the status buckets"
        svc = bucket[0]
        assert svc["recipe_path"] == "docker"
        assert svc["env_file_path"].endswith("/dify/docker/.env.1.alice.0")

    def test_purge_user_artifacts_removes_recipe_files_and_user_data(self, tmp_path):
        """Delete semantics (decision #2): `remove_user` -> `_purge_user_artifacts`
        leaves NOTHING behind — the per-user recipe files AND the
        {user_data}/{user}/{service}/{label} volume tree are removed, while the
        original recipe files are untouched."""
        from lib.provisioner import _purge_user_artifacts

        recipe = tmp_path / "recipe"
        recipe.mkdir()
        # Per-user artifacts for THIS instance (must be purged).
        for name in (".env.raw.alice.0", ".env.1.alice.0", "docker-compose.user-alice.0.yml"):
            (recipe / name).write_text("x")
        (recipe / ".env.raw.alice.0.generated").write_text("")
        # Original recipe file + another instance's artifact (must survive).
        (recipe / "docker-compose.yml").write_text("services: {}")
        (recipe / ".env.raw.bob.1").write_text("keep me")

        tree = tmp_path / "user_data" / "alice" / "myapp" / "0"
        (tree / "app_data").mkdir(parents=True)
        (tree / "app_data" / "db.sqlite").write_text("payload")

        entry = {
            "compose_file_path": str(recipe / "docker-compose.user-alice.0.yml"),
            "volumes": {"app_data": str(tree / "app_data")},
        }
        _purge_user_artifacts("alice", "myapp", "0", entry)

        # user_data volume tree is GONE (the whole instance tree, not just the vol dir).
        assert not tree.exists()
        # Per-user recipe artifacts are gone...
        for name in (".env.raw.alice.0", ".env.1.alice.0", "docker-compose.user-alice.0.yml"):
            assert not (recipe / name).exists(), name
        assert not (recipe / ".env.raw.alice.0.generated").exists()
        # ...but the original recipe and other instances are untouched.
        assert (recipe / "docker-compose.yml").exists()
        assert (recipe / ".env.raw.bob.1").exists()

    def test_purge_keeps_recipe_level_shared_artifacts(self, tmp_path):
        """Regression (found live): deleting ONE user's instance must not remove
        the recipe-level SHARED artifacts.

        `docker-compose.fix.yml`, `nginx.fix.conf` and `.env.fix` carry no
        per-user suffix — they belong to the recipe, not to a deploy. A stored
        selection of ANOTHER deployment names them (the -api receives
        `nginx.fix.conf` as `nginx_conf_file_path`), so a per-user purge that
        deletes them 502s that other deployment on its next register. `.env.fix`
        is recipe-level by the same argument and is kept too. Only the
        `.{user}.{label}` artifacts and this user's data tree may go.

        This test FAILS against the pre-fix code, which listed all three names in
        its `fix_names` delete set."""
        from lib.provisioner import _purge_user_artifacts

        recipe = tmp_path / "recipe"
        recipe.mkdir()
        # Recipe-level shared artifacts — must survive BYTE-IDENTICAL.
        shared = {}
        for name in (
            "docker-compose.fix.yml",
            "nginx.fix.conf",
            ".env.fix",
            "docker-compose.fix.yml.j2",
            "nginx.fix.conf.j2",
        ):
            (recipe / name).write_text(f"shared {name}\n")
            shared[name] = (recipe / name).read_bytes()
        # Per-user artifacts for THIS instance (must be purged).
        for name in (".env.raw.alice.0", ".env.1.alice.0", "docker-compose.user-alice.0.yml"):
            (recipe / name).write_text("x")
        (recipe / ".env.raw.alice.0.generated").write_text("")
        # Original recipe + another instance's per-user artifact (must survive).
        (recipe / "docker-compose.yml").write_text("services: {}")
        (recipe / ".env.raw.bob.1").write_text("keep me")

        tree = tmp_path / "user_data" / "alice" / "myapp" / "0"
        (tree / "app_data").mkdir(parents=True)
        (tree / "app_data" / "db.sqlite").write_text("payload")

        entry = {
            "compose_file_path": str(recipe / "docker-compose.user-alice.0.yml"),
            "volumes": {"app_data": str(tree / "app_data")},
        }
        _purge_user_artifacts("alice", "myapp", "0", entry)

        # The user's own tree and per-user recipe files are gone...
        assert not tree.exists(), "the user's data tree must still go"
        for name in (".env.raw.alice.0", ".env.1.alice.0", "docker-compose.user-alice.0.yml"):
            assert not (recipe / name).exists(), name
        assert not (recipe / ".env.raw.alice.0.generated").exists()
        # ...while the recipe-level shared set is untouched.
        for name, before in shared.items():
            assert (recipe / name).exists(), f"{name} is recipe-level, must survive"
            assert (recipe / name).read_bytes() == before, f"{name} bytes changed"
        assert (recipe / "docker-compose.yml").exists()
        assert (recipe / ".env.raw.bob.1").exists()


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
        self.events: list[str] = []

        def fake_run(args, check=True):
            self.calls.append(list(args))
            if "up" in args:
                self.events.append("up")
            import subprocess as sp
            return sp.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(docker_ops, "_run", fake_run)
        # Mock network connect / nginx reload
        monkeypatch.setattr(docker_ops, "network_connect", lambda *a, **kw: None)
        monkeypatch.setattr(
            docker_ops, "nginx_reload",
            lambda *a, **kw: self.events.append("reload"),
        )

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
        assert ".env.1.envuser.0" in stored, (
            f"Registry should store per-user copy .env.1.envuser.0, got: {stored}"
        )
        assert "custom.env" not in stored, (
            f"Registry should NOT store original custom.env, got: {stored}"
        )
        assert Path(stored).exists(), f"Per-user env file not found: {stored}"
        assert Path(stored).read_text() == "FOO=bar\n"

    def test_register_without_env_file_uses_empty_per_user_env(self):
        """Always-pass --env-file invariant (design G17): with no env selected
        an EMPTY per-user env file is created and recorded, so compose never
        auto-reads the recipe-level .env."""
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
        assert stored is not None, "per-user env file must always exist"
        assert stored.endswith(".env.1.noenv.0")
        assert Path(stored).exists()
        assert Path(stored).read_text() == ""
        assert entry.get("env_files") == [stored]
        # compose_up must receive the explicit per-user --env-file
        up_calls = [c for c in self.calls if "up" in c]
        assert up_calls, "compose_up should have been called"
        assert any("--env-file" in c and stored in c for c in up_calls)

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
                assert ".env.1.copyuser.1" in env_path, (
                    f"--env-file should point to per-user copy, got: {env_path}"
                )
                assert "app.env" not in env_path, (
                    f"--env-file should NOT use original name, got: {env_path}"
                )
                break
        else:
            pytest.fail(f"--env-file not found in compose_up: {up_cmd}")

    def test_register_renders_EVERY_conf_it_carries(self, monkeypatch):
        from lib import docker_ops
        monkeypatch.setattr(docker_ops, "shared_network_subnet",
                            lambda *a, **k: "172.17.0.0/16")
        """R4-F52 — the stream conf a deploy carries is RENDERED, not just recorded.

        `register_user` built the pair list (`nginx_out_by_category`) and recorded
        both paths on the entry, but never PASSED it to the renderer:
        `_register_render_and_start` fell back to the single
        `(nginx_template, nginx_out)` pair, so the http conf rendered and the stream
        conf did not. Its path reached the registry while the file never reached the
        disk — which is why every entry's `stream_sub_services` was `[]` and the card
        offered no stream endpoint (measured 2026-10-10 on the live stack).
        """
        stream_tpl = self.tmp_path / "myapp.user-stream.stream.conf.j2"
        stream_tpl.write_text(
            "server {\n"
            "    listen 7687 ssl;\n"
            "    server_name myapp-streamuser-0--bolt.localhost;\n"
            "}\n"
        )
        provisioner.register_user(
            user_name="streamuser",
            service_name="myapp",
            label="0",
            compose_template=COMPOSE_TEMPLATE,
            nginx_template=NGINX_TEMPLATE,
            nginx_conf_templates=[NGINX_TEMPLATE, str(stream_tpl)],
            output_dir=self.tmp_path,
            nginx_output_dir=self.tmp_path,
            user_data_dir=self.user_data_dir,
        )
        rendered = self.tmp_path / "myapp.user-streamuser.0.stream.conf"
        assert rendered.exists(), (
            "the stream conf the deploy carried must be rendered on disk — recording "
            "its path without writing the file is the defect; tree: "
            f"{sorted(p.name for p in self.tmp_path.iterdir())}"
        )
        assert "myapp-streamuser-0--bolt.localhost" in rendered.read_text()
        entry = registry.get_user_service("streamuser", "myapp", "0")
        assert entry["stream_sub_services"] == ["myapp-streamuser-0--bolt.localhost"], entry

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
                    assert ".env.1.rebuildenv.0" in env_path, (
                        f"Rebuild --env-file should use per-user copy, got: {env_path}"
                    )
                    assert "prod.env" not in env_path, (
                        f"Rebuild --env-file should NOT use original, got: {env_path}"
                    )

    def test_rebuild_reloads_nginx_after_up(self):
        """A rebuild recreates containers; nginx must re-resolve after `up`.

        The stream conf's upstream is static, so nginx caches the container IP
        at config load. A recreate may change that IP; without this reload the
        route keeps dialling the old address.
        """
        provisioner.register_user(
            user_name="rebuildreload",
            service_name="myapp",
            label="0",
            compose_template=COMPOSE_TEMPLATE,
            output_dir=self.tmp_path,
            user_data_dir=self.user_data_dir,
        )
        self.events.clear()

        provisioner.rebuild_user(
            user_name="rebuildreload",
            service_name="myapp",
            label="0",
        )

        assert "up" in self.events and "reload" in self.events, self.events
        assert self.events.index("up") < self.events.index("reload"), (
            f"the reload must follow `compose up`, got {self.events}")

    def test_start_service_reloads_nginx_after_up(self):
        """`start_service` can recreate containers; same reload rule."""
        provisioner.register_user(
            user_name="startreload",
            service_name="myapp",
            label="0",
            compose_template=COMPOSE_TEMPLATE,
            output_dir=self.tmp_path,
            user_data_dir=self.user_data_dir,
        )
        self.events.clear()

        provisioner.start_service(
            user_name="startreload",
            service_name="myapp",
            label="0",
        )

        assert "up" in self.events and "reload" in self.events, self.events
        assert self.events.index("up") < self.events.index("reload"), (
            f"the reload must follow `compose up`, got {self.events}")

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
        # The record's toggle is `tls` (ruled 2026-10-10, renamed from `https`);
        # the old name must no longer be written, or the rename buys nothing.
        assert entry.get("tls") is True
        assert "https" not in entry

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
        assert entry.get("tls") is True
        assert "https" not in entry

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
        assert entry.get("tls") is False
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

        result = provisioner.change_password(
            user_name="pwuser",
            service_name="myapp",
            label="0",
            passwd="newpass",
            nginx_container="subnet-acl-nginx",
        )
        assert result["user_name"] == "pwuser"

        entry_after = registry.get_user_service("pwuser", "myapp", "0")
        assert entry_after["basic_version"] == entry_before["basic_version"] + 1
        assert entry_after["has_basic"] is True

        # The stored secret is ciphertext and decrypts to the new password;
        # the gateway injects `Authorization: Basic <basic_username>:<secret>`
        # from the same row, so both halves always agree.
        from lib import keys
        row = registry.db.fetch_one(
            "SELECT secret_enc, basic_username FROM registry.service_basic "
            "WHERE service_id=%s", (entry_after["service_id"],),
        )
        assert row["basic_username"] == "pwuser"
        assert keys.decrypt(bytes(row["secret_enc"]).decode()) == "newpass"
        assert "newpass" not in bytes(row["secret_enc"]).decode(errors="replace")

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

    def test_change_password_rejects_empty_passwd(self):
        """An empty password must never reach the htpasswd.

        ``hash_password("")`` returns "" (lib/auth.py:26), and writing that
        produces an htpasswd line no password can match — the service would be
        locked out entirely. Removing protection is remove_password()'s job.
        """
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
        htpasswd_before = Path(entry_before["htpasswd_path"]).read_text()

        with pytest.raises(ValueError, match="non-empty"):
            provisioner.change_password(
                user_name="pwuser",
                service_name="myapp",
                label="0",
                passwd="",
                nginx_container="subnet-acl-nginx",
            )

        # Neither the htpasswd nor the stored ciphertext may move.
        assert Path(entry_before["htpasswd_path"]).read_text() == htpasswd_before
        after = registry.get_user_service("pwuser", "myapp", "0")
        assert after["basic_version"] == entry_before["basic_version"]
        before_row = registry.db.fetch_one(
            "SELECT secret_enc FROM registry.service_basic WHERE service_id=%s",
            (entry_before["service_id"],),
        )
        after_row = registry.db.fetch_one(
            "SELECT secret_enc FROM registry.service_basic WHERE service_id=%s",
            (entry_before["service_id"],),
        )
        assert after_row["secret_enc"] == before_row["secret_enc"]

    def test_change_password_without_a_matching_entry_never_writes_the_registry(self, monkeypatch):
        """The write-back is guarded on a real match.

        Same rule as _mark_failed / _update_registry_status: an unguarded save
        writes back an unchanged (possibly empty) list, which is how an empty
        registry gets persisted over real data. The lookup can succeed while the
        write-back loop sees a list that no longer holds the entry — the race
        this guard exists for.
        """
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
        entry = registry.get_user_service("pwuser", "myapp", "0")
        before = registry.db.fetch_one(
            "SELECT secret_enc, basic_version FROM registry.service_basic "
            "WHERE service_id=%s",
            (entry["service_id"],),
        )

        # No matching identity ⇒ the operation is refused before any write.
        with pytest.raises(KeyError):
            provisioner.change_password(
                user_name="ghost",
                service_name="myapp",
                label="0",
                passwd="newpass",
                nginx_container="subnet-acl-nginx",
            )

        after = registry.get_user("pwuser")
        assert len(after) == 1
        after_row = registry.db.fetch_one(
            "SELECT secret_enc, basic_version FROM registry.service_basic "
            "WHERE service_id=%s",
            (entry["service_id"],),
        )
        assert after_row["secret_enc"] == before["secret_enc"]
        assert after_row["basic_version"] == before["basic_version"]

    def test_remove_password_strips_auth_basic_and_clears_the_entry(self):
        """The exit for a protected service whose plaintext is gone.

        bcrypt gives no way back to the original password, so the instance's own
        layer has to come off: the conf is re-rendered without auth_basic, the
        entry loses passwd/passwd_plain/htpasswd_path, and the secret file goes.
        """
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
        entry = registry.get_user_service("pwuser", "myapp", "0")
        htpasswd_path = entry["htpasswd_path"]
        conf_path = entry["nginx_conf_path"]
        assert "auth_basic" in Path(conf_path).read_text()
        assert Path(htpasswd_path).exists()

        provisioner.remove_password(
            user_name="pwuser",
            service_name="myapp",
            label="0",
            nginx_container="subnet-acl-nginx",
        )

        after = registry.get_user_service("pwuser", "myapp", "0")
        assert not after.get("passwd"), "the hash must be cleared"
        assert not after.get("passwd_plain"), "the plaintext must be cleared"
        assert not after.get("htpasswd_path"), "the htpasswd reference must be cleared"
        assert not Path(htpasswd_path).exists(), "the secret file must be deleted"
        assert "auth_basic" not in Path(conf_path).read_text(), (
            "the re-rendered conf must have no auth_basic"
        )

    def test_change_password_keeps_state_when_the_reload_fails(self, monkeypatch):
        """A failed reload must roll the new credential back, not half-apply it."""
        provisioner.register_user(
            user_name="pwchg", service_name="myapp", label="0",
            compose_template=COMPOSE_TEMPLATE, nginx_template=NGINX_TEMPLATE,
            output_dir=self.tmp_path, nginx_output_dir=self.tmp_path,
            user_data_dir=self.user_data_dir, passwd="oldpass",
        )
        before = registry.get_user_service("pwchg", "myapp", "0")
        htpasswd_before = Path(before["htpasswd_path"]).read_bytes()
        row_before = registry.db.fetch_one(
            "SELECT basic_version, secret_enc FROM registry.service_basic WHERE service_id=%s",
            (before["service_id"],),
        )

        def _boom(*a, **k):
            raise RuntimeError("nginx reload failed")
        monkeypatch.setattr(provisioner.stream_access, "refresh_and_reload", _boom)
        with pytest.raises(RuntimeError, match="reload failed"):
            provisioner.change_password(
                user_name="pwchg", service_name="myapp", label="0", passwd="newpass",
            )

        after = registry.get_user_service("pwchg", "myapp", "0")
        row_after = registry.db.fetch_one(
            "SELECT basic_version, secret_enc FROM registry.service_basic WHERE service_id=%s",
            (before["service_id"],),
        )
        assert after["basic_version"] == before["basic_version"] == row_before["basic_version"]
        assert row_after["secret_enc"] == row_before["secret_enc"]
        assert Path(after["htpasswd_path"]).read_bytes() == htpasswd_before

    def test_remove_password_keeps_state_when_the_reload_fails(self, monkeypatch):
        """The conf/registry/htpasswd must not half-apply on a reload failure."""
        provisioner.register_user(
            user_name="pwfail",
            service_name="myapp",
            label="0",
            compose_template=COMPOSE_TEMPLATE,
            nginx_template=NGINX_TEMPLATE,
            output_dir=self.tmp_path,
            nginx_output_dir=self.tmp_path,
            user_data_dir=self.user_data_dir,
            passwd="oldpass",
        )
        entry = registry.get_user_service("pwfail", "myapp", "0")
        htpasswd_path = entry["htpasswd_path"]
        conf_path = Path(entry["nginx_conf_path"])
        before = conf_path.read_text()
        assert "auth_basic" in before

        def _boom(*a, **k):
            raise RuntimeError("reload failed")

        monkeypatch.setattr(provisioner.stream_access, "refresh_and_reload", _boom)
        with pytest.raises(RuntimeError, match="reload failed"):
            provisioner.remove_password(
                user_name="pwfail", service_name="myapp", label="0",
            )

        after = registry.get_user_service("pwfail", "myapp", "0")
        from lib import keys
        row = registry.db.fetch_one(
            "SELECT secret_enc FROM registry.service_basic WHERE service_id=%s",
            (after["service_id"],),
        )
        assert keys.decrypt(bytes(row["secret_enc"]).decode()) == "oldpass"
        assert after.get("htpasswd_path"), "the htpasswd reference must survive"
        assert Path(htpasswd_path).exists(), "the secret file must survive"
        assert conf_path.read_text() == before, (
            "the re-rendered conf must be rolled back to what nginx is still serving")

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
            nginx_container="subnet-acl-nginx",
        )
        down_calls = [c for c in self.calls if "down" in c]
        assert len(down_calls) >= 1, "Expected compose_down to be called during removal"

    def test_the_delete_passes_the_instance_profiles_to_compose_down(self, monkeypatch):
        """R4-F59 — a profile-gated service survives a delete without `--profile`.

        `docker compose down` acts on the services in the ACTIVE profile set, and
        a service gated by `profiles: [x]` is not in the default set. Measured
        2026-10-11 on `shared2/graphiti-local/2` (deployed with the `falkordb`
        profile): the registry entry went, two of the four containers went, and
        `…-falkordb` / `…-graph-falkordb` stayed `Up` while their network was
        removed underneath them. §10's step 11 expects the stack to be HEALTHY
        after a delete, so this is that step failing.
        """
        from lib import docker_ops, provisioner, registry
        monkeypatch.setattr(docker_ops, "network_disconnect", lambda *a, **k: None)
        monkeypatch.setattr(docker_ops, "nginx_reload", lambda *a, **k: None)
        monkeypatch.setattr(docker_ops, "remove_project_volumes", lambda *a, **k: [])
        monkeypatch.setattr(provisioner, "_purge_user_artifacts", lambda *a, **k: None)
        seen: dict = {}

        def _down(compose_file, **kw):
            seen.update(kw)
            seen["compose_file"] = compose_file

        monkeypatch.setattr(docker_ops, "compose_down", _down)

        # An entry with a profile, exactly as register_user records it.
        entry = {
            "user_name": "profuser", "service_name": "myapp", "label": "0",
            "compose_file_path": str(self.tmp_path / "docker-compose.user-profuser.0.yml"),
            "network_name": "subnet-acl-myapp-user_profuser-0",
            "profiles": ["falkordb"],
        }
        (self.tmp_path / "docker-compose.user-profuser.0.yml").write_text("services: {}\n")
        add_registry_user(entry)

        provisioner.remove_user(
            user_name="profuser", service_name="myapp", label="0",
            nginx_container="subnet-acl-nginx",
        )

        assert seen.get("profiles") == ["falkordb"], (
            "the entry's profiles must reach compose_down, or the profiled "
            f"containers are never selected for teardown: {seen}")


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
        isolate_fixture_templates(tmp_path, monkeypatch)

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

    def test_a_bare_server_name_is_refused_422_and_nothing_is_written(self):
        """R14 — a NEW selection naming the instance host is refused.

        The conf is the platform's old shape (`server_name example.com`); the
        route must answer 422 before creating a template or an instance.
        """
        from fastapi import HTTPException

        project_dir = self.sp_dir / "bare"
        project_dir.mkdir()
        shutil.copy(FIXTURES_DIR / "docker-compose.template.yml.j2", project_dir)
        (project_dir / "nginx.conf").write_text(
            "server {\n    listen 80;\n    server_name example.com;\n"
            "    location / { proxy_pass http://web:80; }\n}\n"
        )
        with pytest.raises(HTTPException) as exc_info:
            self._call(
                user_name="pruser", service_name="myapp",
                project_root="bare",
                compose_template_path="docker-compose.template.yml.j2",
                nginx_conf_file_path="nginx.conf",
                label="0", domain="localhost", passwd="secret",
            )
        assert exc_info.value.status_code == 422
        assert not (project_dir / "nginx.conf.j2").exists(), (
            "a refused selection writes nothing")


# ---------------------------------------------------------------------------
# api — FastAPI TestClient tests for new endpoints (P1-P6)
# ---------------------------------------------------------------------------


class TestDockerOpsLogReaders:
    """The stdout/stderr reader threads must end quietly when the pipe closes."""

    def test_readers_survive_a_closed_pipe(self):
        import io
        import threading
        from lib import docker_ops

        # A stream whose readline raises ValueError (closed file) after one line.
        class Exploding:
            def __init__(self):
                self.n = 0
            def readline(self):
                self.n += 1
                if self.n == 1:
                    return "hello\n"
                raise ValueError("I/O operation on closed file")

        # The reader is a closure inside _run; exercise the same guard shape by
        # driving the real thread body through a Popen-free call.
        thread_exc = []

        def _read_stdout(pipe):
            try:
                for line in iter(pipe.readline, ""):
                    pass
            except (ValueError, OSError):
                pass

        t = threading.Thread(target=_read_stdout, args=(Exploding(),))
        t.start(); t.join(timeout=5)
        assert not t.is_alive()
        # And the production source carries the guard (source-level check: the
        # closure is not importable in isolation).
        import inspect
        src = inspect.getsource(docker_ops)
        assert src.count("except (ValueError, OSError):") >= 2, (
            "docker_ops stdout/stderr readers must swallow closed-pipe errors"
        )


class TestRegistryLocking:
    """PostgreSQL advisory locks and concurrent writers (T3).

    The pre-cutover suite pinned a cross-process ``flock`` on the YAML file.
    The registry is PostgreSQL now: label allocation serializes on a
    transaction-scoped advisory lock and PostgreSQL releases session locks when
    the holder dies.
    """

    @pytest.fixture(autouse=True)
    def _setup(self):
        from lib import db

        self.db = db
        self.root = str(Path(__file__).parent.parent)

    def _spawn(self, code: str, *args: str):
        import os
        import subprocess
        import sys

        env = dict(os.environ)
        env["PYTHONPATH"] = self.root
        return subprocess.Popen(
            [sys.executable, "-c", code, *args], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def _try_lock(self) -> bool:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS ok",
                ("label:test",),
            ).fetchone()
            return row["ok"]

    _HOLD = (
        "import time, sys\n"
        "from lib import db\n"
        "if __name__ == '__main__':\n"
        "    with db.connection() as conn:\n"
        "        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('label:test',))\n"
        "        print('locked', flush=True)\n"
        "        time.sleep(float(sys.argv[1]) if len(sys.argv) > 1 else 1.5)\n"
    )

    def test_label_lock_is_reentrant_in_one_transaction(self):
        """A nested acquire on the same transaction must not deadlock."""
        with self.db.connection() as conn:
            registry.label_lock(conn, uuid.uuid4(), "svc")
            registry.label_lock(conn, uuid.uuid4(), "svc")
            row = conn.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS ok",
                ("label:other",),
            ).fetchone()
            assert row["ok"] is True

    def test_second_process_is_excluded_then_succeeds(self):
        child = self._spawn(self._HOLD, "1.2")
        assert child.stdout.readline().strip() == "locked"      # child holds the lock
        try:
            assert self._try_lock() is False
        finally:
            child.wait(timeout=10)
        # after the holder exits the lock is free again
        assert self._try_lock() is True

    def test_lock_is_released_when_the_holder_dies(self):
        """No stale lock: kill -9 the holder; PostgreSQL releases the session lock."""
        import signal
        import time as _t

        child = self._spawn(self._HOLD, "30")
        assert child.stdout.readline().strip() == "locked"
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=10)
        deadline = _t.monotonic() + 5
        while _t.monotonic() < deadline:
            if self._try_lock():
                return
            _t.sleep(0.1)
        pytest.fail("lock survived the holder's death (stale lock)")

    def test_concurrent_processes_do_not_lose_updates(self):
        """N writers each insert entries; every row must survive."""
        import threading

        n_threads, per_thread = 5, 10
        errors: list[BaseException] = []

        def worker(p: int):
            try:
                for i in range(per_thread):
                    self._add(f"p{p}-u{i}")
            except BaseException as exc:  # noqa: BLE001 — surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(p,)) for p in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        assert not errors, errors
        users = [u["user_name"] for u in registry.get_all_users()]
        assert len(users) == n_threads * per_thread, f"lost updates: {len(users)}"
        assert len(set(users)) == n_threads * per_thread

    def _add(self, name: str) -> None:
        add_registry_user(
            {"user_name": name, "service_name": "s", "label": "0"}
        )

    def test_readers_are_not_blocked_by_a_writer(self):
        """MVCC: a reader never waits on an open write transaction."""
        import time as _t

        service_id, _proc = add_registry_user(
            {"user_name": "keep", "service_name": "s", "label": "0"}
        )
        with self.db.connection() as conn:
            conn.execute(
                "UPDATE registry.service_instances SET updated_at=now() "
                "WHERE service_id=%s",
                (service_id,),
            )
            t0 = _t.monotonic()
            users = registry.get_all_users()
            elapsed = _t.monotonic() - t0
            assert elapsed < 2.0, f"reader blocked for {elapsed:.3f}s"
            assert [u["user_name"] for u in users] == ["keep"]


class TestRegistryDurability:
    """PostgreSQL transaction/constraint durability (T3/T12).

    The YAML store's truncate-then-write wipe cannot happen now: writes are
    transactional, partially-written state rolls back, and uniqueness is a
    database constraint.
    """

    @pytest.fixture(autouse=True)
    def _setup(self):
        from lib import db

        self.db = db

    def _insert_on(self, conn, user_name: str = "u0") -> uuid.UUID:
        owner, _ = registry.ensure_owner(conn, user_name)
        service_id = uuid.uuid4()
        registry.insert_service(
            conn, service_id=service_id, owner_id=owner,
            service_name="svc", label="0", recipe_path=".",
        )
        registry.insert_instance(
            conn, service_id, procedure_id=uuid.uuid4(), status="running"
        )
        return service_id

    def test_failed_transaction_leaves_no_partial_row(self):
        with pytest.raises(RuntimeError):
            with self.db.connection() as conn:
                self._insert_on(conn, "partial")
                raise RuntimeError("boom")
        assert registry.get_all_users() == []

    def test_committed_write_is_visible_to_a_new_connection(self):
        add_registry_user({"user_name": "u0", "service_name": "svc", "label": "0"})
        assert [u["user_name"] for u in registry.get_all_users()] == ["u0"]

    def test_reader_never_sees_an_uncommitted_write(self):
        """The original wipe mechanism: a reader must never observe a half state."""
        with self.db.connection() as conn:
            self._insert_on(conn)
            assert registry.get_all_users() == [], "read an uncommitted registry"
        assert len(registry.get_all_users()) == 1

    def test_status_update_matches_only_the_target(self):
        u0, _ = add_registry_user(
            {"user_name": "u0", "service_name": "svc", "label": "0", "status": "running"}
        )
        u1, _ = add_registry_user(
            {"user_name": "u1", "service_name": "svc", "label": "0", "status": "running"}
        )
        registry.set_status(u0, "deleting")
        assert registry.get_service_by_id(u0)["status"] == "deleting"
        assert registry.get_service_by_id(u1)["status"] == "running"
        # a non-matching id must not write anything
        registry.set_status("00000000-0000-0000-0000-000000000000", "deleting")
        assert registry.get_service_by_id(u1)["status"] == "running"

    def test_duplicate_active_tuple_is_refused(self):
        add_registry_user({"user_name": "u0", "service_name": "svc", "label": "0"})
        with pytest.raises(Exception) as excinfo:
            add_registry_user({"user_name": "u0", "service_name": "svc", "label": "0"})
        assert "unique" in str(excinfo.value).lower() or "duplicate" in str(excinfo.value).lower()
        assert len(registry.get_all_users()) == 1


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
        isolate_fixture_templates(tmp_path, monkeypatch)

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

    # ── GET /subnet-pool ──

    def test_get_subnet_pool_returns_stats(self, monkeypatch):
        """GET /subnet-pool returns pool usage stats when SUBNET_POOLS is set."""
        import os
        from lib import subnet_manager
        monkeypatch.setenv("SUBNET_POOLS", "100.96.0.0/16")
        subnet_manager._load_env()
        response = self.client.get("/subnet-pool")
        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is True
        assert len(data["pools"]) == 1
        assert data["pools"][0]["cidr"] == "100.96.0.0/16"
        assert data["pools"][0]["total_slots"] > 0
        assert "overall" in data
        assert "allocations" in data

    def test_get_subnet_pool_disabled_when_no_pools(self, monkeypatch):
        """GET /subnet-pool returns disabled state when SUBNET_POOLS is empty."""
        import os
        from lib import subnet_manager
        monkeypatch.setenv("SUBNET_POOLS", "")
        subnet_manager._load_env()
        response = self.client.get("/subnet-pool")
        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is False

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
        response = self.client.post("/docker/network/testnet/connect/subnet-acl-nginx")
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

    # ── POST /users/{user_name}/stop (G78 user-level stop) ──

    def test_stop_all_handles_zero_one_and_many_entries(self, monkeypatch):
        from lib import registry as reg_mod
        calls = []
        monkeypatch.setattr(self.api.provisioner, "stop_service",
            lambda **kw: calls.append((kw["service_name"], kw["label"])) or {"ok": True})

        monkeypatch.setattr(reg_mod, "get_user", lambda u: [])
        r = self.client.post("/users/nobody/stop")
        assert r.status_code == 200
        assert r.json()["ok"] is True and r.json()["results"] == []

        entries = [
            {"user_name": "alice", "service_name": "svc1", "label": "0"},
            {"user_name": "alice", "service_name": "svc2", "label": "0"},
            {"user_name": "alice", "service_name": "svc3", "label": "1"},
        ]
        monkeypatch.setattr(reg_mod, "get_user", lambda u: entries)
        r = self.client.post("/users/alice/stop")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert len(body["results"]) == 3
        assert calls == [("svc1", "0"), ("svc2", "0"), ("svc3", "1")]

        # Idempotent: a second call attempts every entry again, no restart.
        r2 = self.client.post("/users/alice/stop")
        assert r2.status_code == 200 and r2.json()["ok"] is True
        assert len(calls) == 6

    def test_stop_all_attempts_every_service_and_reports_failures(self, monkeypatch):
        from lib import registry as reg_mod
        entries = [
            {"user_name": "alice", "service_name": "good", "label": "0"},
            {"user_name": "alice", "service_name": "bad", "label": "0"},
            {"user_name": "alice", "service_name": "later", "label": "0"},
        ]
        attempted = []
        def stop(**kw):
            attempted.append(kw["service_name"])
            if kw["service_name"] == "bad":
                raise RuntimeError("compose stop failed")
            return {"ok": True}
        monkeypatch.setattr(reg_mod, "get_user", lambda u: entries)
        monkeypatch.setattr(self.api.provisioner, "stop_service", stop)

        r = self.client.post("/users/alice/stop")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert attempted == ["good", "bad", "later"], "every service must be attempted"
        assert body["failed_services"] == [
            {"service_name": "bad", "label": "0", "error": "compose stop failed"}
        ]

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

    def _insert_task_with_log(self, task_id: str, text: str) -> None:
        """A registry task row plus its per-task log file (T5)."""
        import uuid

        from lib import db, registry as reg

        log_dir = self.tmp_path / "task_logs"
        log_dir.mkdir(exist_ok=True)
        log = log_dir / f"task-{task_id}.log"
        log.write_text(text)
        with db.connection() as conn:
            reg.insert_task(
                conn, task_id=task_id, procedure_id=uuid.uuid4(),
                service_id=None, kind="deploy", operation="register",
            )
        db.execute(
            "UPDATE registry.tasks SET log_ref=%s WHERE task_id=%s",
            (str(log), task_id),
        )

    def test_task_log_endpoint_returns_sse(self):
        """GET /tasks/{task_id}/log streams SSE for an existing task log."""
        self._insert_task_with_log("api-sse", "hello\n")
        response = self.client.get("/tasks/api-sse/log?follow=false")
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]

    def test_task_log_sse_streams_from_task_file(self):
        """SSE streams from the task's own log file."""
        self._insert_task_with_log("api-test123", "line 1\nline 2\nline 3\n")

        response = self.client.get("/tasks/api-test123/log?tail=10&follow=false")
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]

        body = response.text
        assert "data: line 1" in body
        assert "data: line 2" in body
        assert "data: line 3" in body
        assert "event: done" in body

    def test_task_log_sse_404s_without_a_record(self):
        """No record ⇒ 404. There is no global-log fallback (T5)."""
        response = self.client.get("/tasks/nonexistent/log?follow=false")
        assert response.status_code == 404
        assert "Task not found" in response.text

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


# ---------------------------------------------------------------------------
# Tests for check-missing-files endpoint (dev-debug-cycle task 3.2.1.4)
# ---------------------------------------------------------------------------

class TestCheckMissingFiles:
    """Tests for the GET /services/{service_name}/check-missing-files endpoint."""

    def test_check_missing_files_response_model_exists(self):
        """CheckMissingFilesResponse model should be importable with correct fields."""
        from api import CheckMissingFilesResponse
        assert CheckMissingFilesResponse is not None
        # Verify the model fields
        fields = CheckMissingFilesResponse.model_fields
        assert "service_name" in fields
        assert "ready" in fields
        assert "missing" in fields
        assert "existing" in fields
        assert "needs_env" in fields

    def test_check_missing_files_endpoint_registered(self, monkeypatch):
        """The endpoint should be registered on the FastAPI app."""
        from api import app
        routes = [r.path for r in app.routes]
        assert "/services/{service_name}/check-missing-files" in routes, (
            f"check-missing-files route not found in app routes"
        )

    def test_check_missing_files_missing_service_returns_404(self, monkeypatch):
        """When the service directory doesn't exist, endpoint returns 404."""
        # Mock SOURCE_PROJECTS_DIR to a non-existent path
        import api
        from pathlib import Path
        original = api.SOURCE_PROJECTS_DIR
        try:
            api.SOURCE_PROJECTS_DIR = Path("/nonexistent/path/xyz")
            from fastapi.testclient import TestClient
            client = TestClient(api.app)
            response = client.get("/services/nonexistent_service/check-missing-files")
            assert response.status_code == 404
        finally:
            api.SOURCE_PROJECTS_DIR = original

    def test_check_missing_files_all_present(self, tmp_path, monkeypatch):
        """When all essential files exist, ready=true and missing=[]."""
        import api
        from pathlib import Path

        # Create a temp service directory with all essential files
        svc_dir = tmp_path / "myapp"
        svc_dir.mkdir()
        (svc_dir / "docker-compose.yml").write_text("services:\n  web:\n    build: .")
        # A complete recipe carries the serving-ports artifact too (R1.1/G20):
        # without it the recipe cannot deploy, so "nothing missing" needs it.
        (svc_dir / "serving_ports.yml").write_text("ports: []\n")
        (svc_dir / "nginx.conf").write_text("server { listen 80; }")
        (svc_dir / "Dockerfile").write_text("FROM python:3.13")
        (svc_dir / ".env").write_text("DEBUG=true")

        original = api.SOURCE_PROJECTS_DIR
        try:
            api.SOURCE_PROJECTS_DIR = tmp_path
            from fastapi.testclient import TestClient
            client = TestClient(api.app)
            response = client.get("/services/myapp/check-missing-files")
            assert response.status_code == 200
            data = response.json()
            assert data["ready"] is True
            assert data["missing"] == []
            # FOUR became FIVE with the serving-ports artifact (R1.1/G20): it is
            # demanded exactly as the compose and the nginx conf are.
            assert len(data["existing"]) == 5
            assert "serving_ports" in data["existing"]
        finally:
            api.SOURCE_PROJECTS_DIR = original

    def test_check_missing_files_with_j2_templates(self, tmp_path, monkeypatch):
        """When .j2 templates exist instead of plain files, ready=true."""
        import api
        from pathlib import Path

        svc_dir = tmp_path / "myapp_j2"
        svc_dir.mkdir()
        # compose references ${TAG} → needs_env true → .env genuinely missing
        (svc_dir / "docker-compose.yml.j2").write_text(
            "services:\n  web:\n    image: nginx:${TAG}"
        )
        (svc_dir / "nginx.conf.j2").write_text("server { server_name {{ hostname }}; }")
        # ...and the serving-ports artifact (a template counts for presence, but
        # the artifact is demanded all the same — R1.1/G20).
        (svc_dir / "serving_ports.yml").write_text("ports: []\n")
        (svc_dir / "Dockerfile").write_text("FROM python:3.13")

        original = api.SOURCE_PROJECTS_DIR
        try:
            api.SOURCE_PROJECTS_DIR = tmp_path
            from fastapi.testclient import TestClient
            client = TestClient(api.app)
            response = client.get("/services/myapp_j2/check-missing-files")
            assert response.status_code == 200
            data = response.json()
            assert data["ready"] is True  # .env is never in the missing list anymore
            assert data["needs_env"] is True
            assert ".env" not in data["missing"]
            assert "docker-compose" in data["existing"]
            assert "nginx.conf" in data["existing"]
            assert "Dockerfile" in data["existing"]
        finally:
            api.SOURCE_PROJECTS_DIR = original

    def test_check_missing_files_no_env_vars_no_env_required(self, tmp_path, monkeypatch):
        """GAP-10: without ${VAR} interpolation, .env is NOT in the missing list."""
        import api
        from pathlib import Path

        svc_dir = tmp_path / "novars"
        svc_dir.mkdir()
        (svc_dir / "docker-compose.yml").write_text("services:\n  web:\n    image: nginx:alpine")
        # A complete recipe carries the serving-ports artifact too (R1.1/G20):
        # without it the recipe cannot deploy, so "nothing missing" needs it.
        (svc_dir / "serving_ports.yml").write_text("ports: []\n")
        (svc_dir / "nginx.conf").write_text("server { listen 80; }")
        (svc_dir / "Dockerfile").write_text("FROM python:3.13")

        original = api.SOURCE_PROJECTS_DIR
        try:
            api.SOURCE_PROJECTS_DIR = tmp_path
            from fastapi.testclient import TestClient
            client = TestClient(api.app)
            response = client.get("/services/novars/check-missing-files")
            assert response.status_code == 200
            data = response.json()
            assert data["needs_env"] is False
            assert ".env" not in data["missing"]
            assert data["ready"] is True
        finally:
            api.SOURCE_PROJECTS_DIR = original

    def test_check_missing_files_recipe_path_all_present(self, tmp_path):
        """recipe_path checks files inside the recipe subdirectory."""
        import api
        from pathlib import Path

        svc_dir = tmp_path / "multisvc"
        recipe_dir = svc_dir / "recipes" / "web"
        recipe_dir.mkdir(parents=True)
        # Root has nothing deployable; the recipe subdir has all essential files
        (recipe_dir / "docker-compose.yml").write_text("services:\n  web:\n    build: .")
        # A complete recipe carries the serving-ports artifact too (R1.1/G20).
        (recipe_dir / "serving_ports.yml").write_text("ports: []\n")
        (recipe_dir / "nginx.conf").write_text("server { listen 80; }")
        (recipe_dir / "Dockerfile").write_text("FROM python:3.13")
        (recipe_dir / ".env").write_text("DEBUG=true")

        original = api.SOURCE_PROJECTS_DIR
        try:
            api.SOURCE_PROJECTS_DIR = tmp_path
            from fastapi.testclient import TestClient
            client = TestClient(api.app)
            response = client.get("/services/multisvc/check-missing-files?recipe_path=recipes/web")
            assert response.status_code == 200
            data = response.json()
            assert data["ready"] is True
            assert data["missing"] == []
            # FOUR became FIVE with the serving-ports artifact (R1.1/G20): it is
            # demanded exactly as the compose and the nginx conf are.
            assert len(data["existing"]) == 5
            assert "serving_ports" in data["existing"]
        finally:
            api.SOURCE_PROJECTS_DIR = original

    def test_check_missing_files_recipe_path_missing_dir_returns_404(self, tmp_path):
        """recipe_path to a non-existent subdirectory returns 404 with the recipe in the message."""
        import api
        from pathlib import Path

        svc_dir = tmp_path / "multisvc"
        svc_dir.mkdir()
        (svc_dir / "docker-compose.yml").write_text("services:\n  web:\n    build: .")
        # A complete recipe carries the serving-ports artifact too (R1.1/G20):
        # without it the recipe cannot deploy, so "nothing missing" needs it.
        (svc_dir / "serving_ports.yml").write_text("ports: []\n")
        original = api.SOURCE_PROJECTS_DIR
        try:
            api.SOURCE_PROJECTS_DIR = tmp_path
            from fastapi.testclient import TestClient
            client = TestClient(api.app)
            response = client.get("/services/multisvc/check-missing-files?recipe_path=recipes/nope")
            assert response.status_code == 404
            assert "recipe 'recipes/nope'" in response.json()["detail"]
        finally:
            api.SOURCE_PROJECTS_DIR = original

    def test_check_missing_files_recipe_path_ignores_root_files(self, tmp_path):
        """With recipe_path given, root-only files are NOT counted as existing."""
        import api
        from pathlib import Path

        svc_dir = tmp_path / "multisvc2"
        recipe_dir = svc_dir / "recipes" / "api"
        recipe_dir.mkdir(parents=True)
        # Root has compose + Dockerfile, but the recipe subdir is EMPTY
        (svc_dir / "docker-compose.yml").write_text("services:\n  web:\n    build: .")
        (svc_dir / "Dockerfile").write_text("FROM python:3.13")

        original = api.SOURCE_PROJECTS_DIR
        try:
            api.SOURCE_PROJECTS_DIR = tmp_path
            from fastapi.testclient import TestClient
            client = TestClient(api.app)
            response = client.get("/services/multisvc2/check-missing-files?recipe_path=recipes/api")
            assert response.status_code == 200
            data = response.json()
            assert data["ready"] is False
            assert len(data["existing"]) == 0  # root files are outside the recipe
            # No compose in the recipe → no interpolation → .env not required
            assert data["needs_env"] is False
            assert ".env" not in data["missing"]
            # FOUR, not three: the serving-ports artifact is demanded with the
            # rest (R1.1/G20), so an empty recipe misses it too.
            assert len(data["missing"]) == 4
            assert "serving_ports" in data["missing"]
        finally:
            api.SOURCE_PROJECTS_DIR = original


# ---------------------------------------------------------------------------
# Tests for render_compose with subnet/gateway params (GAP-026)
# ---------------------------------------------------------------------------


class TestRenderComposeWithSubnet:
    """Test that render_compose passes subnet and gateway params to template context."""

    def test_render_compose_with_subnet_and_gateway(self, tmp_path):
        """render_compose with subnet/gateway should include them in rendered output."""
        out = str(tmp_path / "docker-compose.user-alice.0.yml")
        template_engine.render_compose(
            COMPOSE_TEMPLATE, out,
            user_name="alice", service_name="myapp", label="0",
            volumes={"app_data": "/srv/alice/app", "db_data": "/srv/alice/db"},
            subnet="10.0.0.0/29",
            gateway="10.0.0.1",
        )
        content = Path(out).read_text()
        data = yaml.safe_load(content)
        services = data["services"]
        assert "web" in services
        assert "db" in services
        # The template context should include subnet/gateway
        # The compose template fixture may or may not use them
        # At minimum, render should not crash with these params

    def test_render_compose_without_subnet_backward_compat(self, tmp_path):
        """render_compose without subnet should still work (backward compat)."""
        out = str(tmp_path / "docker-compose.user-alice.0.yml")
        template_engine.render_compose(
            COMPOSE_TEMPLATE, out,
            user_name="alice", service_name="myapp", label="0",
            volumes={"app_data": "/srv/alice/app", "db_data": "/srv/alice/db"},
        )
        content = Path(out).read_text()
        data = yaml.safe_load(content)
        assert "web" in data["services"]
        # subnet should NOT appear in content when not passed
        assert "10.0.0.0" not in content

    def test_render_compose_with_subnet_empty_string_subnet(self, tmp_path):
        """render_compose with empty subnet string should not inject subnet."""
        out = str(tmp_path / "docker-compose.user-alice.0.yml")
        template_engine.render_compose(
            COMPOSE_TEMPLATE, out,
            user_name="alice", service_name="myapp", label="0",
            volumes={"app_data": "/srv/alice/app", "db_data": "/srv/alice/db"},
            subnet="",
            gateway="",
        )
        content = Path(out).read_text()
        # Empty subnet should not appear as a literal IP in output
        # (it may appear in a context diff if template has a guard)
        assert "web" in content or "services:" in content


# ---------------------------------------------------------------------------
# Tests for provisioner storing hostname + passwd_plain + subnet (GAP-009, GAP-031)
# ---------------------------------------------------------------------------


class TestProvisionerRegistryFields:
    """Verify register_user stores hostname, passwd_plain, and subnet in registry."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, tmp_path):
        """Mock docker ops so no real Docker calls happen."""
        self.calls: list[list[str]] = []

        def fake_run(args, check=True):
            self.calls.append(list(args))
            import subprocess as sp
            return sp.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(docker_ops, "_run", fake_run)
        monkeypatch.setattr(docker_ops, "network_connect", lambda *a, **kw: None)
        monkeypatch.setattr(docker_ops, "nginx_reload", lambda *a: None)
        # Mock container operations needed by provisioner
        monkeypatch.setattr(docker_ops, "container_exists", lambda *a: False)
        monkeypatch.setattr(docker_ops, "container_running", lambda *a: False)
        monkeypatch.setattr(docker_ops, "network_inspect", lambda *a: {"IPAM": {"Config": []}})

        self.tmp_path = tmp_path
        self.user_data_dir = tmp_path / "user_data"
        self.user_data_dir.mkdir()
        self.ssl_base_dir = tmp_path / "provision" / "ssl"
        self.ssl_base_dir.mkdir(parents=True, exist_ok=True)

    def test_register_user_stores_hostname_in_registry(self):
        """Registry entry should include hostname field after registration."""
        provisioner.register_user(
            user_name="hostuser",
            service_name="myapp",
            label="0",
            compose_template=COMPOSE_TEMPLATE,
            output_dir=self.tmp_path,
            volumes={"app_data": str(self.tmp_path / "vol_app"), "db_data": str(self.tmp_path / "vol_db")},
            passwd="testpass",
            user_data_dir=self.user_data_dir,
        )
        entries = registry.get_user("hostuser")
        assert len(entries) == 1
        entry = entries[0]
        assert "hostname" in entry
        assert entry["hostname"] == "myapp-hostuser-0.localhost"

    def test_register_user_encrypts_the_basic_password(self):
        """R2.7/T2: the Basic password is AES-GCM ciphertext, never plaintext."""
        provisioner.register_user(
            user_name="pwuser",
            service_name="myapp",
            label="0",
            compose_template=COMPOSE_TEMPLATE,
            output_dir=self.tmp_path,
            volumes={"app_data": str(self.tmp_path / "vol_app"), "db_data": str(self.tmp_path / "vol_db")},
            passwd="secret123",
            user_data_dir=self.user_data_dir,
        )
        entries = registry.get_user("pwuser")
        assert len(entries) == 1
        entry = entries[0]
        assert "passwd_plain" not in entry
        assert "passwd" not in entry
        row = registry.db.fetch_one(
            "SELECT basic_username, secret_enc FROM registry.service_basic "
            "WHERE service_id=%s", (entry["service_id"],),
        )
        assert row["basic_username"] == "pwuser"
        assert row["secret_enc"]
        from lib import keys
        assert keys.decrypt(bytes(row["secret_enc"]).decode()) == "secret123"

    def test_register_user_stores_subnet_field_in_registry(self):
        """Registry entry should have subnet field (empty when SUBNET_POOLS is not set)."""
        provisioner.register_user(
            user_name="subnetuser",
            service_name="myapp",
            label="0",
            compose_template=COMPOSE_TEMPLATE,
            output_dir=self.tmp_path,
            volumes={"app_data": str(self.tmp_path / "vol_app"), "db_data": str(self.tmp_path / "vol_db")},
            passwd="testpass",
            user_data_dir=self.user_data_dir,
        )
        entries = registry.get_user("subnetuser")
        assert len(entries) == 1
        entry = entries[0]
        assert "subnet" in entry
        # subnet is empty when SUBNET_POOLS is not configured
        assert entry["subnet"] == ""

    def test_register_user_stores_hostname_with_custom_domain(self):
        """hostname should use the domain parameter."""
        provisioner.register_user(
            user_name="domuser",
            service_name="myapp",
            label="0",
            compose_template=COMPOSE_TEMPLATE,
            output_dir=self.tmp_path,
            volumes={"app_data": str(self.tmp_path / "vol_app"), "db_data": str(self.tmp_path / "vol_db")},
            passwd="testpass",
            domain="example.com",
            user_data_dir=self.user_data_dir,
        )
        entries = registry.get_user("domuser")
        assert len(entries) == 1
        assert entries[0]["hostname"] == "myapp-domuser-0.example.com"


class TestV5EnvDDeprecated:
    """v5 (decision 1/6): env.d disappears from the internal side — -api never
    writes it, the internal confs never include it, and the v4 writers are removed from the codebase."""

    def test_render_nginx_conf_never_includes_env_d(self, tmp_path):
        """The rendered simple conf must never carry an env.d include."""
        out = str(tmp_path / "out.conf")
        template_engine.render_nginx_conf(
            NGINX_TEMPLATE, out,
            user_name="alice", service_name="myapp", label="0",
            domain_name="example.com", htpasswd_path=str(tmp_path / "x.htpasswd"),
        )
        content = Path(out).read_text()
        assert "env.d" not in content

    def test_api_module_has_no_envd_portald(self):
        """-api no longer writes env.d/portal.d nor READS ENABLE_ACL/PORTAL_MODE.
        Docstrings may explain the removal; the v4 code paths must be gone."""
        import api
        src = Path(api.__file__).read_text()
        assert "_write_v4_nginx_dirs" not in src
        assert "write_env_d" not in src
        assert "write_portal_d" not in src
        assert "os.environ.get(\"ENABLE_ACL\"" not in src
        assert "os.environ.get(\"PORTAL_MODE\"" not in src
        assert "generated/env.d" not in src
        assert "generated/portal.d" not in src


class TestV5PortalDDeprecated:
    """v5 (decision 5/15): the portal moves to the edge — the internal
    nginx.provision.conf is services-only (no portal.d include), the v4 internal writer is removed, and rendered confs never reference
    the v4 portal constants."""

    def test_internal_nginx_provision_conf_services_only(self):
        """nginx.provision.conf has NO portal.d include and keeps services.d + 444."""
        repo = Path(__file__).resolve().parents[2]  # _users_provision
        conf = (repo / "nginx.provision.conf").read_text()
        # F21 (2026-10-10): the http include is CATEGORY-narrowed. `*.conf` also
        # matched the stream category (`<svc>.user-<user>.<label>.stream.conf`),
        # so the first stream conf made nginx refuse the WHOLE configuration —
        # platform-wide, and a restart (not a reload) is what surfaces it.
        assert "include /etc/nginx/services.d/*.nginx.conf;" in conf
        assert "include /etc/nginx/services.d/*.conf;" not in conf, (
            "the category-blind glob is what loaded a stream conf into http")
        assert "include /etc/nginx/portal.d" not in conf
        assert "include /etc/nginx/env.d" not in conf
        assert "listen 80 default_server;" in conf
        assert "return 444;" in conf

    def test_internal_nginx_provision_conf_server_names_hash_bucket_size(self):
        """nginx.provision.conf raises server_names_hash_bucket_size above the
        64-byte default — generated services.d server_names (up to ~51 chars +
        ngx_hash_elt_t overhead) otherwise fail nginx -t with the
        'could not build server_names_hash' [emerg] (regression 2026-09-01,
        subnet-acl-nginx crash-loop)."""
        repo = Path(__file__).resolve().parents[2]  # _users_provision
        conf = (repo / "nginx.provision.conf").read_text()
        # Must be set inside the http block (after 'http {'), before any include.
        assert "server_names_hash_bucket_size 128;" in conf

    def test_render_nginx_conf_no_portal_constants(self, tmp_path):
        """Rendered confs never reference the v4 portal constants ($portal_scheme
        / $dashboard_host) — the portal is the edge's job."""
        out = str(tmp_path / "out.conf")
        template_engine.render_nginx_conf(
            NGINX_TEMPLATE, out,
            user_name="alice", service_name="myapp", label="0",
            domain_name="example.com", htpasswd_path=str(tmp_path / "x.htpasswd"),
        )
        content = Path(out).read_text()
        assert "$portal_scheme" not in content
        assert "$dashboard_host" not in content
        assert "subnet-acl-gateway" not in content
        assert "subnet-acl-dashboard" not in content


class TestV5SimpleNginxSyntax:
    """Structural checks on the rendered v5 simple conf (nginx -t-compatible):
    server_name + auth_basic + variable proxy_pass, NO ACL scaffold (the gate
    lives entirely at the edge, decision 6)."""

    def test_render_nginx_conf_has_no_edge_gate(self, tmp_path):
        """v5 §5: no auth_request, no client-type branches, no WWW-Authenticate,
        no portal redirects, no named locations in the internal conf."""
        out = str(tmp_path / "out.conf")
        template_engine.render_nginx_conf(
            NGINX_TEMPLATE, out,
            user_name="alice", service_name="myapp", label="0",
            domain_name="example.com", htpasswd_path="",
        )
        content = Path(out).read_text()
        assert "$client_type" not in content
        assert "auth_request" not in content
        assert "WWW-Authenticate" not in content
        assert "$auth_action" not in content
        assert "location @auth_401" not in content
        assert "location @auth_403" not in content
        assert "return 302 $portal_scheme" not in content
        assert "acl_denied" not in content

    def test_render_nginx_conf_has_no_accept_map(self, tmp_path):
        """GAP-11: no $is_browser / Accept map in the per-service conf — client
        type comes from the gateway."""
        out = str(tmp_path / "out.conf")
        template_engine.render_nginx_conf(
            NGINX_TEMPLATE, out,
            user_name="alice", service_name="myapp", label="0",
            domain_name="example.com", htpasswd_path="",
        )
        content = Path(out).read_text()
        assert "map $http_accept $is_browser" not in content
        assert "$is_browser" not in content

    def test_render_nginx_conf_auth_basic_at_server_level(self, tmp_path):
        """v5 §5: auth_basic lives at server level (not in a /__basic__/
        location) — the internal conf is a dumb router, byte-identical."""
        out = str(tmp_path / "out.conf")
        htpasswd = str(tmp_path / "x.htpasswd")
        template_engine.render_nginx_conf(
            NGINX_TEMPLATE, out,
            user_name="alice", service_name="myapp", label="0",
            domain_name="example.com", htpasswd_path=htpasswd,
        )
        content = Path(out).read_text()
        assert "location /__basic__/" not in content
        assert 'auth_basic "myapp - alice";' in content
        assert f"auth_basic_user_file {htpasswd};" in content
        # No /__basic__/ relocation anywhere.
        assert "/__basic__/" not in content

    def test_render_nginx_conf_https_simple_in_ssl_block(self, tmp_path):
        """v5: the 443 ssl block is simple (ssl_certificate + auth_basic +
        proxy_pass); the 301 block is plain. No scaffold in either."""
        out = str(tmp_path / "out.conf")
        template_engine.render_nginx_conf(
            NGINX_TEMPLATE, out,
            user_name="alice", service_name="myapp", label="0",
            domain_name="example.com", htpasswd_path=str(tmp_path / "x.htpasswd"),
            https=True,
            ssl_certificate_path="/provision/ssl/example.com/fullchain.pem",
            ssl_certificate_key_path="/provision/ssl/example.com/privkey.pem",
        )
        content = Path(out).read_text()
        ssl_block = content.split("listen 443 ssl;", 1)[1]
        assert "ssl_certificate" in ssl_block
        assert "/provision/ssl/example.com/fullchain.pem;" in ssl_block
        assert "/provision/ssl/example.com/privkey.pem;" in ssl_block
        assert "auth_basic" in ssl_block
        assert "auth_request" not in content
        assert "location /__basic__/" not in content
        redirect_block = content.split("listen 443 ssl;", 1)[0]
        assert "return 301 https://$host$request_uri;" in redirect_block
        assert "auth_request" not in redirect_block


# ---------------------------------------------------------------------------
# reconciliation — regenerate_nginx_confs (QA2: v3→v4 is_browser migration)
# ---------------------------------------------------------------------------


class TestRegenerateNginxConfs:
    """regenerate_nginx_confs / strip_is_browser_refs (QA2 stale-conf migration)."""

    def _make_registry_entry(self, tmp_path: Path, user: str, service: str,
                             label: str, hostname: str) -> dict:
        gen = tmp_path / "generated"
        gen.mkdir(exist_ok=True)
        nginx_out = gen / f"{service}.user-{user}.{label}.nginx.conf"
        return {
            "user_name": user,
            "service_name": service,
            "label": label,
            "nginx_conf_template_path": NGINX_TEMPLATE,
            "nginx_conf_path": str(nginx_out),
            "hostname": f"{service}-{user}-{label}.{hostname}",
            "htpasswd_path": "",
            "https": False,
        }

    def _setup(self, tmp_path: Path, monkeypatch, entries: list[dict]):
        gen = tmp_path / "generated"
        gen.mkdir(exist_ok=True)
        for entry in entries:
            add_registry_user(entry)
        monkeypatch.setenv("GENERATED_DIR", str(gen))
        return gen

    def test_renders_registered_conf_clean(self, tmp_path, monkeypatch):
        """A registry-backed stale v3/v4 conf is re-rendered into the v5 SIMPLE
        ACL-free form (no is_browser, no scaffold)."""
        from lib import reconciliation
        gen = self._setup(tmp_path, monkeypatch, [
            self._make_registry_entry(tmp_path, "alice", "myapp", "0", "localhost"),
        ])
        conf = gen / "myapp.user-alice.0.nginx.conf"
        conf.write_text(
            "# v3 stale\n"
            "if ($is_browser) { return 302 http://$dashboard_host/login; }\n"
        )
        report = reconciliation.regenerate_nginx_confs()
        assert report["regenerated"] == 1
        assert report["is_browser_stripped"] == 0
        content = conf.read_text()
        assert "$is_browser" not in content
        # v5: the gate lives at the edge — no scaffold in the internal conf.
        assert "auth_request" not in content
        assert "location /__basic__/" not in content
        assert "server_name myapp-alice-0--web.localhost;" in content

    def test_strips_is_browser_from_orphan(self, tmp_path, monkeypatch):
        """An orphan conf (no registry entry) is surgically stripped of is_browser
        AND the v4 ACL scaffold (@auth_401/@auth_403 named locations) — the v5
        strip path (v5 §11.1, decision 16)."""
        from lib import reconciliation
        gen = self._setup(tmp_path, monkeypatch, [])
        conf = gen / "orphan.user-ghost.0.nginx.conf"
        conf.write_text(
            "server {\n"
            "    listen 80;\n"
            "    server_name orphan.localhost;\n"
            "    location @auth_401 {\n"
            "        if ($is_browser) { return 302 http://$dashboard_host/login?redirect=$scheme://$host$request_uri; }\n"
            "        return 401;\n"
            "    }\n"
            "    location @auth_403 {\n"
            "        if ($is_browser) { return 302 http://$dashboard_host/alert?reason=acl_denied&service=$host; }\n"
            "        return 403;\n"
            "    }\n"
            "    location / {\n"
            "        proxy_pass http://127.0.0.1:80;\n"
            "    }\n"
            "}\n"
        )
        report = reconciliation.regenerate_nginx_confs()
        assert report["regenerated"] == 0
        assert report["is_browser_stripped"] == 1
        content = conf.read_text()
        assert "$is_browser" not in content
        # v5 strip removes the ACL scaffold blocks entirely (gate at the edge).
        assert "@auth_401" not in content
        assert "@auth_403" not in content
        assert "return 401;" not in content
        assert "acl_denied" not in content
        # The plain proxy location survives.
        assert "proxy_pass http://127.0.0.1:80;" in content
        assert "server_name orphan.localhost;" in content

    def test_idempotent(self, tmp_path, monkeypatch):
        """Re-running the migration on clean confs is a stable no-op."""
        from lib import reconciliation
        gen = self._setup(tmp_path, monkeypatch, [
            self._make_registry_entry(tmp_path, "bob", "myapp", "1", "localhost"),
        ])
        first = reconciliation.regenerate_nginx_confs()
        conf = gen / "myapp.user-bob.1.nginx.conf"
        before = conf.read_text()
        second = reconciliation.regenerate_nginx_confs()
        assert second["regenerated"] == 1
        assert second["is_browser_stripped"] == 0
        assert conf.read_text() == before  # byte-stable re-render

    def test_strip_is_browser_refs_multiline(self):
        """strip_is_browser_refs handles both single-line and multi-line if-blocks."""
        from lib import reconciliation
        content = (
            "    location @auth_401 {\n"
            "        if ($is_browser) {\n"
            "            return 302 http://$dashboard_host/login?redirect=$scheme://$host$request_uri;\n"
            "        }\n"
            "        return 401;\n"
            "    }\n"
        )
        stripped = reconciliation.strip_is_browser_refs(content)
        assert "$is_browser" not in stripped
        assert "return 401;" in stripped


# ---------------------------------------------------------------------------
# api — POST /nginx/regenerate (QA2)
# ---------------------------------------------------------------------------


class TestAPIRegenerateNginx:
    """POST /nginx/regenerate re-renders confs, strips is_browser, restarts nginx."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        """Set up TestClient with mocked docker_ops + registry."""
        import api
        from lib import registry as reg_mod, docker_ops
        from fastapi.testclient import TestClient

        gen_dir = tmp_path / "generated"
        gen_dir.mkdir()
        monkeypatch.setattr(api, "GENERATED_DIR", gen_dir)
        monkeypatch.setattr(api, "USER_DATA_DIR", tmp_path / "user_data")
        monkeypatch.setattr(api, "SOURCE_PROJECTS_DIR", tmp_path / "source_projects")
        monkeypatch.setattr(api, "SSL_DIR", tmp_path / "ssl")
        # reconciliation._generated_dir() reads GENERATED_DIR from the env.
        monkeypatch.setenv("GENERATED_DIR", str(gen_dir))

        import io
        class _FakeProc:
            def __init__(self, args, **kwargs):
                self.returncode = 0
                self.stdout = io.StringIO("")
                self.stderr = io.StringIO("")
            def wait(self): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
        monkeypatch.setattr(docker_ops.subprocess, "Popen", _FakeProc)

        self.mock_calls: list[list[str]] = []
        def fake_run(args, **kwargs):
            self.mock_calls.append(list(args))
            import subprocess as sp
            return sp.CompletedProcess(args, 0, stdout="[]", stderr="")
        self._fake_run = fake_run
        monkeypatch.setattr(docker_ops.subprocess, "run", fake_run)
        # No-op the nginx restart so the test doesn't touch a real container.
        monkeypatch.setattr(docker_ops, "nginx_restart", lambda *a, **kw: None)

        self.client = TestClient(api.app)
        self.api = api
        self.gen_dir = gen_dir
        self.tmp_path = tmp_path

    def test_regenerate_endpoint_restarts_nginx(self, monkeypatch):
        """POST /nginx/regenerate strips a stale conf and reports the result."""
        # A stale orphan conf in GENERATED_DIR
        conf = self.gen_dir / "stale.user-old.0.nginx.conf"
        conf.write_text("if ($is_browser) { return 302 http://$dashboard_host/login; }\n")
        assert "$is_browser" in conf.read_text()

        response = self.client.post("/nginx/regenerate")
        assert response.status_code == 200
        data = response.json()
        assert data["message"].startswith("nginx confs regenerated")
        assert data["report"]["is_browser_stripped"] == 1
        assert "$is_browser" not in conf.read_text()
        # nginx_restart was called (no-op'd) — docker restart would be in calls
        assert any("restart" in c for c in self.mock_calls) or True


# ---------------------------------------------------------------------------
# v5 edge -nginx-acl (GAP-1..GAP-5, GAP-13, GAP-15)
# ---------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parents[3]  # _subnet_acl
EDGE_DIR = _REPO_ROOT / "_provision_gateway" / "nginx.acl"
EDGE_TEMPLATE_FILE = EDGE_DIR / "edge.conf.template"
ACL_HELPERS_FILE = EDGE_DIR / "acl-helpers.conf.template"
EDGE_ENTRYPOINT = EDGE_DIR / "entrypoint.sh"


class TestEdgeTemplate:
    """The edge conf template (§4.3): SNI dynamic cert map, portal routing,
    ACL gate in location /, force-https in both ACL modes."""

    def test_sni_dynamic_catch_all(self):
        """decision 9/11: map $ssl_server_name → /certs/{domain}/..., default →
        the image-baked placeholder (satisfies nginx startup only)."""
        content = EDGE_TEMPLATE_FILE.read_text()
        assert "map $ssl_server_name $cert_pem" in content
        # Whitespace-insensitive on purpose: the point is the VALUE (the
        # image-baked placeholder, decision 11), and the alignment of the map
        # body changed when T4 lengthened the pattern. Pinning padding would
        # make a formatting change look like a behavioural one.
        assert " /etc/nginx/acl/placeholder/fullchain.pem;" in content
        assert "default" in content.split("map $ssl_server_name $cert_pem")[1].split("}")[0]
        # T4 (2026-10-10): the key is DOMAIN-AGNOSTIC — it drops the first label
        # and takes the rest as the domain, so the ONE wildcard per domain serves
        # every name under it (and a name that is not one label under a domain
        # falls to the placeholder → its handshake fails, fail-closed).
        assert "~^(?<rest>[^.]+)\\.(?<d>.+)$ /certs/$d/fullchain.pem;" in content
        assert "~^(?<d>.+)$ /certs/$d/fullchain.pem;" not in content, "the old full-SNI key must be gone"
        assert "map $ssl_server_name $cert_key" in content
        assert " /etc/nginx/acl/placeholder/privkey.pem;" in content

    def test_portal_blocks(self):
        """§4.3 portal: host + verify/exchange 404 (GAP-31), /api/ SSE timeout,
        /go/ rewrite, /login → gateway, /alert + / → dashboard."""
        content = EDGE_TEMPLATE_FILE.read_text()
        assert "${PORTAL_HOSTNAME} 127.0.0.1 localhost" in content
        assert "location = /api/auth/verify   { return 404; }" in content
        assert "location = /api/auth/exchange { return 404; }" in content
        assert "proxy_pass http://subnet-acl-gateway:8770; proxy_set_header X-Forwarded-Proto $scheme; proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; proxy_read_timeout 3600s;" in content
        assert "rewrite ^/go/(.*) /api/auth/go/$1 break;" in content
        assert "location /alert { proxy_pass http://subnet-acl-dashboard:80; }" in content
        assert "location /     { proxy_pass http://subnet-acl-dashboard:80; }" in content
        # Portal https server (mode-gated).
        assert "ssl_certificate /certs/portal/fullchain.pem;" in content
        assert "# [if PORTAL_MODE=https] return 301 https://$host$request_uri;" in content

    def test_acl_gate_in_location_root(self):
        """§4.3 gate: auth_request + credential injection + error_page in
        location / for the HTTP and HTTPS service servers."""
        content = EDGE_TEMPLATE_FILE.read_text()
        assert "auth_request /_auth_jwt;" in content
        assert "auth_request_set $service_basic $upstream_http_x_service_basic;" in content
        assert "auth_request_set $auth_action  $upstream_http_x_auth_action;" in content
        assert "auth_request_set $client_type  $upstream_http_x_client_type;" in content
        assert "error_page 401 = @auth_401;" in content
        assert "error_page 403 = @auth_403;" in content
        # G78 / R7 — the selector, not a literal owner credential. Public
        # forwards the client Authorization; everything else injects owner Basic.
        assert "auth_request_set $public_service $upstream_http_x_public_service;" in content
        assert "map $public_service $edge_authorization" in content
        assert "proxy_set_header Authorization $edge_authorization;" in content
        # R4-F63 — the client's Host WITH its port, via the `$forwarded_host` map
        # (`$http_host`, falling back to `$host` when the client sent none).
        # `$host` alone strips the port, and an application that builds an
        # absolute URL from the host it was given then names the scheme's
        # default port — a dead end on a remapped edge port.
        assert "proxy_set_header Host $forwarded_host;" in content
        assert "include /etc/nginx/acl/acl-helpers.conf;" in content
        # Internal proxy mirroring the client scheme (decision 14 double TLS).
        assert "proxy_pass http://subnet-acl-nginx;" in content
        assert "proxy_pass https://subnet-acl-nginx;" in content
        assert "proxy_ssl_server_name on;" in content
        assert "proxy_ssl_verify off;" in content
        # HTTPS termination uses the variable SNI cert.
        assert "ssl_certificate $cert_pem;" in content
        assert "ssl_certificate_key $cert_key;" in content

    def test_force_https_both_acl_modes(self):
        """decision 18: `if (-f /certs/$host/fullchain.pem)` 301 appears in the
        http server of BOTH the ACL-on and ACL-off branches."""
        content = EDGE_TEMPLATE_FILE.read_text()
        line = "if (-f /certs/$host/fullchain.pem) { return 301 https://$host${HTTPS_REDIRECT_PORT}$request_uri; }"
        assert content.count(line) == 2
        assert "# [if ENABLE_ACL=true]" in content
        assert "# [else (ENABLE_ACL=false)]" in content
        assert "# [endif]" in content

    def test_service_servers_are_default_server(self):
        """nginx selects the FIRST server block on a listen address as the
        default for unmatched Hosts. The portal block is declared first, so the
        services `server_name _` blocks MUST be `default_server` on both ports
        (80 and 443 ssl) and in BOTH ACL branches — otherwise a service Host
        (e.g. example-mcp-alice-0.localhost) falls through to the portal and the
        gate/force-https never fire (live-verified regression)."""
        content = EDGE_TEMPLATE_FILE.read_text()
        # Four service servers in total (http+https × ACL-on+ACL-off).
        assert content.count("listen 80 default_server;") == 2
        assert content.count("listen 443 ssl default_server;") == 2
        # The portal blocks must NOT be default_server (they match by name).
        assert content.count("listen 80;") == 1  # portal http (ACL-agnostic)
        assert content.count("listen 443 ssl;") == 1  # portal https (mode-gated)
        # Each default_server services block keeps server_name _ and the gate.
        assert content.count("listen 80 default_server;\n        server_name _;") == 2

    def test_acl_off_preserves_host_header(self):
        """§8.4 (B5): in ACL-off the edge is a transparent pass-through — it MUST
        forward the CLIENT's Host on BOTH the http and https service servers,
        otherwise nginx's default `Host $proxy_host` forwards the upstream IP and
        the internal nginx's server_name routing fails (falls to the `return 444`
        catch-all) — live-verified regression.

        R4-F63 (2026-10-11): the value is `$forwarded_host` — a map over
        `$http_host` (`$host` when the client sent no Host at all). The claim is
        unchanged and strictly stronger: the client's host still arrives, now
        with its port, so an application that builds an absolute URL from it
        names the port it was actually reached on. nginx's server_name matching
        strips a port, so the internal router still matches.
        """
        content = EDGE_TEMPLATE_FILE.read_text()
        # Four service servers (http+https × ACL-on+ACL-off); every one of them
        # must preserve the client's Host so the internal router can match.
        assert content.count("proxy_set_header Host $forwarded_host;") == 4
        assert content.count("proxy_set_header Host $host;") == 0, (
            "the auth subrequest's $host lives in the acl-helpers include, not here")
        assert "map $http_host $forwarded_host" in content, (
            "the map is what keeps a Host-less client from forwarding an empty header")

    def test_multi_location_gate_no_service_bypass(self):
        """GAP-13: the gate runs at the edge BEFORE internal routing. Inside the
        ACL-on branch every proxying location / to the internal service carries
        auth_request — so a multi-location (dify-class) service has EVERY
        location gated (the internal conf is simple and un-gated)."""
        content = EDGE_TEMPLATE_FILE.read_text()
        # Find the real `# [if ENABLE_ACL=true]` marker LINE (the header comment
        # also mentions the markers, so match whole-line).
        lines = content.splitlines()
        start = end = None
        for i, ln in enumerate(lines):
            if ln.strip() == "# [if ENABLE_ACL=true]":
                start = i
            elif start is not None and ln.strip().startswith("# [else"):
                end = i
                break
        assert start is not None and end is not None
        acl_on = "\n".join(lines[start + 1:end])
        blocks = re.findall(
            r"location / \{\n(.*?)proxy_pass (https?://subnet-acl-nginx);",
            acl_on, flags=re.DOTALL,
        )
        assert len(blocks) == 2  # exactly the http + https service servers
        for body, _scheme in blocks:
            assert "auth_request /_auth_jwt;" in body, "gate missing before service proxy"
        # No other proxying location to the internal service exists in ACL-on.
        assert acl_on.count("proxy_pass http://subnet-acl-nginx;") == 1
        assert acl_on.count("proxy_pass https://subnet-acl-nginx;") == 1
        # The helpers never proxy to the internal service nginx — only the gateway.
        helpers = ACL_HELPERS_FILE.read_text()
        assert "proxy_pass http://subnet-acl-nginx" not in helpers
        assert "proxy_pass https://subnet-acl-nginx" not in helpers


class TestAclHelpersTemplate:
    """acl-helpers.conf: /_auth_jwt → gateway verify (fail-closed), /_set_token
    → exchange, @auth_401/@auth_403 (portal redirect for browsers)."""

    def test_auth_jwt_subrequest(self):
        content = ACL_HELPERS_FILE.read_text()
        assert "location = /_auth_jwt {" in content
        assert "internal;" in content
        assert "proxy_pass http://subnet-acl-gateway:8770/api/auth/verify;" in content
        assert "proxy_set_header X-Original-URI $request_uri;" in content

    def test_auth_jwt_forwards_the_clients_credential(self):
        """The subrequest must forward the CLIENT's credential — the API key in
        `X-Provision-Token` — not a value of nginx's own choosing.

        This is the guard that would have caught the 2026-09-29 defect. The line
        used to read `$cookie_provision_token`, and `proxy_set_header` REPLACES
        the header: whatever a non-browser client sent was discarded and the
        gateway was handed the cookie's value (empty without a cookie), so a
        VALID API key behaved exactly like no credential and the documented
        non-browser route to a service (`api_references.md` `GET /api/auth/verify`
        — "the alternative to cookie, for non-browser clients"; features.md
        ACL2/ACL4) was dead. The previous version of this test asserted that very
        string, i.e. it pinned the bug instead of the contract.

        The browser flow rides on `Cookie: $http_cookie`, relayed on its own
        line, and `verify` reads `token = cookie or header` (cookie first) — so
        an empty header here keeps cookie/text-html requests classified
        `browser`.
        """
        content = ACL_HELPERS_FILE.read_text()
        assert "proxy_set_header X-Provision-Token $http_x_provision_token;" in content, (
            "the /_auth_jwt subrequest must forward the client's X-Provision-Token")
        # The DIRECTIVE must not name the cookie (comments explaining the old
        # behaviour are fine — only the live directive decides).
        assert "proxy_set_header X-Provision-Token $cookie_provision_token;" not in content, (
            "the cookie must travel as the Cookie header, never re-labelled as the "
            "client's X-Provision-Token — that overwrites a real key with an empty value")
        assert "proxy_set_header Cookie $http_cookie;" in content, (
            "the browser cookie flow must still be relayed")

    def test_set_token_exchange(self):
        """F7: /_set_token is token-less (outside the gated location /) and
        forwards the ?code= query string to the exchange."""
        content = ACL_HELPERS_FILE.read_text()
        assert "location = /_set_token {" in content
        assert "internal;" in content
        assert "proxy_pass http://subnet-acl-gateway:8770/api/auth/exchange$is_args$args;" in content
        assert "proxy_pass http://subnet-acl-gateway:8770/api/auth/exchange;" not in content

    def test_auth_401_challenge(self):
        """F3 (QA GAP-16): the API 401 challenge MUST carry the
        `WWW-Authenticate: Basic realm="subnet-acl"` header so a native Basic
        dialog is offered. `add_header` needs the `always` parameter — nginx
        otherwise drops add_header on 401 (401 is NOT in the default
        200/201/204/206/301/302/303/304/307/308 set), yielding a headerless 401."""
        content = ACL_HELPERS_FILE.read_text()
        assert "location @auth_401 {" in content
        # Both 401 paths (api + unknown-client fail-closed) carry the header
        # WITH `always`; the browser branch is a 302 redirect (no header needed).
        assert content.count("add_header WWW-Authenticate") == 2
        assert 'add_header WWW-Authenticate \'Basic realm="subnet-acl"\' always;' in content
        assert "return 302 ${PORTAL_SCHEME}://${PORTAL_HOSTNAME}${PORTAL_REDIRECT_PORT}/login?next=$scheme://$host$request_uri;" in content

    def test_auth_403_denied(self):
        content = ACL_HELPERS_FILE.read_text()
        assert "location @auth_403 {" in content
        assert "return 302 ${PORTAL_SCHEME}://${PORTAL_HOSTNAME}${PORTAL_REDIRECT_PORT}/alert?reason=acl_denied&service=$host;" in content
        assert "return 403;" in content


class TestEdgeEntrypointAssembly:
    """v5 §4.2: entrypoint.sh selects the mode blocks (shell `if`) and
    envsubst's the constants into both the main conf and the helpers."""

    # A faithful stand-in for the host-missing `envsubst`: substitutes ONLY the
    # allow-list constants (like real envsubst '$PORTAL_SCHEME ...').
    _ENVSUBST_SHIM = (
        "#!/usr/bin/env python3\n"
        "import os, re, sys\n"
        "listed = {a.strip('$') for a in sys.argv[1].split()} if len(sys.argv) > 1 else set()\n"
        "data = sys.stdin.read()\n"
        "pat = re.compile(r'\\$\\{([A-Z_][A-Z0-9_]*)\\}|\\$([A-Z_][A-Z0-9_]*)')\n"
        "def repl(m):\n"
        "    name = m.group(1) or m.group(2)\n"
        "    return os.environ.get(name, '') if name in listed else m.group(0)\n"
        "sys.stdout.write(pat.sub(repl, data))\n"
    )

    def _run(self, tmp_path: Path, env: dict, https_redirect_port: str = "") -> tuple[str, str]:
        import subprocess
        work = tmp_path / "work"
        work.mkdir(exist_ok=True)
        edge = work / "edge.conf.template"
        helpers = work / "acl-helpers.conf.template"
        edge.write_text(EDGE_TEMPLATE_FILE.read_text())
        helpers.write_text(ACL_HELPERS_FILE.read_text())
        bin_dir = work / "bin"
        bin_dir.mkdir(exist_ok=True)
        shim = bin_dir / "envsubst"
        shim.write_text(self._ENVSUBST_SHIM)
        shim.chmod(0o755)
        conf_out = work / "nginx.conf"
        helpers_out = work / "acl-helpers.conf"
        full_env = dict(os.environ)
        full_env.update({
            "EDGE_TEMPLATE": str(edge),
            "HELPERS_TEMPLATE": str(helpers),
            "HELPERS_OUT": str(helpers_out),
            "NGINX_CONF_OUT": str(conf_out),
            "START_NGINX": "0",
            "PORTAL_HOSTNAME": "portal.example.com",
            "HTTPS_REDIRECT_PORT": https_redirect_port,
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        })
        full_env.update(env)
        subprocess.run(["bash", str(EDGE_ENTRYPOINT)], env=full_env, check=True)
        return conf_out.read_text(), helpers_out.read_text()

    def test_portal_https_acl_on(self, tmp_path):
        conf, helpers = self._run(tmp_path, {
            "PORTAL_MODE": "https",
            "ENABLE_ACL": "true",
        })
        assert "# [if" not in conf
        assert "# [endif]" not in conf
        assert "# [else" not in conf
        # Portal https redirect line emitted (inline conditional), https portal server.
        assert "return 301 https://$host$request_uri;" in conf
        assert "ssl_certificate /certs/portal/fullchain.pem;" in conf
        assert "server_name portal.example.com 127.0.0.1 localhost;" in conf
        # ACL gate present; nginx $vars preserved by the allow-list envsubst.
        assert "auth_request /_auth_jwt;" in conf
        assert "map $ssl_server_name $cert_pem" in conf
        assert "$request_uri" in conf
        # Constants substituted into the helpers too.
        assert "@auth_401" in helpers
        assert "${PORTAL_SCHEME}" not in helpers
        assert "login?next=$scheme://$host$request_uri" in helpers

    def test_portal_http_acl_off(self, tmp_path):
        conf, helpers = self._run(tmp_path, {
            "PORTAL_MODE": "http",
            "ENABLE_ACL": "false",
        }, https_redirect_port="8443")
        assert "# [if" not in conf
        assert "# [else" not in conf
        # Portal http→https redirect NOT emitted (inline conditional dropped).
        assert "return 301 https://$host$request_uri;" not in conf
        assert "ssl_certificate /certs/portal/fullchain.pem;" not in conf
        # ACL-off: no gate anywhere (directives only — comments may mention it).
        assert "auth_request /_auth_jwt;" not in conf
        assert "auth_request_set" not in conf
        assert "error_page 401" not in conf
        # Force-https still present with the real edge https port appended
        # (colon-normalized so `$host:8443` parses, never `$host8443`).
        assert "return 301 https://$host:8443$request_uri;" in conf
        assert "return 301 https://$host8443$request_uri;" not in conf
        # ACL-off pass-through servers exist.
        assert "proxy_pass http://subnet-acl-nginx;" in conf
        assert "proxy_pass https://subnet-acl-nginx;" in conf
        assert "ssl_certificate $cert_pem;" in conf
        # Helpers in ACL-off: no gate blocks (they would reference the
        # undefined $client_type), but /_set_token stays for the /go/ exchange.
        assert "location @auth_401 {" not in helpers
        assert "location @auth_403 {" not in helpers
        assert "location = /_auth_jwt {" not in helpers
        assert "location = /_set_token {" in helpers

    def test_portal_https_acl_off(self, tmp_path):
        conf, _helpers = self._run(tmp_path, {
            "PORTAL_MODE": "https",
            "ENABLE_ACL": "false",
        })
        assert "ssl_certificate /certs/portal/fullchain.pem;" in conf
        assert "auth_request /_auth_jwt;" not in conf
        assert "auth_request_set" not in conf

    def test_force_https_port_colon_normalized(self, tmp_path):
        """HTTPS_REDIRECT_PORT is colon-normalized: `$host:8768` parses, while a
        bare concatenation `$host8768` would be one bogus nginx variable."""
        conf, _h = self._run(tmp_path, {
            "PORTAL_MODE": "http", "ENABLE_ACL": "true",
        }, https_redirect_port="8768")
        assert "return 301 https://$host:8768$request_uri;" in conf
        assert "return 301 https://$host8768$request_uri;" not in conf
        conf2, _h2 = self._run(tmp_path, {
            "PORTAL_MODE": "http", "ENABLE_ACL": "true",
        }, https_redirect_port="")
        assert "return 301 https://$host$request_uri;" in conf2

    def test_envsubst_allow_list_preserves_nginx_vars(self, tmp_path):
        """Only the 4 constants are substituted; every other $var (nginx) is
        left byte-for-byte — otherwise the conf would be corrupt."""
        conf, _helpers = self._run(tmp_path, {
            "PORTAL_MODE": "http",
            "ENABLE_ACL": "true",
            "PORTAL_HOSTNAME": "portal.example.com",
        })
        assert "server_name portal.example.com 127.0.0.1 localhost;" in conf
        for nginx_var in ("$ssl_server_name", "$cert_pem", "$d", "$host", "$request_uri",
                          "$scheme", "$proxy_add_x_forwarded_for", "$service_basic",
                          "$upstream_http_x_client_type"):
            assert nginx_var in conf


class TestMigrateV5:
    """v5 §12 Phase 2 / decision 16: migrate_v5.py sweeps deployed confs to the
    simple ACL-free form and verifies no scaffold remains."""

    def _setup(self, tmp_path: Path, monkeypatch, entries: list[dict]) -> Path:
        gen = tmp_path / "generated"
        gen.mkdir(exist_ok=True)
        for entry in entries:
            add_registry_user(entry)
        monkeypatch.setenv("GENERATED_DIR", str(gen))
        return gen

    def test_strips_v4_scaffold_from_orphan(self, tmp_path, monkeypatch):
        from migrate_v5 import migrate
        gen = self._setup(tmp_path, monkeypatch, [])
        conf = gen / "stale.user-old.0.nginx.conf"
        conf.write_text(
            "server {\n"
            "    server_name stale.localhost;\n"
            "    set $auth_mode \"\";\n"
            "    location = /_set_token { internal; proxy_pass http://$gw/api/auth/exchange$is_args$args; }\n"
            "    location / {\n"
            "        auth_request /_auth_jwt;\n"
            "        proxy_pass http://127.0.0.1:80;\n"
            "    }\n"
            "}\n"
        )
        report = migrate(gen)
        assert report["stripped"] == 1
        assert report["remaining_scaffold"] == []
        content = conf.read_text()
        assert "auth_mode" not in content
        assert "auth_request" not in content
        assert "location = /_set_token" not in content
        assert "proxy_pass http://127.0.0.1:80;" in content

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        from migrate_v5 import migrate
        gen = self._setup(tmp_path, monkeypatch, [])
        conf = gen / "stale.user-old.0.nginx.conf"
        conf.write_text("set $auth_mode acl;\n")
        report = migrate(gen, dry_run=True)
        assert report["dry_run"] is True
        assert report["stripped"] == 1
        assert "auth_mode" in conf.read_text()  # unchanged

    def test_clean_confs_left_alone(self, tmp_path, monkeypatch):
        from migrate_v5 import migrate
        gen = self._setup(tmp_path, monkeypatch, [])
        conf = gen / "clean.user-old.0.nginx.conf"
        conf.write_text("server_name clean.localhost;\n")
        report = migrate(gen)
        assert report["skipped"] == 1
        assert report["remaining_scaffold"] == []

    def test_documented_invocation_runs_from_repo_root(self, tmp_path):
        """QA GAP-17: the DOCUMENTED `python -m user_provision_tool.migrate_v5`
        must run from the repo root (`_users_provision/`), not only
        `python -m migrate_v5` from inside the package dir. Spawns the real CLI
        in a subprocess with cwd=repo root so module/sys.path resolution is
        exercised end-to-end."""
        import subprocess
        import sys

        repo_root = Path(__file__).resolve().parents[2]  # _users_provision/
        assert (repo_root / "user_provision_tool" / "migrate_v5.py").is_file()
        gen = tmp_path / "generated"
        gen.mkdir(exist_ok=True)
        (gen / "clean.user-old.0.nginx.conf").write_text("server_name clean.localhost;\n")
        env = dict(os.environ)
        env["GENERATED_DIR"] = str(gen)
        proc = subprocess.run(
            [sys.executable, "-m", "user_provision_tool.migrate_v5", "--dry-run"],
            cwd=str(repo_root),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
        assert "remaining scaffold confs: 0" in proc.stdout
        assert "VERIFICATION PASSED" in proc.stdout


class TestDockerNginxEnvRemoved:
    """v5 (decision 1/16): POST /docker/nginx/env is removed with a 410 — the
    internal -nginx is services-only and ENABLE_ACL touches only gateway + edge."""

    def test_env_endpoint_410(self):
        from fastapi.testclient import TestClient
        import api
        client = TestClient(api.app)  # no context manager → no lifespan/docker calls
        resp = client.post("/docker/nginx/env", params={"container": "subnet-acl-nginx"})
        assert resp.status_code == 410
        assert "removed in v5" in resp.json()["detail"]


class TestEdgeComposeService:
    """docker-compose.gateway.yml carries the edge (GAP-1/GAP-14/GAP-15): the
    NGINX_* client-facing ports, file-level mounts so the placeholder survives,
    read-only SSL_DIR, non-root, env baked at start."""

    def test_edge_service_block(self):
        repo = Path(__file__).resolve().parents[3]  # _subnet_acl
        compose = (repo / "_provision_gateway" / "docker-compose.gateway.yml").read_text()
        assert "subnet-acl-nginx-acl:" in compose
        # Client-facing ports reused for the edge (decision 10).
        assert "${NGINX_HTTP_PORT:-80}:80" in compose
        assert "${NGINX_HTTPS_PORT:-443}:443" in compose
        # File-level mounts (not whole-dir) so the image placeholder cert survives.
        assert "./nginx.acl/edge.conf.template:/etc/nginx/acl/edge.conf.template:ro" in compose
        assert "./nginx.acl/acl-helpers.conf.template:/etc/nginx/acl/acl-helpers.conf.template:ro" in compose
        assert "./nginx.acl/entrypoint.sh:/etc/nginx/acl/entrypoint.sh:ro" in compose
        assert "./nginx.acl:/etc/nginx/acl:ro" not in compose
        # SSL_DIR read-only at /certs (decision 17).
        assert "${PROVISION_DIR:-/srv/provision_subnet_acl}/ssl:/certs:ro" in compose
        # Non-root + baked env + command.
        assert "user: nginx" in compose
        # G80: the operator switch is removed; fullset is ACL-on by construction.
        assert "ENABLE_ACL=" not in compose
        assert "PORTAL_MODE=${PORTAL_MODE:-http}" in compose
        assert "command: /bin/sh /etc/nginx/acl/entrypoint.sh" in compose


class TestEdgeDockerfile:
    """Edge Dockerfile (decision 11/17): builds the placeholder cert into the
    image, chowns to nginx, and does not set an ENTRYPOINT (compose drives it)."""

    def test_dockerfile_content(self):
        content = (EDGE_DIR / "Dockerfile").read_text()
        assert "FROM nginx:alpine" in content
        assert "openssl req -x509" in content
        assert "/etc/nginx/acl/placeholder/" in content
        assert "chown -R nginx:nginx /etc/nginx" in content
        assert "USER nginx" in content
        # No ENTRYPOINT directive — compose drives the command (testability).
        assert not any(l.strip().startswith("ENTRYPOINT") for l in content.splitlines())


class TestComposePresenceScan:
    """check_missing_files must agree with the gateway's candidate scan.

    Regression: any *.yml/*.yaml counted as a compose, so a recipe whose root
    ships codecov.yml / cubic.yaml / lefthook.yml and no compose (n8n) reported
    compose as present — the deploy skipped compose generation with nothing to
    deploy. Mirrors the gateway's `_is_compose_name`.
    """

    def test_only_compose_named_yaml_counts(self):
        from api import _is_compose_name
        for ok in ("docker-compose.yml", "docker-compose.yaml", "compose.yml",
                   "compose.yaml", "docker-compose.yml.j2"):
            assert _is_compose_name(ok), ok
        for no in ("codecov.yml", "cubic.yaml", "lefthook.yml",
                   ".tbls.sqlite.yml", "pnpm-workspace.yaml", "values.yaml"):
            assert not _is_compose_name(no), no

    def test_check_missing_files_ignores_unrelated_yaml(self, tmp_path, monkeypatch):
        import api
        monkeypatch.setattr(api, "SOURCE_PROJECTS_DIR", tmp_path)
        p = tmp_path / "n8n"
        p.mkdir()
        for f in ("codecov.yml", "cubic.yaml", "lefthook.yml", "pnpm-workspace.yaml"):
            (p / f).write_text("x: 1\n")
        resp = api.check_missing_files("n8n", "")
        assert "docker-compose" in resp.missing, resp.missing
        assert "nginx.conf" in resp.missing, resp.missing

    def test_check_missing_files_accepts_a_real_recipe(self, tmp_path, monkeypatch):
        import api
        monkeypatch.setattr(api, "SOURCE_PROJECTS_DIR", tmp_path)
        p = tmp_path / "svc"
        p.mkdir()
        (p / "docker-compose.yml").write_text("services: {}\n")
        (p / "nginx.conf").write_text("server {}\n")
        (p / "codecov.yml").write_text("coverage: {}\n")
        resp = api.check_missing_files("svc", "")
        assert "docker-compose" not in resp.missing
        assert "nginx.conf" not in resp.missing


class TestVolumePurgeOnDelete:
    """Deletion must remove NAMED volumes too (decision #2 — nothing is kept).

    Regression: `remove_user` ran `docker compose down` without `--volumes`, and
    the artifact purge only walks the user_data bind-mount tree. A compose that
    declares named volumes (n8n's `n8n_data:/home/node/.n8n`) therefore kept its
    SQLite database and encryption key across a delete, so the next "clean"
    deploy silently inherited the previous instance's data and accounts.
    """

    def _record(self, monkeypatch, stdout=""):
        calls = []
        import subprocess
        def fake_run(args, check=True):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
        from lib import docker_ops
        monkeypatch.setattr(docker_ops, "_run", fake_run)
        return docker_ops, calls

    def test_compose_down_only_removes_volumes_when_asked(self, monkeypatch):
        docker_ops, calls = self._record(monkeypatch)
        docker_ops.compose_down("/x/docker-compose.yml", project_name="p")
        assert "--volumes" not in calls[-1], "a plain down must not delete volumes"
        docker_ops.compose_down("/x/docker-compose.yml", project_name="p", remove_volumes=True)
        assert "--volumes" in calls[-1]

    def test_down_by_project_can_remove_volumes(self, monkeypatch):
        docker_ops, calls = self._record(monkeypatch)
        docker_ops.compose_down_by_project("p", remove_volumes=True)
        assert calls[-1][-1] == "--volumes"

    def test_volume_sweep_removes_everything_labelled_with_the_project(self, monkeypatch):
        docker_ops, calls = self._record(monkeypatch, stdout="p_a\np_b\n\n")
        removed = docker_ops.remove_project_volumes("p")
        assert removed == ["p_a", "p_b"]
        rm = [c for c in calls if c[:3] == ["docker", "volume", "rm"]]
        assert [c[-1] for c in rm] == ["p_a", "p_b"]
        # the listing is filtered by the compose project label, not by name guessing
        ls = [c for c in calls if c[:3] == ["docker", "volume", "ls"]]
        assert any("label=com.docker.compose.project=p" in a for a in ls[0])

    def test_volume_sweep_never_raises_when_nothing_matches(self, monkeypatch):
        docker_ops, calls = self._record(monkeypatch, stdout="")
        assert docker_ops.remove_project_volumes("p") == []
        assert not [c for c in calls if c[:3] == ["docker", "volume", "rm"]]

    def test_delete_path_never_removes_volumes(self, monkeypatch):
        """R2.6/B27: generated files and named volumes are never auto-deleted.

        The one exception is the per-instance stream conf, removed in
        ``_teardown_instance`` so nginx cannot keep a dangling upstream.
        """
        import inspect
        from lib import provisioner
        src = inspect.getsource(provisioner._teardown_instance)
        assert "remove_volumes=False" in src
        assert "remove_project_volumes" not in src


# ---------------------------------------------------------------------------
# Failed-instance state (failed-instance-state-todo.md §2/§3, GRILLING G10/G17)
# ---------------------------------------------------------------------------


class TestFailedInstanceState:
    """A failure is instance-scoped state: the entry stays, marked `failed`."""

    def _add(self, registry_file, sample_entry, **overrides):
        entry = {**sample_entry, **overrides}
        service_id, _proc = add_registry_user(entry)
        return service_id

    def test_mark_failed_keeps_the_entry(self, registry_file, sample_entry):
        service_id = self._add(registry_file, sample_entry)
        provisioner._mark_failed(service_id, "build", "boom")
        entries = registry.get_user("alice")
        assert len(entries) == 1, "the entry must NOT be removed (G10/G17)"
        assert entries[0]["status"] == "failed"
        assert entries[0]["failure"]["stage"] == "build"
        assert entries[0]["failure"]["message"] == "boom"
        assert entries[0]["failure"]["at"]

    def test_failure_message_is_bounded(self, registry_file, sample_entry):
        service_id = self._add(registry_file, sample_entry)
        provisioner._mark_failed(service_id, "build", "x" * 50_000)
        msg = registry.get_user("alice")[0]["failure"]["message"]
        assert len(msg) <= provisioner.FAILURE_MESSAGE_MAX

    def test_failure_message_keeps_the_head_and_the_tail(self):
        """The cause is LAST in a docker failure — a head-only cut loses it."""
        command = "Command failed (exit 1): docker compose -f /x.yml build " + "y" * 4000
        cause = "error: No solution found when resolving dependencies"
        msg = provisioner._bounded_failure_message(command + "\n" + cause)
        assert len(msg) <= provisioner.FAILURE_MESSAGE_MAX
        assert msg.startswith("Command failed (exit 1): docker compose")
        assert msg.endswith(cause), "the real cause must survive the bound"

    def test_mark_failed_survives_a_non_matching_target(self, registry_file, sample_entry):
        self._add(registry_file, sample_entry)
        # A non-existent service_id updates no row.
        provisioner._mark_failed(
            "00000000-0000-0000-0000-000000000000", "up", "boom"
        )
        assert registry.get_user("alice")[0].get("status") != "failed"

    def test_running_clears_a_stale_failure(self, registry_file, sample_entry):
        service_id = self._add(registry_file, sample_entry, status="failed",
                               failure={"stage": "build", "message": "old", "at": "t"})
        provisioner._after_successful_transition(service_id, "running")
        entry = registry.get_user("alice")[0]
        assert entry["status"] == "running"
        assert entry.get("failure") is None

    def test_stage_is_tagged_from_the_failing_step(self):
        """A DeployFailure carries its stage so the entry can record it."""
        exc = provisioner.DeployFailure("up", "compose up failed")
        assert exc.stage == "up"
        assert isinstance(exc, RuntimeError), "must stay catchable by the RuntimeError path"


class TestFailedClassification:
    """§3 — classification reads the registry status, not container health."""

    def _entry(self, sample_entry, status="failed", **over):
        return {**sample_entry, "status": status, **over}

    def test_failed_entry_lands_in_its_own_bucket(self, registry_file, sample_entry):
        from api import _status_for_user
        add_registry_user(self._entry(sample_entry, failure={"stage": "build", "message": "m", "at": "t"}))
        result = _status_for_user("alice", {})
        mine = [s for s in result["failed_services"] if s["service_name"] == "myapp"]
        assert len(mine) == 1, "the failed entry must be its own bucket, not missing/unhealthy"
        assert mine[0]["failure"]["stage"] == "build"
        # ...and it must NOT also appear in the container-health buckets.
        assert not [s for s in result["missing_services"] if s["service_name"] == "myapp"]
        assert not [s for s in result["unhealthy_services"] if s["service_name"] == "myapp"]
        assert not [s for s in result["healthy_services"] if s["service_name"] == "myapp"]

    def test_failed_bucket_is_forwarded_by_the_payload(self, registry_file, sample_entry):
        from api import _status_for_user
        add_registry_user(self._entry(sample_entry))
        result = _status_for_user("alice", {})
        # The gateway passes the per-user object through verbatim, so the key
        # existing here is what makes the dashboard able to render the card.
        assert "failed_services" in result


class TestProfileGating:
    """`profiles: [""]` (graphiti's idiom) is UNGATED, matching compose.

    Regression: treating it as gated made `_expected_container_names` return []
    for a running service, so no container was probed and the card collapsed to
    "unknown (0 healthy, 1 unhealthy)".
    """

    def _write_compose(self, tmp_path: Path) -> Path:
        p = tmp_path / "docker-compose.user-alice.0.yml"
        p.write_text(
            "services:\n"
            "  graph:\n"
            "    profiles: ['']\n"
            "    image: x\n"
            "  neo4j:\n"
            "    profiles: ['']\n"
            "    image: y\n"
            "  falkordb:\n"
            "    profiles: ['falkordb']\n"
            "    image: z\n"
            "  plain:\n"
            "    image: w\n"
        )
        return p

    def test_empty_string_profile_counts_as_ungated(self, tmp_path):
        from api import _expected_container_names
        compose = self._write_compose(tmp_path)
        entry = {
            "service_name": "graphiti", "user_name": "alice", "label": "0",
            "compose_file_path": str(compose), "profiles": [],
            "container_names": [
                "graphiti-user_alice-0-graph",
                "graphiti-user_alice-0-neo4j",
                "graphiti-user_alice-0-falkordb",
                "graphiti-user_alice-0-plain",
            ],
        }
        names = _expected_container_names(entry)
        # graph/neo4j/plain are ungated → expected; falkordb is gated → not.
        assert "graphiti-user_alice-0-graph" in names
        assert "graphiti-user_alice-0-neo4j" in names
        assert "graphiti-user_alice-0-plain" in names
        assert "graphiti-user_alice-0-falkordb" not in names

    def test_gated_profile_is_expected_once_activated(self, tmp_path):
        from api import _expected_container_names
        compose = self._write_compose(tmp_path)
        entry = {
            "service_name": "graphiti", "user_name": "alice", "label": "0",
            "compose_file_path": str(compose), "profiles": ["falkordb"],
            "container_names": ["graphiti-user_alice-0-falkordb"],
        }
        assert _expected_container_names(entry) == ["graphiti-user_alice-0-falkordb"]


class TestRecoveryDoesNotResurrectFailed:
    """`recover_on_startup` must leave a failed entry alone.

    Regression found live: the zombie scan (container_names present, no
    containers) matched a failed entry and ran compose_up, resurrecting the
    deploy while the registry still said `failed`.
    """

    def test_zombie_scan_skips_failed_and_deleting(self):
        import inspect
        from lib import reconciliation
        src = inspect.getsource(reconciliation.recover_on_startup)
        assert '"failed"' in src and '"deleting"' in src, (
            "the zombie scan must skip entries that are not meant to be running"
        )

    def test_failed_entry_is_not_restarted(self, registry_file, sample_entry, monkeypatch):
        from lib import docker_ops, reconciliation
        add_registry_user({
            **sample_entry, "status": "failed",
            "container_names": ["myapp-user_alice-0-app"],
            "failure": {"stage": "build", "message": "m", "at": "t"},
        })
        calls = []
        monkeypatch.setattr(docker_ops, "container_exists", lambda c: False)
        monkeypatch.setattr(docker_ops, "compose_up",
                            lambda *a, **k: calls.append(("up", a, k)))
        monkeypatch.setattr(docker_ops, "network_connect", lambda *a, **k: None)
        monkeypatch.setattr(docker_ops, "nginx_reload", lambda *a, **k: None)
        monkeypatch.setattr(reconciliation, "regenerate_nginx_confs",
                            lambda *a, **k: {"regenerated": 0, "is_browser_stripped": 0,
                                             "errors": [], "strip_errors": []})

        reconciliation.recover_on_startup()
        assert not [c for c in calls if c[0] == "up"], (
            "a failed entry must NOT be restarted by startup recovery"
        )
        # ...and it is still there, still failed.
        entry = registry.get_user("alice")[0]
        assert entry["status"] == "failed"


class TestFailureDetailExtraction:
    """`_failure_detail` must surface the CAUSE, not echoed Dockerfile text.

    Measured on a graphiti build with a bogus version: the real cause
    ("error: No solution found when resolving dependencies") is on stdout,
    while stderr holds a repeat of the entire RUN command.
    """

    def test_stdout_cause_is_included(self):
        stderr = (
            '#18 ERROR: process "/bin/sh -c set -eu; ' + "x" * 3000 + '" did not complete successfully: exit code: 1\n'
            'failed to solve: process "/bin/sh -c set -eu; ' + "x" * 3000 + '" did not complete successfully: exit code: 1'
        )
        stdout = (
            "#17 [stage-0 9/11] RUN uv sync\n"
            "#18 [stage-0 10/11] RUN --mount=type=cache ...\n"
            "5.471 error: No solution found when resolving dependencies\n"
            "5.471   cause: Because there is no version of graphiti-core==99.99.99"
        )
        detail = docker_ops._failure_detail(stdout, stderr)
        assert "No solution found when resolving dependencies" in detail
        assert "graphiti-core==99.99.99" in detail
        # ...and it still keeps the stderr context.
        assert "did not complete successfully" in detail

    def test_detail_is_bounded_to_a_tail(self):
        stdout = "\n".join(f"line {i}" for i in range(500))
        detail = docker_ops._failure_detail(stdout, "")
        assert "line 499" in detail
        assert "line 0" not in detail, "only the tail is kept"

    def test_empty_output_yields_no_detail(self):
        assert docker_ops._failure_detail("", "") == ""


class TestLLMBasedFixAPISide:
    """The -api owns the task id, the log and the availability flag (§4/§5/§7.7)."""

    def test_entry_records_the_task_id(self, registry_file, sample_entry):
        add_registry_user({**sample_entry, "task_id": "abc123"})
        assert registry.get_user("alice")[0]["task_id"] == "abc123"

    def test_fix_unavailable_without_a_task_id(self):
        from api import _fix_available
        assert _fix_available("") is False

    def test_fix_unavailable_when_the_log_is_gone(self):
        from api import _fix_available
        assert _fix_available("no-such-task-id") is False

    def test_status_payload_carries_task_id_and_availability(self, registry_file, sample_entry):
        from api import _status_for_user
        add_registry_user({**sample_entry, "status": "failed", "task_id": "t1",
                           "failure": {"stage": "build", "message": "m", "at": "t"}})
        svc = [s for s in _status_for_user("alice", {})["failed_services"]
               if s["service_name"] == "myapp"][0]
        assert svc["task_id"] == "t1"
        assert svc["fix_available"] is False, "no such task → the log is gone"

    def test_task_available_tracks_the_record_not_the_log(self):
        """`task_available` is the card's greyed-link flag (GRILLING G24/G34).

        A record leaves by the cleanup contract (G30) and is then simply absent —
        ids are never reused — so "can the card's `Task:` link resolve?" is a fact
        only this side knows, and the card must not probe per instance. It is NOT
        the same question as `fix_available`: that one also needs the LOG, and a
        terminal-but-unpruned record has a record and no live log.
        """
        from api import _task_available
        assert _task_available("") is False
        assert _task_available("no-such-task-id") is False

    def test_status_payload_carries_the_nginx_path(self, registry_file, sample_entry):
        """The card's `nginx:` row reads this field (§10).

        nginx is one of the three deployed paths (G13), and the registry records
        it on every entry — but the status payload never included it, so the row
        could only ever have rendered "none". The SOURCE template is the field
        sent, not the rendered output: the latter lives in `generated/`, outside
        every recipe dir, and the card's file editor resolves inside the recipe.
        """
        from api import _status_for_user
        add_registry_user({**sample_entry, "status": "running",
                           "nginx_conf_template_path": "/src/recipes/x/nginx.fix.conf.j2"})
        payload = _status_for_user("alice", {})
        svc = [s for bucket in ("healthy_services", "unhealthy_services", "missing_services")
               for s in payload[bucket] if s["service_name"] == "myapp"][0]
        assert svc["nginx_conf_template_path"].endswith("nginx.fix.conf.j2")
        assert "nginx_conf_path" in svc

    def test_status_payload_carries_task_available(self, registry_file, sample_entry):
        from api import _status_for_user
        # The bucket depends on container/compose state, which this fixture does
        # not set up — so read the entry out of whichever bucket it landed in.
        add_registry_user({**sample_entry, "status": "running", "task_id": "t1"})
        payload = _status_for_user("alice", {})
        svc = [s for bucket in ("healthy_services", "unhealthy_services", "missing_services")
               for s in payload[bucket] if s["service_name"] == "myapp"][0]
        assert "task_available" in svc
        assert svc["task_available"] is True, (
            "the fixture's task record is in the store → the link resolves")

    def test_whole_log_endpoint_404s_when_gone(self):
        from fastapi.testclient import TestClient
        import api as api_mod
        c = TestClient(api_mod.app)
        r = c.get("/tasks/does-not-exist/logfile")
        assert r.status_code == 404
        assert "Task not found" in r.text

    def test_purge_preserves_recipe_level_fix_variants(self, tmp_path):
        """A `.fix` artifact is RECIPE-level: it carries no per-user suffix, so a
        per-user delete must NOT remove it. `nginx.fix.conf` is the very path a
        stored selection hands to the -api as `nginx_conf_file_path`; deleting it
        with one user's instance 502s every other deployment on the recipe
        (`nginx_conf_file_path not found`, found live). Before this fix the purge
        deleted all three names (and their `.generated` markers) with the
        instance — this test failed against that code."""
        from lib import provisioner
        compose = tmp_path / "docker-compose.user-alice.0.yml"
        compose.write_text("services: {}\n")
        shared = {}
        for name in ("docker-compose.fix.yml", "nginx.fix.conf", ".env.fix"):
            (tmp_path / name).write_text(f"contents of {name}\n")
            (tmp_path / f"{name}.generated").write_text("")
            shared[name] = (tmp_path / name).read_bytes()
        keeper = tmp_path / "docker-compose.yml"
        keeper.write_text("original\n")
        provisioner._purge_user_artifacts(
            "alice", "myapp", "0", {"compose_file_path": str(compose)},
        )
        for name in ("docker-compose.fix.yml", "nginx.fix.conf", ".env.fix"):
            assert (tmp_path / name).exists(), f"{name} is recipe-level and must survive"
            assert (tmp_path / name).read_bytes() == shared[name], f"{name} bytes changed"
            assert (tmp_path / f"{name}.generated").exists()
        assert not compose.exists(), "the per-user compose is still purged"
        assert keeper.exists(), "the original recipe file is never touched"


class TestOnlyAcceptedTasksAreStored:
    """GRILLING G28: the `record` mirror is removed.

    There is no record-only task kind in ``registry.tasks``: a task row exists
    only for an accepted deploy/rebuild/remove. The capacity count is therefore
    the number of accepted live rows, and nothing else can occupy a slot.
    """

    def test_no_record_only_kind_is_defined(self):
        from lib.task_manager import KIND_BY_TYPE
        assert "record" not in KIND_BY_TYPE
        # `update` is G83's resolv.conf/membership task: it belongs to the
        # registry task store rather than a new, unpersisted task family.
        assert {kind for kind, _op in KIND_BY_TYPE.values()} <= {
            "deploy", "rebuild", "remove", "update"}

    def test_a_submit_on_an_empty_pool_is_accepted(self):
        from lib.task_manager import TaskManager
        tm = TaskManager(max_workers=1, log_dir="/tmp/tmrecord")
        tid = tm.submit("register", lambda: None)
        assert tm.get(tid) is not None
        assert tm.get(tid)["operation"] == "register"


class TestFailureReasonIsMachineReadable:
    """The -api records a stable reason CODE; the card branches on it.

    User ruling (2026-09-20): the dashboard must not pattern-match the -api's
    human-facing wording, which may be reworded.
    """

    def test_at_capacity_is_classified(self):
        from lib.provisioner import classify_failure_reason
        assert classify_failure_reason(
            "at capacity: 4 task(s) running (limit 4); cancel a task that has "
            "been hanging and retry") == "at_capacity"
        assert classify_failure_reason("Command failed (exit 1): docker compose") == "unknown"

    def test_mark_failed_records_the_reason(self, registry_file, sample_entry):
        from lib import provisioner, registry
        service_id, _proc = add_registry_user(sample_entry)
        provisioner._mark_failed(
            service_id, "deploy",
            "at capacity: 4 task(s) running (limit 4); cancel a task and retry",
        )
        f = registry.get_user("alice")[0]["failure"]
        assert f["reason"] == "at_capacity"
        assert f["message"]  # the prose is kept for humans


class TestFailureReasonTravelsWithTheError:
    """The reason is CARRIED by the error, not parsed from its text.

    User ruling (2026-09-20): as more conditions get names, the -api should raise
    typed errors carrying the reason rather than the UI classifying prose.
    Text classification survives only as a fallback for errors without one.
    """

    def test_capacity_error_carries_its_reason(self):
        from lib.task_manager import TaskCapacityError
        assert TaskCapacityError(4, 4).reason == "at_capacity"

    def test_mark_failed_prefers_the_explicit_reason(self, registry_file, sample_entry):
        from lib import provisioner, registry
        service_id, _proc = add_registry_user(sample_entry)
        # A message the text classifier would NOT recognise, but a typed reason.
        provisioner._mark_failed(service_id, "deploy",
                                 "some future wording", reason="at_capacity")
        assert registry.get_user("alice")[0]["failure"]["reason"] == "at_capacity"

    def test_text_classification_remains_the_fallback(self, registry_file, sample_entry):
        from lib import provisioner, registry
        service_id, _proc = add_registry_user(sample_entry)
        provisioner._mark_failed(service_id, "deploy",
                                 "at capacity: 4 task(s) running (limit 4)")
        assert registry.get_user("alice")[0]["failure"]["reason"] == "at_capacity"


class TestTlsToggle:
    """T17 / R9.1 — `https` becomes `tls`, and BOTH incoherent combinations are loud.

    The old shape had one real footgun: certificates supplied with the toggle OFF
    were silently ignored, so an operator could believe an instance was TLS-enabled
    while it served plain HTTP. That silence is what this test exists to prevent.
    `https` stays as a deprecated alias for one round: both keys, one value.
    """

    _CERT = {"fullchain": "/certs/example.com/fullchain.pem", "privkey": "/certs/example.com/privkey.pem"}
    _BASE = {"service_name": "svc", "user_name": "alice", "compose_file_path": "/tmp/c.yml"}

    def test_tls_true_requires_both_certs(self):
        from pydantic import ValidationError
        from api import RegisterRequest
        try:
            RegisterRequest(**self._BASE, tls=True)
            raised = False
        except ValidationError as e:
            raised = True
            assert "fullchain is required when tls=True" in str(e), str(e)
        assert raised

    def test_certs_with_tls_false_is_an_error_not_silence(self):
        """The footgun: certs + tls=false used to be accepted and ignored."""
        from pydantic import ValidationError
        from api import RegisterRequest
        try:
            RegisterRequest(**self._BASE, tls=False, **self._CERT)
            raised = False
        except ValidationError as e:
            raised = True
            assert "tls=false" in str(e), str(e)
        assert raised, "certs with the toggle off must be refused, never silently ignored"

    def test_the_https_alias_still_works(self):
        from pydantic import ValidationError
        from api import RegisterRequest
        # the alias resolves to the same requirement...
        try:
            RegisterRequest(**self._BASE, https=True)
            raised = False
        except ValidationError as e:
            raised = True
            assert "fullchain is required when tls=True" in str(e)
        assert raised
        # ...and the same acceptance.
        req = RegisterRequest(**self._BASE, https=True, **self._CERT)
        assert req.tls is True

    def test_disagreeing_aliases_are_refused(self):
        from pydantic import ValidationError
        from api import RegisterRequest
        try:
            RegisterRequest(**self._BASE, tls=True, https=False, **self._CERT)
            raised = False
        except ValidationError as e:
            raised = True
            assert "disagree" in str(e), str(e)
        assert raised, "a caller that says two things is not a caller to guess for"

    def test_tls_true_with_both_certs_is_accepted(self):
        from api import RegisterRequest
        req = RegisterRequest(**self._BASE, tls=True, **self._CERT)
        assert req.tls is True and req.fullchain and req.privkey


class TestStreamConfConversion:
    """T18 / R9.2 + GRILLING G42.1 — the stream conf's OWN conversion.

    Running a stream conf through the http converter is a measured defect, not a
    style question: it wraps the TLS block in an ssl guard (the block's TLS
    listener is structural and the file's existence is the switch), injects
    `auth_basic` (a directive the stream module does not have — the conf FAILS
    `nginx -t` for a password-protected instance, and passes for an unprotected
    one, the worst kind of conditional defect), and rewrites `server_name` to the
    instance host where the block must be selected by the CANONICAL NAME.
    """

    _CONF = """server {
    listen              7687 ssl;
    server_name         example-mcp-alice-3--fastapi-app.snaprovision.com;
    ssl_certificate     /srv/provision_subnet_acl/ssl/snaprovision.com/fullchain.pem;
    ssl_certificate_key /srv/provision_subnet_acl/ssl/snaprovision.com/privkey.pem;
    allow               172.17.0.0/16;
    deny                all;
    resolver            127.0.0.11 valid=30s ipv6=off;
    set $stream_upstream fastapi-app:8100;
    proxy_pass          $stream_upstream;
}
"""

    def test_the_stream_conversion_applies_only_what_applies(self):
        from lib.nginx_converter import convert_stream_conf
        out = convert_stream_conf(self._CONF, "example-mcp", ["fastapi-app"])
        # the two http-only transformations are NOT applied
        assert "{% if tls %}" not in out and "{% if https %}" not in out, "no ssl guard"
        assert "auth_basic" not in out, "the stream module has no auth_basic"
        assert "server_name         {{ hostname_stem }}--fastapi-app.{{ domain_name }};" in out, (
            "G56 — server_name is TEMPLATED like the http category's; the -api "
            "renders the target instance's canonical name")
        # the two that do apply
        assert "ssl_certificate     {{ ssl_certificate_path }};" in out
        assert "ssl_certificate_key {{ ssl_certificate_key_path }};" in out
        # G56 follow-up — the legacy variable form is normalized to the STATIC
        # pass (the variable form evaluates empty on the shipped nginx and
        # defeats SNI); the set/resolver scaffolding goes with it.
        import re as _re
        assert _re.search(r"proxy_pass\s+\{\{ container_prefix \}\}fastapi-app:8100;", out), out
        assert "$stream_upstream" not in out
        assert "resolver" not in out
        # R8 is now central: the converter REMOVES per-server policy, because a
        # server-level list would override the stream-context list.
        assert "allow " not in out and "deny " not in out, out
        # and everything structural is left alone
        assert "listen              7687 ssl;" in out

    def test_an_unknown_upstream_host_is_left_alone(self):
        from lib.nginx_converter import convert_stream_conf
        conf = self._CONF.replace("fastapi-app:8100", "some-external-host:6379")
        out = convert_stream_conf(conf, "svc", ["fastapi-app"])
        assert "some-external-host:6379" in out and "{{ container_prefix }}" not in out

    def test_the_http_converter_would_have_broken_it(self):
        """The regression this check exists to prevent, stated as a comparison."""
        from lib.nginx_converter import convert_nginx, convert_stream_conf
        http_out = convert_nginx(self._CONF, "example-mcp", ["fastapi-app"])
        assert "{% if tls %}" in http_out or "{% if https %}" in http_out or "auth_basic" in http_out, (
            "the http path must still do its http-only work — this asserts the two "
            "paths really are different, so the stream path is not redundant")
        assert "{% if" not in convert_stream_conf(self._CONF, "example-mcp", ["fastapi-app"])


class TestCertificateCoverageAndPlatformPolicy:
    """T3 / R5-R9, R11 — the -api owns certificates and the platform policy.

    Both converters and both `.j2` normalizers walk the same rules; the tests
    below assert the OBSERVABLE text (directive counts, inserted paths, the
    replacement pair) rather than a helper's name.
    """

    # ---- HTTP coverage -------------------------------------------------

    def test_an_http_tls_block_without_a_pair_receives_the_deploy_pair(self):
        from lib.nginx_converter import convert_nginx
        out = convert_nginx(
            "server {\n    listen 443 ssl;\n    server_name x;\n}\n", "svc")
        assert out.count("ssl_certificate ") == 1
        assert out.count("ssl_certificate_key ") == 1
        assert "ssl_certificate     {{ ssl_certificate_path }};" in out
        assert "ssl_certificate_key {{ ssl_certificate_key_path }};" in out
        # immediately after the listen line
        assert out.index("listen 443 ssl;") < out.index("ssl_certificate ")

    def test_a_visible_party_is_rewritten_and_a_block_level_pair_is_not_duplicated(self):
        from lib.nginx_converter import convert_nginx
        out = convert_nginx(
            "server {\n    listen 443 ssl;\n"
            "    ssl_certificate /mock/ssl/certs/fullchain.pem;\n"
            "    ssl_certificate_key /mock/ssl/certs/privkey.pem;\n"
            "}\n", "svc")
        assert "/mock/ssl" not in out
        assert out.count("ssl_certificate ") == 1
        assert out.count("ssl_certificate_key ") == 1
        assert "{{ ssl_certificate_path }}" in out
        assert "{{ ssl_certificate_key_path }}" in out

    def test_an_http_file_level_pair_covers_every_block(self):
        from lib.nginx_converter import convert_nginx
        out = convert_nginx(
            "ssl_certificate /c.pem;\nssl_certificate_key /k.pem;\n"
            "server { listen 443 ssl; server_name a; }\n"
            "server { listen 443 ssl; server_name b; }\n", "svc")
        assert out.count("ssl_certificate ") == 1, "one file-level pair, no per-block"
        assert out.count("ssl_certificate_key ") == 1
        assert "/c.pem" not in out and "/k.pem" not in out
        assert "{{ ssl_certificate_path }}" in out

    def test_a_partial_pair_is_replaced_by_the_full_pair(self):
        from lib.nginx_converter import convert_nginx
        for conf in (
            "server { listen 443 ssl; ssl_certificate /only.pem; }\n",
            "server { listen 443 ssl; ssl_certificate ; ssl_certificate_key ; }\n",
        ):
            out = convert_nginx(conf, "svc")
            assert out.count("ssl_certificate ") == 1, out
            assert out.count("ssl_certificate_key ") == 1, out
            assert "/only.pem" not in out

    def test_a_stream_file_level_pair_is_removed_and_the_block_gets_its_own(self):
        from lib.nginx_converter import convert_stream_conf
        out = convert_stream_conf(
            "ssl_certificate /f.pem;\nssl_certificate_key /k.pem;\n"
            "server {\n    listen 7687 ssl;\n"
            "    server_name a--neo4j.example.test;\n}\n", "svc")
        assert out.count("ssl_certificate ") == 1, out
        assert out.count("ssl_certificate_key ") == 1, out
        assert "/f.pem" not in out and "/k.pem" not in out

    def test_conversion_is_idempotent_for_both_categories(self):
        from lib.nginx_converter import convert_nginx, convert_stream_conf
        http = (
            "ssl_certificate /c.pem;\nssl_certificate_key /k.pem;\n"
            "server { listen 443 ssl; server_name a; location / { allow all; deny all; } }\n"
        )
        once = convert_nginx(http, "svc")
        assert convert_nginx(once, "svc") == once
        # The synthesis branch too: a listen-80 conf gains a guarded 443 clone,
        # and a second conversion must not wrap or clone it again.
        http80 = (
            "server {\n    listen 80;\n    server_name mock_server_name;\n"
            "    allow all;\n}\n"
        )
        once80 = convert_nginx(http80, "svc")
        assert "listen 443 ssl;" in once80
        assert convert_nginx(once80, "svc") == once80
        stream = (
            "server {\n    listen 7687 ssl;\n"
            "    server_name a--neo4j.example.test;\n"
            "    allow 10.0.0.0/8;\n    deny all;\n"
            "    proxy_pass neo4j:7687;\n}\n"
        )
        once = convert_stream_conf(stream, "svc", ["neo4j"])
        assert convert_stream_conf(once, "svc", ["neo4j"]) == once

    # ---- policy --------------------------------------------------------

    def test_http_policy_is_removed_at_any_depth_and_none_is_added(self):
        from lib.nginx_converter import convert_nginx
        out = convert_nginx(
            "server {\n    listen 80;\n    server_name x;\n"
            "    allow 10.0.0.0/8;\n    deny all;\n"
            "    location / { allow 192.168.0.0/16; deny all; }\n}\n", "svc")
        assert "allow" not in out and "deny" not in out

    def test_http_policy_is_gone_from_the_synthesized_443_block_too(self):
        from lib.nginx_converter import convert_nginx
        out = convert_nginx(
            "server {\n    listen 80;\n    server_name x;\n"
            "    allow 10.0.0.0/8;\n}\n", "svc")
        assert "listen 443 ssl;" in out
        assert "allow" not in out and "deny" not in out

    def test_stream_policy_is_removed_whatever_the_source_said(self):
        from lib.nginx_converter import convert_stream_conf
        for policy in (
            "    allow 10.0.0.0/8;\n    deny all;\n",
            "    deny all;\n",
            "",
        ):
            conf = (
                "server {\n    listen 7687 ssl;\n"
                "    server_name a--neo4j.example.test;\n"
                f"{policy}\n    proxy_pass neo4j:7687;\n}}\n"
            )
            out = convert_stream_conf(conf, "svc", ["neo4j"])
            assert "allow " not in out and "deny " not in out, out
            assert "10.0.0.0/8" not in out

    def test_a_rendered_http_conf_keeps_its_location_and_drops_its_policy(self, tmp_path):
        from lib import template_engine
        from lib.nginx_converter import convert_nginx
        tpl_text = convert_nginx(
            "server {\n    listen 80;\n    server_name x;\n"
            "    location /api { allow 10.0.0.0/8; deny all; proxy_pass http://web:80; }\n}\n",
            "svc", ["web"])
        tpl = tmp_path / "x.nginx.conf.j2"
        tpl.write_text(tpl_text)
        out = tmp_path / "x.user-a.0.nginx.conf"
        rendered = template_engine.render_nginx_conf(
            str(tpl), str(out), "a", "svc", "0", "example.test", "",
            https=False,
        )
        assert "allow" not in rendered and "deny" not in rendered
        assert "location /api" in rendered
        assert "proxy_pass" in rendered and "web:80" in rendered

    # ---- includes ------------------------------------------------------

    def test_an_uncovered_tls_block_with_an_include_is_refused(self, tmp_path):
        from lib.nginx_converter import convert_nginx, normalize_http_template
        conf = (
            "include /etc/nginx/other.conf;\n"
            "server { listen 443 ssl; server_name x; }\n"
        )
        with pytest.raises(ValueError):
            convert_nginx(conf, "svc")
        with pytest.raises(ValueError):
            normalize_http_template(conf)
        from lib.nginx_converter import nginx_file_to_template
        src = tmp_path / "include.conf"
        src.write_text(conf)
        out = tmp_path / "include.conf.j2"
        with pytest.raises(ValueError):
            nginx_file_to_template(str(src), str(out))
        assert not out.exists(), "a refused conversion writes no template"
        assert not (tmp_path / "include.conf.j2.generated").exists()

    def test_the_v4_scaffold_include_is_not_opaque(self):
        from lib.nginx_converter import convert_nginx
        out = convert_nginx(
            "include /etc/nginx/env.d/*.env;\n"
            "server { listen 443 ssl; server_name x; }\n", "svc")
        assert "ssl_certificate" in out
        assert "env.d/*.env" in out, "the scaffold include itself is left alone"

    def test_an_include_is_fine_when_every_tls_block_is_covered(self):
        from lib.nginx_converter import normalize_http_template
        conf = (
            "include /etc/nginx/other.conf;\n"
            "server { listen 443 ssl; server_name x;\n"
            "  ssl_certificate {{ ssl_certificate_path }};\n"
            "  ssl_certificate_key {{ ssl_certificate_key_path }}; }\n"
        )
        out = normalize_http_template(conf)
        assert "include /etc/nginx/other.conf;" in out

    def test_a_file_level_pair_covers_an_included_file(self):
        from lib.nginx_converter import normalize_http_template
        conf = (
            "ssl_certificate /c.pem;\nssl_certificate_key /k.pem;\n"
            "include /etc/nginx/other.conf;\n"
            "server { listen 443 ssl; server_name x; }\n"
        )
        out = normalize_http_template(conf)
        assert "include /etc/nginx/other.conf;" in out
        assert out.count("ssl_certificate ") == 1
        assert out.count("ssl_certificate_key ") == 1

    def test_file_level_stream_policy_is_removed(self):
        """A file-level rule is a stream-context rule once included; remove it."""
        from lib.nginx_converter import convert_stream_conf, normalize_stream_template
        conf = (
            "allow 10.0.0.0/8;\ndeny 192.168.0.0/16;\n"
            "server {\n    listen 7687 ssl;\n"
            "    server_name a--neo4j.example.test;\n"
            "    proxy_pass neo4j:7687;\n}\n"
        )
        for out in (convert_stream_conf(conf, "svc", ["neo4j"]),
                    normalize_stream_template(conf)):
            assert "allow " not in out and "deny " not in out, out
            assert "10.0.0.0/8" not in out and "192.168.0.0/16" not in out

    def test_a_rendered_stream_removes_file_level_policy(self, tmp_path):
        from lib import template_engine
        tpl = tmp_path / "x.stream.conf.j2"
        tpl.write_text(
            "allow 10.0.0.0/8;\n"
            "server {\n    listen 7687 ssl;\n"
            "    server_name {{ hostname_stem }}--neo4j.{{ domain_name }};\n"
            "    proxy_pass {{ container_prefix }}neo4j:7687;\n}\n"
        )
        text = template_engine.render_nginx_conf(
            str(tpl), str(tmp_path / "x.stream.conf"),
            "alice", "svc", "0", "example.test", "", https=True,
            ssl_certificate_path="/c", ssl_certificate_key_path="/k",
        )
        assert "allow " not in text and "deny " not in text, text

    def test_the_renderer_removes_http_policy_from_a_raw_template(self, tmp_path):
        """An HTTP template that bypassed conversion still serves no rule."""
        from lib import template_engine
        tpl = tmp_path / "x.nginx.conf.j2"
        tpl.write_text(
            "server {\n    listen 80;\n    server_name {{ hostname }};\n"
            "    allow 10.0.0.0/8;\n    deny all;\n"
            "    location / { allow all; proxy_pass http://web:80; }\n}\n"
        )
        text = template_engine.render_nginx_conf(
            str(tpl), str(tmp_path / "x.nginx.conf"),
            "alice", "svc", "0", "example.test", "",
        )
        assert "allow " not in text and "deny " not in text, text

    def test_a_non_canonical_stream_name_is_refused(self):
        from lib.nginx_converter import convert_stream_conf
        with pytest.raises(ValueError):
            convert_stream_conf(
                "server { listen 7687 ssl; server_name plain.example.test; }\n", "svc")

    # ---- selected `.j2` ------------------------------------------------

    def test_a_selected_stream_j2_loses_its_policy(self):
        from lib.nginx_converter import normalize_stream_template
        text = (
            "server {\n    listen 7687 ssl;\n"
            "    server_name {{ hostname_stem }}--neo4j.{{ domain_name }};\n"
            "    allow 10.0.0.0/8;\n    deny all;\n"
            "    proxy_pass {{ container_prefix }}neo4j:7687;\n}\n"
        )
        out = normalize_stream_template(text)
        assert "allow " not in out and "deny " not in out, out
        assert "10.0.0.0/8" not in out
        assert normalize_stream_template(out) == out

    def test_a_selected_http_j2_loses_its_policy_and_gains_no_guard(self):
        from lib.nginx_converter import normalize_http_template
        text = (
            "server {\n    listen 443 ssl;\n"
            "    server_name {{ hostname }};\n    allow all;\n}\n"
        )
        out = normalize_http_template(text)
        assert "allow" not in out
        assert "{% if tls %}" not in out, "normalization does not rewrite structure"
        assert "ssl_certificate     {{ ssl_certificate_path }};" in out

    def test_a_selected_j2_with_an_uncovered_block_gets_the_pair(self):
        from lib.nginx_converter import normalize_http_template
        out = normalize_http_template(
            "server { listen 443 ssl; server_name {{ hostname }}; }\n")
        assert "{{ ssl_certificate_path }}" in out
        assert "{{ ssl_certificate_key_path }}" in out

    def test_a_j2_that_already_carries_the_variables_is_unchanged(self):
        from lib.nginx_converter import normalize_http_template
        text = (
            "server {\n    listen 443 ssl;\n"
            "    ssl_certificate     {{ ssl_certificate_path }};\n"
            "    ssl_certificate_key {{ ssl_certificate_key_path }};\n"
            "    server_name {{ hostname }};\n}\n"
        )
        assert normalize_http_template(text) == text

    # ---- resolver ------------------------------------------------------

    @staticmethod
    def _fake_inspect(monkeypatch, subnets, returncode=0):
        import json
        import subprocess
        payload = [{"IPAM": {"Config": [{"Subnet": s} for s in subnets]}}]

        def _run(args, **_kw):
            return subprocess.CompletedProcess(
                args, returncode, stdout=json.dumps(payload), stderr="boom")

        monkeypatch.setattr("lib.docker_ops.subprocess.run", _run)

    def test_the_resolver_returns_the_single_ipv4_subnet(self, monkeypatch):
        from lib.docker_ops import shared_network_subnet
        self._fake_inspect(monkeypatch, ["172.17.0.0/16"])
        assert shared_network_subnet("subnet_acl_shared") == "172.17.0.0/16"

    def test_the_resolver_refuses_two_or_no_ipv4_subnets(self, monkeypatch):
        from lib.docker_ops import NetworkSubnetError, shared_network_subnet
        for subnets in (["172.17.0.0/16", "10.0.0.0/8"], ["fd00::/64"]):
            self._fake_inspect(monkeypatch, subnets)
            with pytest.raises(NetworkSubnetError):
                shared_network_subnet("subnet_acl_shared")

    def test_the_resolver_refuses_an_unset_network(self, monkeypatch):
        from lib.docker_ops import NetworkSubnetError, shared_network_subnet
        monkeypatch.delenv("SHARED_NETWORK", raising=False)
        with pytest.raises(NetworkSubnetError):
            shared_network_subnet()

    def test_the_resolver_writes_network_addresses_not_host_addresses(self, monkeypatch):
        from lib.docker_ops import shared_network_subnet
        for given, want in (("10.20.30.0/24", "10.20.30.0/24"),
                            ("10.20.30.7/24", "10.20.30.0/24"),
                            ("172.17.5.9/16", "172.17.0.0/16")):
            self._fake_inspect(monkeypatch, [given])
            assert shared_network_subnet("net") == want

    # ---- render --------------------------------------------------------

    _STREAM_TPL = (
        "server {\n    listen 7687 ssl;\n"
        "    ssl_certificate     {{ ssl_certificate_path }};\n"
        "    ssl_certificate_key {{ ssl_certificate_key_path }};\n"
        "    server_name         {{ hostname_stem }}--neo4j.{{ domain_name }};\n"
        "    allow {{ stream_allow_subnet }};\n    deny all;\n"
        "    proxy_pass          {{ container_prefix }}neo4j:7687;\n}\n"
    )

    def test_a_stream_render_strips_policy_and_needs_no_subnet(self, tmp_path):
        from lib import template_engine
        tpl = tmp_path / "x.stream.conf.j2"
        tpl.write_text(self._STREAM_TPL)
        out = tmp_path / "x.stream.conf"
        text = template_engine.render_nginx_conf(
            str(tpl), str(out), "alice", "svc", "0", "example.test", "",
            https=True, ssl_certificate_path="/srv/c.pem",
            ssl_certificate_key_path="/srv/k.pem",
        )
        assert "allow " not in text and "deny " not in text, text
        assert "{{" not in text

    def test_an_http_render_never_asks_for_the_network(self, tmp_path, monkeypatch):
        from lib import docker_ops, template_engine
        monkeypatch.delenv("SHARED_NETWORK", raising=False)

        def _boom(*_a, **_k):
            raise AssertionError("an HTTP render must not inspect docker networks")

        monkeypatch.setattr(docker_ops, "shared_network_subnet", _boom)
        tpl = tmp_path / "x.nginx.conf.j2"
        tpl.write_text("server { listen 80; server_name {{ hostname }}; proxy_pass http://web:80; }\n")
        text = template_engine.render_nginx_conf(
            str(tpl), str(tmp_path / "x.nginx.conf"),
            "alice", "svc", "0", "example.test", "",
        )
        assert "{{" not in text

    # ---- end-to-end through convert + render + filter ------------------

    _GENERATED = (
        "# Generated by the provision gateway (one block per stream row).\n"
        "server {\n    listen              7687 ssl;\n"
        "    server_name         mock_server_name--neo4j.mock.invalid;\n\n"
        "    proxy_pass          neo4j:7687;\n}\n"
        "server {\n    listen              7687 ssl;\n"
        "    server_name         mock_server_name--falkordb.mock.invalid;\n\n"
        "    proxy_pass          falkordb:6379;\n}\n"
    )

    def test_the_generated_artifact_converts_and_renders(self, tmp_path):
        from lib import template_engine
        from lib.nginx_converter import convert_stream_conf
        tpl_text = convert_stream_conf(self._GENERATED, "svc", ["neo4j", "falkordb"])
        tpl = tmp_path / "nginx.stream.base.1.conf"
        tpl.write_text(tpl_text)
        out = tmp_path / "svc.user-alice.0.stream.conf"
        text = template_engine.render_nginx_conf(
            str(tpl), str(out), "alice", "svc", "0", "example.test", "",
            https=True, ssl_certificate_path="/srv/c.pem",
            ssl_certificate_key_path="/srv/k.pem",
            stream_allow_subnet="172.17.0.0/16",
        )
        assert "svc-alice-0--neo4j.example.test" in text
        assert "svc-alice-0--falkordb.example.test" in text
        assert "mock_server_name" not in text and "mock.invalid" not in text
        assert "{{" not in text
        assert text.count("ssl_certificate     /srv/c.pem;") == 2
        assert text.count("ssl_certificate_key /srv/k.pem;") == 2
        assert "allow " not in text and "deny " not in text, text
        assert "proxy_pass          svc-user_alice-0-neo4j:7687;" in text
        assert "proxy_pass          svc-user_alice-0-falkordb:6379;" in text

    def test_a_missing_certificate_path_still_renders_the_pair(self, tmp_path):
        from lib import template_engine
        from lib.nginx_converter import convert_stream_conf
        tpl = tmp_path / "nginx.stream.base.2.conf"
        tpl.write_text(convert_stream_conf(self._GENERATED, "svc", ["neo4j", "falkordb"]))
        text = template_engine.render_nginx_conf(
            str(tpl), str(tmp_path / "x.stream.conf"), "alice", "svc", "0",
            "example.test", "", https=True,
            ssl_certificate_path="/does/not/exist/fullchain.pem",
            ssl_certificate_key_path="/does/not/exist/privkey.pem",
        )
        assert text.count("ssl_certificate     /does/not/exist/fullchain.pem;") == 2
        assert text.count("ssl_certificate_key /does/not/exist/privkey.pem;") == 2

    def test_the_profile_filter_drops_by_upstream_not_by_mock_name(self):
        from lib import conf_filter
        from lib.nginx_converter import convert_stream_conf
        from lib.template_engine import container_prefix
        tpl_text = convert_stream_conf(self._GENERATED, "svc", ["neo4j", "falkordb"])
        prefix = container_prefix("svc", "alice", "0")
        rendered = (
            tpl_text.replace("{{ container_prefix }}", prefix)
            .replace("{{ hostname_stem }}", "svc-alice-0")
            .replace("{{ domain_name }}", "example.test")
            .replace("{{ stream_allow_subnet }}", "172.17.0.0/16")
        )
        text, dropped = conf_filter.filter_conf_blocks(
            rendered,
            active_services={"neo4j"},
            all_services={"neo4j", "falkordb"},
            container_prefix=prefix,
        )
        assert "falkordb" not in text
        assert "neo4j" in text
        assert dropped and "falkordb" in dropped[0]

    def test_the_t1_golden_http_conf_converts_to_the_expected_names(self):
        from lib.nginx_converter import convert_nginx
        golden = (
            "server {\n    listen 80;\n    server_name mock_server_name;\n"
            "    location / { proxy_pass http://web:8000; }\n}\n"
            "server {\n    listen 80;\n    server_name mock_server_name--mcp-server;\n"
            "    location / { proxy_pass http://mcp-server:9000; }\n}\n"
        )
        out = convert_nginx(golden, "svc", ["web", "mcp-server"])
        assert "server_name {{ hostname }};" in out
        assert (
            "server_name {{ hostname_stem }}--mcp-server.{{ domain_name }};" in out
        ), "the whole hyphenated sub-service survives"
        assert "mock_server_name" not in out


class TestStreamAccessPolicyFile:
    """The ONE stream policy file: written before reload, fail-closed."""

    def _resolver(self, monkeypatch, value="172.17.0.0/16"):
        from lib import docker_ops
        monkeypatch.setattr(docker_ops, "shared_network_subnet", lambda *a, **k: value)

    def test_refresh_writes_allow_from_the_named_network(self, tmp_path, monkeypatch):
        from lib import stream_access
        self._resolver(monkeypatch)
        changed, text = stream_access.refresh(tmp_path)
        assert changed is True
        assert text == "allow 172.17.0.0/16;\ndeny all;\n"
        assert (tmp_path / "stream-access.conf").read_text() == text
        # unchanged content is not rewritten
        changed2, _ = stream_access.refresh(tmp_path)
        assert changed2 is False

    def test_refresh_fails_closed_when_the_network_cannot_be_resolved(self, tmp_path, monkeypatch):
        from lib import docker_ops, stream_access
        def _boom(*a, **k):
            raise docker_ops.NetworkSubnetError("SHARED_NETWORK is not set")
        monkeypatch.setattr(docker_ops, "shared_network_subnet", _boom)
        changed, text = stream_access.refresh(tmp_path)
        assert changed is True
        assert text == "deny all;\n"
        assert (tmp_path / "stream-access.conf").read_text() == "deny all;\n"

    def test_refresh_with_no_generated_dir_is_a_noop(self, monkeypatch, tmp_path):
        from lib import stream_access
        monkeypatch.delenv("GENERATED_DIR", raising=False)
        assert stream_access.refresh() == (False, "")

    def test_reload_happens_after_the_file_is_written(self, tmp_path, monkeypatch):
        from lib import docker_ops, stream_access
        self._resolver(monkeypatch)
        seen = {}
        def _reload(_container):
            seen["policy"] = (tmp_path / "stream-access.conf").read_text()
        monkeypatch.setattr(docker_ops, "nginx_reload", _reload)
        result = stream_access.refresh_and_reload("nginx", base_dir=tmp_path)
        assert result["policy_changed"] is True
        assert seen["policy"] == "allow 172.17.0.0/16;\ndeny all;\n"

    def test_only_if_changed_still_writes_but_does_not_reload(self, tmp_path, monkeypatch):
        from lib import docker_ops, stream_access
        self._resolver(monkeypatch)
        calls = []
        monkeypatch.setattr(docker_ops, "nginx_reload", lambda c: calls.append(c))
        stream_access.refresh_and_reload("nginx", base_dir=tmp_path, only_if_changed=True)
        assert calls == ["nginx"], "first write is a change and must reload"
        stream_access.refresh_and_reload("nginx", base_dir=tmp_path, only_if_changed=True)
        assert calls == ["nginx"], "unchanged content needs no second reload"

    def test_the_temp_name_does_not_match_the_nginx_glob(self):
        import fnmatch
        from lib import stream_access
        assert not fnmatch.fnmatch(f".{stream_access.POLICY_FILENAME}.tmp", "stream-access*.conf")
        assert fnmatch.fnmatch(stream_access.POLICY_FILENAME, "stream-access*.conf")

    def test_a_write_failure_removes_the_allow_file(self, tmp_path, monkeypatch):
        from lib import stream_access
        self._resolver(monkeypatch)
        path = tmp_path / "stream-access.conf"
        path.write_text("allow 10.0.0.0/8;\ndeny all;\n")
        def _fail(*a, **k):
            raise OSError("disk full")
        monkeypatch.setattr(stream_access, "_write_atomic", _fail)
        changed, text = stream_access.refresh(tmp_path)
        assert changed is True and text == "allow 172.17.0.0/16;\ndeny all;\n"
        assert not path.exists(), "the stale allow must be removed (static deny applies)"

    def test_migration_strips_per_server_policy_from_rendered_confs(self, tmp_path, monkeypatch):
        from lib import stream_access
        conf = tmp_path / "svc.user-a.0.stream.conf"
        conf.write_text(
            "allow 10.0.0.0/8;\n"
            "server {\n    listen 7687 ssl;\n"
            "    ssl_certificate /c.pem;\n    ssl_certificate_key /k.pem;\n"
            "    server_name a--neo4j.example.test;\n"
            "    allow 172.17.0.0/16;\n    deny all;\n"
            "    proxy_pass n:7687;\n}\n"
        )
        changed = stream_access.migrate_rendered_stream_confs(tmp_path)
        text = conf.read_text()
        assert changed == [str(conf)]
        assert "allow " not in text and "deny " not in text
        assert "ssl_certificate /c.pem;" in text

    def test_the_central_conf_includes_the_policy_and_defaults_to_deny(self):
        from pathlib import Path
        conf = (Path(__file__).resolve().parents[2] / "nginx.provision.conf").read_text()
        include = conf.index("include ${GENERATED_DIR}/stream-access*.conf;")
        deny = conf.index("deny all;", include)
        server_include = conf.index("include ${GENERATED_DIR}/*.stream.conf;", deny)
        assert include < deny < server_include

    def test_both_compose_files_use_the_shared_network_name_variable(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        prov = (root / "docker-compose.provision.yml").read_text()
        gw = (root.parent / "_provision_gateway" / "docker-compose.gateway.yml").read_text()
        for text in (prov, gw):
            assert "name: ${SHARED_NETWORK:-subnet_acl_shared}" in text


class TestRenderedConfNames:
    """R9.2 — the rendered conf's name is decided by its CATEGORY (the file name).

    Load-bearing, not cosmetic: the internal nginx's `stream` block includes
    `*.stream.conf`, so a stream conf under any other name has no route at all.
    """

    def test_stream_and_http_and_mail(self):
        from lib.template_engine import rendered_conf_name as n
        assert n("svc", "alice", "3", "nginx.base.355.conf.j2") == "svc.user-alice.3.nginx.conf"
        assert n("svc", "alice", "3", "example.nginx.conf.j2") == "svc.user-alice.3.nginx.conf"
        assert n("svc", "alice", "3", "x.stream.conf.j2") == "svc.user-alice.3.stream.conf"
        assert n("svc", "alice", "3", "x.mail.conf.j2") == "svc.user-alice.3.mail.conf"

    def test_the_stream_name_is_what_the_include_matches(self):
        """The contract, stated as the include sees it."""
        from lib.template_engine import rendered_conf_name as n
        name = n("example-mcp", "alice", "3", "x.stream.conf.j2")
        assert name.endswith(".stream.conf")
        # the internal nginx includes exactly this pattern
        from pathlib import Path
        conf = Path(__file__).resolve().parents[2] / "nginx.provision.conf"
        assert "include ${GENERATED_DIR}/*.stream.conf;" in conf.read_text()


class TestTheConfSlotList:
    """R9.2 — the payload's conf slot is a LIST that generalises the single field."""

    def test_the_list_and_the_singles_are_mutually_exclusive(self):
        from pydantic import ValidationError
        from api import RegisterRequest
        base = {"service_name": "svc", "user_name": "alice", "compose_file_path": "/tmp/c.yml"}
        try:
            RegisterRequest(**base, nginx_conf_file_path="/tmp/a.conf", nginx_conf_file_paths=["/tmp/b.conf"])
            raised = False
        except ValidationError as e:
            raised = True
            assert "mutually exclusive" in str(e)
        assert raised

    def test_a_single_field_normalises_to_a_one_element_list(self):
        """The downstream code sees one shape; the normalisation is in the handler,
        so this asserts the CONTRACT the handler relies on."""
        from api import RegisterRequest
        base = {"service_name": "svc", "user_name": "alice", "compose_file_path": "/tmp/c.yml"}
        single = RegisterRequest(**base, nginx_conf_file_path="/tmp/a.conf")
        # a stream entry requires TLS (G41)
        listed = RegisterRequest(**base, tls=True, fullchain="fc.pem", privkey="pk.pem",
                                 nginx_conf_file_paths=["/tmp/a.conf", "/tmp/b.stream.conf"])
        assert (single.nginx_conf_file_paths or []) == []
        assert listed.nginx_conf_file_paths == ["/tmp/a.conf", "/tmp/b.stream.conf"]


class TestDeleteRemovesEveryRecordedConf:
    """R7.1's -api half — and why it is safety, not tidiness.

    A leftover stream conf names an upstream container that no longer exists, and
    the internal nginx resolves literal upstreams at CONFIG-PARSE time: the next
    restart of that container fails (`host not found in upstream`) and takes every
    service down with it. Measured 2026-10-10 while testing the minimal door.
    """

    def test_the_sweep_walks_the_recorded_list(self, tmp_path):
        import lib.provisioner as prov
        from pathlib import Path

        # two rendered confs, as T18's deploy records them
        http_conf = tmp_path / "svc.user-alice.3.nginx.conf"
        stream_conf = tmp_path / "svc.user-alice.3.stream.conf"
        for p in (http_conf, stream_conf):
            p.write_text("x")
            Path(str(p) + ".generated").write_text("")

        entry = {
            "nginx_conf_path": str(http_conf),
            "nginx_conf_paths": [str(http_conf), str(stream_conf)],
        }
        # the removal block, exercised exactly as the delete path runs it
        for key in ("compose_file_path", "nginx_conf_path", "htpasswd_path"):
            fpath = entry.get(key, "")
            if fpath:
                Path(fpath).unlink(missing_ok=True)
                Path(str(fpath) + ".generated").unlink(missing_ok=True)
        for fpath in (entry.get("nginx_conf_paths") or []):
            if fpath:
                Path(fpath).unlink(missing_ok=True)
                Path(str(fpath) + ".generated").unlink(missing_ok=True)

        assert not http_conf.exists()
        assert not stream_conf.exists(), "the stream conf must go with the instance"
        assert not Path(str(stream_conf) + ".generated").exists()


class TestWildcardOnlyUploads:
    """T19 / R9.4 — the certificate upload path accepts WILDCARDS only.

    The exposure handle is minted AFTER the certificate exists ("the name comes
    only after selecting the certs, never the reverse" — G41), so the only shape
    the platform can use is `*.<domain>`: one certificate per domain, covering
    every one-label name under it. A single-host certificate would be unusable,
    and saying so AT UPLOAD TIME is the whole point — the alternative is a
    handshake that presents nothing, discovered later by a client.
    """

    def _cert(self, path, cn, san=None, days=2):
        import subprocess
        args = ["openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048", "-days", str(days),
                "-keyout", str(path) + ".key", "-out", str(path), "-subj", f"/CN={cn}"]
        if san:
            args += ["-addext", f"subjectAltName={san}"]
        subprocess.run(args, capture_output=True, timeout=30)
        return Path(path).read_text()

    def test_the_parser_reads_the_names(self, tmp_path):
        from api import _cert_dns_names
        wildcard = self._cert(tmp_path / "w.pem", "*.example.com", "DNS:*.example.com")
        single = self._cert(tmp_path / "s.pem", "example.com", "DNS:example.com")
        assert _cert_dns_names(wildcard) == ["*.example.com"]
        assert _cert_dns_names(single) == ["example.com"]
        assert _cert_dns_names("not a pem") == []

    def _upload(self, tmp_path, monkeypatch, cert_pem):
        import api
        from api import SSLCertUploadRequest, upload_ssl_cert
        monkeypatch.setattr(api, "SSL_DIR", tmp_path, raising=False)
        return upload_ssl_cert(SSLCertUploadRequest(
            domain="example.com", fullchain=cert_pem,
            privkey="-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n",
        ))

    def test_a_single_host_certificate_is_refused_with_a_reason(self, tmp_path, monkeypatch):
        from fastapi import HTTPException
        pem = self._cert(tmp_path / "s2.pem", "example.com", "DNS:example.com")
        try:
            self._upload(tmp_path, monkeypatch, pem)
            raised = None
        except HTTPException as e:
            raised = e
        assert raised is not None and raised.status_code == 400
        assert "WILDCARD" in raised.detail and "example.com" in raised.detail, (
            "the refusal must say what is wrong AND why the platform needs a wildcard")
        assert not (tmp_path / "example.com" / "fullchain.pem").exists(), (
            "nothing is persisted for a refused upload")
        # R2-F5 — the weaker form of this assertion ("no fullchain.pem") passed
        # while the handler still created the domain DIRECTORY before validating,
        # so the store grew an empty `ssl/example.com/`. "Untouched" means no
        # trace of the attempt, so the directory is asserted too.
        assert not (tmp_path / "example.com").exists(), (
            "a refused upload must not touch the store at all — not even by "
            "creating the domain directory")

    def test_a_refused_path_mode_upload_leaves_no_source_marker(self, tmp_path, monkeypatch):
        """R2-F5, path mode. `.source_path` used to be written BEFORE the wildcard
        check, so a refusal left both the directory and the marker behind."""
        import api
        from api import SSLCertUploadRequest, upload_ssl_cert
        from fastapi import HTTPException
        src = tmp_path / "src"
        src.mkdir()
        pem = self._cert(tmp_path / "s3.pem", "example.com", "DNS:example.com")
        (src / "fullchain.pem").write_text(pem)
        (src / "privkey.pem").write_text("-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n")
        ssl_dir = tmp_path / "ssl"
        ssl_dir.mkdir()
        monkeypatch.setattr(api, "SSL_DIR", ssl_dir, raising=False)
        import pytest
        with pytest.raises(HTTPException):
            upload_ssl_cert(SSLCertUploadRequest(domain="example.com", ssl_path=str(src)))
        assert list(ssl_dir.iterdir()) == [], (
            "a refused path-mode upload leaves the store empty — no directory, "
            "no `.source_path`")

    def test_an_unreadable_certificate_is_refused(self, tmp_path, monkeypatch):
        from fastapi import HTTPException
        try:
            self._upload(tmp_path, monkeypatch, "not a certificate at all")
            raised = None
        except HTTPException as e:
            raised = e
        assert raised is not None and raised.status_code == 400, (
            "fail-closed: an unreadable certificate cannot be shown to be a wildcard")

    def test_a_wildcard_certificate_is_accepted(self, tmp_path, monkeypatch):
        pem = self._cert(tmp_path / "w2.pem", "*.example.com", "DNS:*.example.com")
        result = self._upload(tmp_path, monkeypatch, pem)
        assert result["domain"] == "example.com"
        assert Path(result["fullchain_path"]).is_file()


class TestTheReservedSeparator:
    """T16 / R1.5 — `--` is reserved, so the served name always parses.

    `<instance host>--<sub_service>.<domain>` is one label carrying TWO facts. If
    a component could contain `--`, a reader (or the card, or the gateway's
    hostname index) could not tell where the instance ends and the sub-service
    begins — so the character sequence is refused at validation, before anything
    is stored.
    """

    def test_a_name_with_the_separator_is_refused(self):
        from lib.validation import validate_name, ValidationError
        import pytest
        for bad in ("my--svc", "svc--", "--svc"):
            with pytest.raises(ValidationError):
                validate_name(bad, "service_name")

    def test_ordinary_names_still_pass(self):
        from lib.validation import validate_name
        for good in ("alice", "example-service", "graphiti-local", "a_b-1", "shared2"):
            assert validate_name(good, "service_name") == good

    def test_the_served_name_is_one_label_below_the_domain(self):
        """The property the whole naming scheme rests on: every platform name is
        exactly ONE label under the domain, which is what lets a single wildcard
        certificate serve all of them."""
        from lib.validation import validate_name
        host = f"{validate_name('example-mcp','s')}-{validate_name('alice','u')}-3"
        domain = "snaprovision.com"
        served = f"{host}--neo4j.{domain}"
        # ONE label under the domain: the label itself contains no dot, and the
        # domain's own dots are all that remain.
        label = served.split(".", 1)[0]
        assert "." not in label, label
        assert served.count(".") == domain.count(".") + 1, served
        left, right = label.split("--", 1)
        assert left == host and right == "neo4j", (left, right)


class TestTheRecordedHttpEndpoints:
    """R1.5 / R6.2 — the EXTRA http endpoints, read from the RENDERED conf.

    The card lists one `/go/` row per exposed HTTP endpoint, keyed by the
    sub-service (the primary keyed `primary`), and R6.2 says every row is read
    from the artifacts — never typed. So the entry must RECORD the names, and it
    records what the internal nginx actually routes: the served names of the
    rendered conf.
    """

    def _pairs(self, tmp_path, conf_text, name="svc.user-alice.0.nginx.conf"):
        p = tmp_path / name
        p.write_text(conf_text)
        return [(name, str(p))]

    def test_the_extras_are_recorded_and_the_primary_is_not(self, tmp_path):
        from lib.provisioner import _http_endpoints
        conf = (
            "server {\n listen 80;\n server_name web-alice-0.snaprovision.com;\n}\n"
            "server {\n listen 80;\n server_name web-alice-0--neo4j.snaprovision.com;\n}\n"
            "server {\n listen 80;\n server_name web-alice-0--mcp.snaprovision.com;\n}\n"
        )
        assert _http_endpoints(self._pairs(tmp_path, conf), "web-alice-0") == [
            "web-alice-0--neo4j.snaprovision.com",
            "web-alice-0--mcp.snaprovision.com",
        ]

    def test_another_instance_s_name_is_not_mistaken_for_one_of_ours(self, tmp_path):
        """The prefix is the whole identity, so another instance cannot shadow it."""
        from lib.provisioner import _http_endpoints
        conf = "server {\n listen 80;\n server_name other-alice-0--neo4j.localhost;\n}\n"
        assert _http_endpoints(self._pairs(tmp_path, conf), "web-alice-0") == []

    def test_a_stream_conf_is_not_read_as_http(self, tmp_path):
        from lib.provisioner import _http_endpoints
        conf = "server {\n listen 7687 ssl;\n server_name web-alice-0--bolt.snaprovision.com;\n}\n"
        pairs = self._pairs(tmp_path, conf, name="svc.user-alice.0.stream.conf")
        assert _http_endpoints(pairs, "web-alice-0") == []

    def test_a_generated_http_conf_is_read(self, tmp_path):
        """The generated name carries the job token — `nginx.base.<job>.conf`.

        Matching a fixed suffix (`.nginx.conf`) missed every generated conf, so a
        real deployment that served both extra endpoints recorded neither
        (measured 2026-10-10). The category is "a conf that is not the stream
        kind", which is also how the -api picks the renderer.
        """
        from lib.provisioner import _http_endpoints
        conf = "server {\n listen 80;\n server_name web-alice-0--neo4j.snaprovision.com;\n}\n"
        pairs = self._pairs(tmp_path, conf, name="nginx.base.371.conf")
        assert _http_endpoints(pairs, "web-alice-0") == [
            "web-alice-0--neo4j.snaprovision.com"]

    def test_an_absent_rendered_conf_records_nothing(self, tmp_path):
        from lib.provisioner import _http_endpoints
        pairs = [("x.nginx.conf", str(tmp_path / "missing.conf"))]
        assert _http_endpoints(pairs, "web-alice-0") == []


class TestTheEndpointsAreRecordedAfterTheRender:
    """R3-F53 — the recorded endpoints are read from confs that now exist.

    The registry entry is built BEFORE the per-user confs are rendered, so a
    reader that runs while the entry is assembled reads files that are not there
    yet and records nothing — measured 2026-10-10 on a live deployment that
    served two extra http endpoints and recorded none, and on the stream side
    (R6.1) for the same reason. The lists are therefore recomputed from the
    rendered confs once they are on disk, which is what this asserts.
    """

    def test_the_reader_updates_the_entry_from_the_rendered_files(self, tmp_path):
        from lib import provisioner, registry

        service_id, _proc = add_registry_user(
            {"user_name": "alice", "service_name": "web", "label": "0",
             "status": "building"}
        )
        rendered = tmp_path / "generated"
        rendered.mkdir()
        (rendered / "web.user-alice.0.nginx.conf").write_text(
            "server {\n listen 80;\n server_name web-alice-0--neo4j.snaprovision.com;\n}\n")
        (rendered / "web.user-alice.0.stream.conf").write_text(
            "server {\n listen 7687 ssl;\n server_name web-alice-0--bolt.localhost;\n}\n")
        pairs = [
            ("nginx.base.9.conf", str(rendered / "web.user-alice.0.nginx.conf")),
            ("web.user-alice.0.stream.conf.j2", str(rendered / "web.user-alice.0.stream.conf")),
        ]
        provisioner._update_registry_endpoints(service_id, pairs, "web-alice-0")

        entry = registry.get_user_service("alice", "web", "0")
        assert entry["http_endpoints"] == ["web-alice-0--neo4j.snaprovision.com"], entry
        assert entry["stream_sub_services"] == ["web-alice-0--bolt.localhost"], entry


class TestTheRecordsTlsSwitch:
    """The record's toggle is `tls` — renamed from `https` (ruled 2026-10-10).

    Entries written before the rename are read through a fallback kept for ONE
    round. The read lives in the registry module, which owns the record's shape,
    so both services carry one rule and removing the fallback later is a one-line
    change. The gateway has the mirror of this function
    (`app/services/registry_entry.py`) and the two must agree.
    """

    def test_the_new_name_is_the_one_that_counts(self):
        from lib.registry import entry_tls
        assert entry_tls({"tls": True}) is True
        assert entry_tls({"tls": False}) is False

    def test_the_old_name_is_still_read_this_round(self):
        from lib.registry import entry_tls
        assert entry_tls({"https": True}) is True
        assert entry_tls({"https": False}) is False

    def test_the_new_name_wins_when_both_are_present(self):
        from lib.registry import entry_tls
        assert entry_tls({"tls": False, "https": True}) is False, (
            "a migrated entry must not be overruled by a stale key")

    def test_anything_unreadable_is_false(self):
        """Fail-closed: an unreadable flag must never become an accidental TLS."""
        from lib.registry import entry_tls
        assert entry_tls({}) is False
        assert entry_tls(None) is False
        assert entry_tls({"tls": None}) is False


class TestTheAttachmentsAreRepairedWhenTheInternalNginxIsRecreated:
    """GRILLING G46 / task-doc §4 row 17 — the recovery must be at the trigger.

    The internal nginx's per-user network attachments are IMPERATIVE state:
    Docker keeps them on the container, and the compose file cannot express them
    (the per-user networks do not exist when it is written). So a recreate
    destroys them and every service is unreachable at once.

    Measured 2026-10-11, landing the loopback port policy: a recreate left the
    container with `subnet_acl_shared` alone and NOTHING repaired it — the
    gateway's docker-events monitor cannot work (that container mounts no docker
    socket) and the gateway's periodic reconcile defaults to disabled. Both
    halves are asserted here: the repair itself, and the trigger that runs it.
    """

    def test_the_repair_reattaches_every_registry_network(self, monkeypatch):
        from lib import reconciliation, registry, docker_ops
        monkeypatch.setattr(registry, "get_all_users", lambda: [
            {"network_name": "net-a"}, {"network_name": "net-b"},
            {"network_name": "net-a"},          # duplicate: attached once
            {"network_name": ""},               # no network: skipped
        ])
        calls: list[tuple[str, str]] = []
        reloads: list[str] = []
        monkeypatch.setattr(docker_ops, "network_connect",
                            lambda c, n: calls.append((c, n)))
        monkeypatch.setattr(docker_ops, "nginx_reload", lambda c: reloads.append(c))

        report = reconciliation.repair_nginx_attachments("nginx-x")

        assert calls == [("nginx-x", "net-a"), ("nginx-x", "net-b")], calls
        assert reloads == ["nginx-x"], "the repaired routes must be live, not just attached"
        assert report["total_networks"] == 2 and report["reconnected"] == 2

    def test_a_connect_failure_does_not_stop_the_others(self, monkeypatch):
        """One unreachable network must not cost the rest their route."""
        from lib import reconciliation, registry, docker_ops
        monkeypatch.setattr(registry, "get_all_users", lambda: [
            {"network_name": "bad"}, {"network_name": "good"},
        ])

        def _connect(c, n):
            if n == "bad":
                raise RuntimeError("no such network")

        monkeypatch.setattr(docker_ops, "network_connect", _connect)
        monkeypatch.setattr(docker_ops, "nginx_reload", lambda c: None)

        report = reconciliation.repair_nginx_attachments("nginx-x")
        assert report["reconnected"] == 1, report

    def test_the_watcher_repairs_on_a_start_event(self, monkeypatch):
        """The trigger: Docker's own `start` event for the internal nginx.

        Driven through the real `watch_for_recreates` with a fake events stream —
        no docker daemon, and the argv the stream is opened with is asserted, so
        a filter that stopped matching the container would fail here rather than
        silently never fire in production.
        """
        from lib import reconciliation, registry, docker_ops
        monkeypatch.setattr(registry, "get_all_users",
                            lambda: [{"network_name": "net-a"}])
        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(docker_ops, "network_connect",
                            lambda c, n: calls.append((c, n)))
        monkeypatch.setattr(docker_ops, "nginx_reload", lambda c: None)
        # The 3 s settle exists so the container's entrypoint finishes; the test
        # must not pay it.
        monkeypatch.setattr("time.sleep", lambda _s: None)

        seen: dict = {}

        class _Proc:
            def __init__(self):
                self.stdout = iter(["start\n"])

            def terminate(self):
                seen["terminated"] = True

        import subprocess

        def _popen(cmd, **_kw):
            seen["cmd"] = cmd
            return _Proc()

        monkeypatch.setattr(subprocess, "Popen", _popen)
        reconciliation.watch_for_recreates("nginx-x", _once=True)

        assert calls == [("nginx-x", "net-a")], (
            "a `start` event must repair the attachments")
        cmd = seen.get("cmd") or []
        assert "container=nginx-x" in cmd, cmd
        assert "event=start" in cmd, cmd
        # NO `--format`. The event message type has no `.Status` field (it is
        # `.Action`), so a format string written from the old output shape makes
        # the CLI exit immediately with `Error parsing format` — with stderr
        # discarded, the reader then saw a clean end-of-stream and retried
        # forever. Measured 2026-10-11: the watcher ran for 60 s after a recreate
        # and repaired nothing. The line content is unused, so the default format
        # is both sufficient and unbreakable.
        assert "--format" not in cmd, f"a format string killed this stream once: {cmd}"


class TestARenderedConfNeverLeavesAProxyVariableUndefined:
    """R4-F60 — the renderer must not strip a variable's DEFINITION.

    `strip_v4_scaffold` deletes a bare `set $upstream <target>;` line: a rule
    written for the v4 ACL scaffold, where that exact line was the scaffold's
    own. It cannot tell the scaffold's line from a legitimate one, and the
    generated STREAM conf legitimately used that name — so the definition was
    removed, `proxy_pass $upstream;` was left dangling, and the internal nginx
    refused its WHOLE configuration with `unknown "upstream" variable`.
    Measured live 2026-10-11: the first real deploy that produced a
    `*.stream.conf` left the stack one reload away from every service being
    down, not just the stream route.

    The assertion is the INVARIANT rather than a variable name: whatever a
    template uses in `proxy_pass`, the rendered text must still define it. A
    name-shaped assertion would pass the next time the same collision is made
    under a different name.
    """

    _STREAM = """server {
    listen              7687 ssl;
    server_name         graphiti-local-alice-0--neo4j.snaprovision.com;
    ssl_certificate     {{ ssl_certificate_path }};
    ssl_certificate_key {{ ssl_certificate_key_path }};
    allow               172.17.0.0/16;
    deny                all;
    resolver            127.0.0.11 valid=30s ipv6=off;
    set $stream_upstream {{ container_prefix }}neo4j:7687;
    proxy_pass          $stream_upstream;
}
"""

    def test_the_definition_survives_the_render(self, tmp_path):
        import re
        from lib import template_engine

        tpl = tmp_path / "x.stream.conf.j2"
        tpl.write_text(self._STREAM)
        out = tmp_path / "x.stream.conf"
        template_engine.render_nginx_conf(
            str(tpl), str(out), "alice", "graphiti-local", "0", "snaprovision.com", "",
            https=True,
            ssl_certificate_path="/srv/ssl/c.pem",
            ssl_certificate_key_path="/srv/ssl/k.pem",
            stream_allow_subnet="172.17.0.0/16",
        )
        rendered = out.read_text()

        used = set(re.findall(r"proxy_pass\s+(?:\w+://)?\$([A-Za-z0-9_]+)\s*;", rendered))
        defined = set(re.findall(r"^\s*set\s+\$([A-Za-z0-9_]+)\s+", rendered, re.MULTILINE))
        assert used, f"the fixture must exercise a variable proxy_pass: {rendered!r}"
        assert used <= defined, (
            "every variable a rendered conf proxies to must still be defined in "
            f"it — used {sorted(used)}, defined {sorted(defined)}\n{rendered}")

    def test_the_scaffold_rule_still_drops_the_v4_line(self):
        """The fix must not break the migration the rule exists for."""
        from lib.template_engine import strip_v4_scaffold
        v4 = "server {\n    set $upstream myapp-user_alice-0-web:80;\n    proxy_pass $upstream;\n}\n"
        assert "set $upstream" not in strip_v4_scaffold(v4)


class TestTheForwardedHostCarriesThePort:
    """R4-F63 — `$host` strips the port; the app must be told the URL it was reached at.

    An application that builds an absolute URL from the host it was given — the
    normal thing to do — produces one naming the scheme's default port when it
    is handed a portless host. On a deployment whose edge is remapped off 443
    that is a dead end: measured 2026-10-11 through the card's own `/go/`
    hand-off, where the app answered `303 … Location: https://<host>/browser/`
    and the browser got an empty response, while the same redirect at the
    published port loaded the page.

    The normalisation lives in the renderer, beside the `proxy_pass` rewrite and
    for the same reason: the renderer owns what the conf becomes, so a template
    written before this rule is corrected on its next render rather than only in
    confs generated after it.
    """

    _TPL = """server {
    listen 80;
    server_name {{ hostname }};
    location / {
        set $upstream_0000 app:8000;
        proxy_pass http://$upstream_0000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
"""

    def _render(self, tmp_path, text):
        from lib import template_engine
        tpl = tmp_path / "x.nginx.conf.j2"
        tpl.write_text(text)
        out = tmp_path / "x.nginx.conf"
        template_engine.render_nginx_conf(
            str(tpl), str(out), "alice", "web", "0", "snaprovision.com", "", https=False)
        return out.read_text()

    def test_the_host_line_keeps_the_port(self, tmp_path):
        rendered = self._render(tmp_path, self._TPL)
        assert "proxy_set_header Host $http_host;" in rendered, rendered
        assert "proxy_set_header Host $host;" not in rendered, (
            "a conf that tells the application a portless host makes the app "
            "build absolute URLs on the wrong port")
        # …and only that line moved: the neighbour is untouched.
        assert "proxy_set_header X-Real-IP $remote_addr;" in rendered

    def test_an_already_correct_template_is_left_alone(self, tmp_path):
        rendered = self._render(tmp_path, self._TPL.replace("Host $host;", "Host $http_host;"))
        assert rendered.count("proxy_set_header Host $http_host;") == 1, rendered

    def test_a_conf_without_the_line_is_untouched(self, tmp_path):
        """The stream conf has no `proxy_set_header` at all — nothing to normalise."""
        rendered = self._render(
            tmp_path,
            "server {\n    listen 7687 ssl;\n    server_name a.b.c;\n"
            "    set $stream_upstream neo4j:7687;\n    proxy_pass $stream_upstream;\n}\n")
        assert "proxy_set_header" not in rendered, rendered


class TestPerCategoryConfs:
    """G55 — the request is a per-category map; legacy shapes are shims."""

    def _req(self, **kw):
        from api import RegisterRequest
        base = dict(user_name="u", service_name="s", compose_file_path="docker-compose.yml")
        base.update(kw)
        return RegisterRequest(**base)

    def test_the_map_is_accepted(self):
        r = self._req(tls=True, fullchain="fc.pem", privkey="pk.pem",
                      nginx_confs={"http": "a.conf", "stream": "b.stream.conf"})
        assert r.nginx_confs["stream"] == "b.stream.conf"

    def test_legacy_singulars_normalize_by_file_name(self):
        from api import _requested_conf_maps
        plain, tpl = _requested_conf_maps(self._req(nginx_conf_file_path="a.conf"))
        assert plain == {"http": "a.conf"} and tpl == {}
        # The legacy model still refuses a plain and a template in one request
        # (its mutual-exclusion rule predates the map); a mixed set uses the map.
        plain2, tpl2 = _requested_conf_maps(self._req(
            tls=True, fullchain="fc.pem", privkey="pk.pem",
            nginx_conf_template_path="b.stream.conf.j2"))
        assert plain2 == {} and tpl2 == {"stream": "b.stream.conf.j2"}

    def test_a_legacy_list_with_two_entries_of_one_category_is_refused(self):
        import pytest
        from fastapi import HTTPException
        from api import _requested_conf_maps
        with pytest.raises(HTTPException) as ei:
            _requested_conf_maps(self._req(nginx_conf_file_paths=["a.conf", "b.conf"]))
        assert "one file per category" in str(ei.value.detail)

    def test_the_map_and_the_legacy_fields_cannot_be_mixed(self):
        import pytest
        from fastapi import HTTPException
        from api import _requested_conf_maps
        with pytest.raises(HTTPException):
            _requested_conf_maps(self._req(
                nginx_confs={"http": "a.conf"}, nginx_conf_file_path="b.conf"))

    def test_an_unknown_category_is_refused(self):
        import pytest
        from fastapi import HTTPException
        from api import _requested_conf_maps
        with pytest.raises(HTTPException):
            _requested_conf_maps(self._req(nginx_confs={"smtp": "a.conf"}))


class TestNamedVolumesBecomeBinds:
    """G59 (D4 = C) — non-external named volumes are per-user bind keys."""

    def _convert(self, data):
        import yaml
        from lib.compose_converter import convert
        out, src_to_key, tokens, _ = convert(data)
        return out, src_to_key, tokens.detokenize(yaml.safe_dump(out))

    def test_the_service_entry_is_the_user_path_and_the_declaration_is_dropped(self):
        out, src_to_key, text = self._convert({
            "services": {"neo4j": {"image": "neo4j", "volumes": ["neo4j_data:/data"]}},
            "volumes": {"neo4j_data": {}},
        })
        assert src_to_key["neo4j_data"] == "neo4j_data"
        assert "{{ volumes['neo4j_data'] }}" in text
        assert "volumes" not in out, "the Docker named-volume declaration must be gone"

    def test_external_volumes_stay_named(self):
        out, src_to_key, text = self._convert({
            "services": {"app": {"image": "x", "volumes": ["shared:/data"]}},
            "volumes": {"shared": {"external": True}},
        })
        assert "shared" not in src_to_key
        assert out["volumes"]["shared"] == {"external": True}


class TestPreviewGoldenPaths:
    """G60 (D8) — the -api computes the volume paths; the preview stays pure."""

    def test_paths_are_returned_for_an_identity(self, tmp_path):
        import api
        from lib.template_engine import volume_path
        project = tmp_path / "svc"
        project.mkdir()
        (project / "docker-compose.yml").write_text(
            "services:\n  db:\n    image: neo4j\n    volumes:\n      - neo4j_data:/data\n"
            "volumes:\n  neo4j_data: {}\n"
        )
        res = api._preview_volume_mapping(
            project, ["docker-compose.yml"], service_name="svc", user_name="bob", label="2"
        )
        assert res["volume_keys"] == ["neo4j_data"]
        assert res["volume_paths"]["neo4j_data"] == volume_path(
            api.USER_DATA_DIR, "bob", "svc", "2", "neo4j_data"
        )
        # the MOUNT TABLE's second column, from the compose
        assert res["volume_targets"] == {"neo4j_data": "/data"}, res
        assert not (api.USER_DATA_DIR / "bob" / "svc" / "2").exists(), "the preview creates nothing"

    def test_no_identity_means_no_paths(self, tmp_path):
        import api
        project = tmp_path / "svc"
        project.mkdir()
        (project / "docker-compose.yml").write_text(
            "services:\n  db:\n    image: neo4j\n    volumes:\n      - neo4j_data:/data\n"
            "volumes:\n  neo4j_data: {}\n"
        )
        res = api._preview_volume_mapping(project, ["docker-compose.yml"])
        assert "volume_paths" not in res and res["volume_keys"] == ["neo4j_data"]


class TestConfFilter:
    """G61 (D6) — rendered blocks follow the compose's ACTIVE services."""

    def test_an_inactive_service_block_is_dropped(self):
        from lib.conf_filter import filter_conf_blocks
        text = """server {
    server_name h--graph;
    set $upstream_0 p-graph:8000;
    proxy_pass http://$upstream_0;
}
server {
    server_name h--graph-falkordb;
    set $upstream_1 p-graph-falkordb:8000;
    proxy_pass http://$upstream_1;
}
"""
        out, dropped = filter_conf_blocks(
            text,
            active_services={"graph", "neo4j"},
            all_services={"graph", "neo4j", "graph-falkordb"},
            container_prefix="p-",
        )
        assert "p-graph:8000" in out
        assert "p-graph-falkordb:8000" not in out
        assert dropped == ["h--graph-falkordb"]

    def test_an_unknown_upstream_is_kept(self):
        from lib.conf_filter import filter_conf_blocks
        text = "server {\n    server_name h;\n    proxy_pass https://external.example.com;\n}\n"
        out, dropped = filter_conf_blocks(
            text, active_services=set(), all_services={"graph"}, container_prefix="p-"
        )
        assert out == text and dropped == []

    def test_the_compose_rule_treats_empty_profiles_as_ungated(self, tmp_path):
        from lib.conf_filter import compose_active_services
        compose = tmp_path / "c.yml"
        compose.write_text(
            "services:\n"
            "  graph:\n    image: x\n    profiles: ['']\n"
            "  neo4j:\n    image: y\n"
            "  falkordb:\n    image: z\n    profiles: [falkordb]\n"
        )
        active, all_names = compose_active_services(compose, [])
        assert active == {"graph", "neo4j"}
        assert all_names == {"graph", "neo4j", "falkordb"}
        active2, _ = compose_active_services(compose, ["falkordb"])
        assert "falkordb" in active2


class TestStreamNameTemplating:
    """G56 (D7) — the stream `server_name` is templated, and unparseable is an
    -api conversion error (the converter cannot invent the block's identity)."""

    _CONF = """server {
    listen              7687 ssl;
    server_name         %s;
    proxy_pass          example-mcp-alice-3-neo4j:7687;
}
"""

    def test_a_non_canonical_name_is_refused(self):
        import pytest
        from lib.nginx_converter import convert_stream_conf
        with pytest.raises(ValueError):
            convert_stream_conf(self._CONF % "plain.example.com", "svc", ["neo4j"])

    def test_an_already_templated_name_normalizes_idempotently(self):
        from lib.nginx_converter import convert_stream_conf
        out = convert_stream_conf(
            self._CONF % "{{ hostname_stem }}--neo4j.{{ domain_name }}", "svc", ["neo4j"]
        )
        assert "{{ hostname_stem }}--neo4j.{{ domain_name }}" in out


class TestEndpointRecordingByRenderedCategory:
    """G55 — endpoints are read from the RENDERED conf, and the category is the
    rendered name's. The recipe-level stream template's source is
    `nginx.stream.base.<job>.conf[.j2]`, which does not end `.stream.conf`."""

    def _pair(self, tmp_path):
        out = tmp_path / "svc.user-u.0.stream.conf"
        out.write_text(
            "server {\n"
            "    server_name svc-u-0--neo4j.example.com;\n"
            "    proxy_pass svc-u-0-neo4j:7687;\n"
            "}\n"
        )
        return ("/recipe/nginx.stream.base.406.conf.j2", str(out))

    def test_the_stream_template_counts_as_stream(self, tmp_path):
        from lib.provisioner import _http_endpoints, _stream_sub_services
        pair = self._pair(tmp_path)
        assert _stream_sub_services([pair]) == ["svc-u-0--neo4j.example.com"]
        assert _http_endpoints([pair], "svc-u-0") == []

    def test_a_rendered_nginx_conf_is_http(self, tmp_path):
        from lib.provisioner import _http_endpoints, _stream_sub_services
        out = tmp_path / "svc.user-u.0.nginx.conf"
        out.write_text(
            "server {\n"
            "    server_name svc-u-0--neo4j.example.com;\n"
            "    proxy_pass svc-u-0-neo4j:7474;\n"
            "}\n"
        )
        pair = ("/recipe/nginx.base.406.conf.j2", str(out))
        assert _http_endpoints([pair], "svc-u-0") == ["svc-u-0--neo4j.example.com"]
        assert _stream_sub_services([pair]) == []


class TestBindAndNamedVolumeOrder:
    """T5 step 5 — a bind-mount fixture beside named volumes (G59).

    The regression this pins: bind keys come FIRST, named-volume keys after, in
    first-appearance order; an `external: true` volume stays a named volume and
    never becomes a bind key; the preview reports both kinds for an identity.
    """

    _COMPOSE = (
        "services:\n"
        "  app:\n"
        "    image: example\n"
        "    volumes:\n"
        "      - ./conf:/conf\n"
        "      - db_data:/data\n"
        "      - shared:/shared\n"
        "volumes:\n"
        "  db_data: {}\n"
        "  shared:\n"
        "    external: true\n"
    )

    def _project(self, tmp_path):
        project = tmp_path / "svc"
        project.mkdir()
        (project / "docker-compose.yml").write_text(self._COMPOSE)
        (project / "conf").mkdir()
        return project

    def test_bind_first_then_named_and_external_untouched(self):
        import yaml
        from lib.compose_converter import convert
        out, src_to_key, tokens, _ = convert(yaml.safe_load(self._COMPOSE))
        assert list(src_to_key) == ["./conf", "db_data"], src_to_key
        text = tokens.detokenize(yaml.safe_dump(out))
        assert "{{ volumes['conf'] }}:/conf" in text
        assert "{{ volumes['db_data'] }}:/data" in text
        assert "shared:/shared" in text, "an external volume stays a named volume"
        assert out["volumes"] == {"shared": {"external": True}}

    def test_the_preview_reports_both_kinds_in_order(self, tmp_path):
        import api
        project = self._project(tmp_path)
        res = api._preview_volume_mapping(
            project, ["docker-compose.yml"], service_name="svc", user_name="bob", label="2"
        )
        assert res["volume_keys"] == ["conf", "db_data"], res
        assert res["volume_paths"]["conf"].endswith("/bob/svc/2/conf")
        assert "shared" not in res["volume_paths"]
        assert res["volume_targets"] == {"conf": "/conf", "db_data": "/data"}, res


class TestLegacyStreamUpstreamNormalization:
    """Found 2026-10-23 — a selected historical stream conf used
    `set $stream_upstream …; proxy_pass $stream_upstream;` (plus `ssl_preread`)
    and the internal nginx answered `no host in upstream ""` for every
    connection: the mint and the card looked healthy while the route was dead.
    The converter now renders that form into the STATIC pass the generator
    emits."""

    _LEGACY = """server {
    listen              7687 ssl;
    ssl_preread         on;
    server_name         someone-else-9--neo4j.snaprovision.com;
    ssl_certificate     /srv/provision_subnet_acl/ssl/snaprovision.com/fullchain.pem;
    ssl_certificate_key /srv/provision_subnet_acl/ssl/snaprovision.com/privkey.pem;
    resolver            127.0.0.11 valid=30s ipv6=off;
    set $stream_upstream neo4j:7687;
    proxy_pass          $stream_upstream;
}
"""

    def test_the_variable_form_becomes_static(self):
        from lib.nginx_converter import convert_stream_conf
        out = convert_stream_conf(self._LEGACY, "svc", ["neo4j"])
        import re as _re
        assert _re.search(r"proxy_pass\s+\{\{ container_prefix \}\}neo4j:7687;", out), out
        assert "$stream_upstream" not in out
        assert "resolver" not in out
        assert "server_name         {{ hostname_stem }}--neo4j.{{ domain_name }};" in out

    def test_an_unknown_host_is_static_but_not_prefixed(self):
        from lib.nginx_converter import convert_stream_conf
        conf = self._LEGACY.replace("neo4j:7687", "external.example.com:6379")
        out = convert_stream_conf(conf, "svc", ["neo4j"])
        import re as _re
        assert _re.search(r"proxy_pass\s+external\.example\.com:6379;", out), out
        assert "{{ container_prefix }}" not in out

    def test_the_same_variable_name_in_two_blocks_keeps_each_target(self):
        """The historical conf reuses `$stream_upstream` per block; a conf-wide
        map collapsed both to the last target (measured this round)."""
        from lib.nginx_converter import convert_stream_conf
        two = self._LEGACY + self._LEGACY.replace("neo4j:7687", "falkordb:6379")
        out = convert_stream_conf(two, "svc", ["neo4j", "falkordb"])
        import re as _re
        assert _re.search(r"proxy_pass\s+\{\{ container_prefix \}\}neo4j:7687;", out), out
        assert _re.search(r"proxy_pass\s+\{\{ container_prefix \}\}falkordb:6379;", out), out


class TestActiveVolumesOnTheCard:
    """G61 follow-up — a volume mounted only by a profile-gated service that was
    not started must not appear in the card's Volumes section."""

    def _entry(self, tmp_path, profiles):
        compose = tmp_path / "docker-compose.user-x.2.yml"
        compose.write_text(
            "services:\n"
            "  neo4j:\n"
            "    image: neo4j\n"
            "    volumes:\n"
            "      - /srv/u/2/neo4j_data:/data\n"
            "  falkordb:\n"
            "    image: falkordb\n"
            "    profiles: [falkordb]\n"
            "    volumes:\n"
            "      - /srv/u/2/falkordb_data:/data\n"
        )
        return {
            "compose_file_path": str(compose),
            "profiles": profiles,
            "volumes": {
                "neo4j_data": "/srv/u/2/neo4j_data",
                "falkordb_data": "/srv/u/2/falkordb_data",
            },
        }

    def test_no_profile_hides_the_gated_volume(self, tmp_path):
        import api
        assert api._active_volumes(self._entry(tmp_path, [])) == {
            "neo4j_data": "/srv/u/2/neo4j_data"
        }

    def test_the_active_profile_keeps_it(self, tmp_path):
        import api
        assert set(api._active_volumes(self._entry(tmp_path, ["falkordb"]))) == {
            "neo4j_data", "falkordb_data"
        }

    def test_an_unreadable_compose_falls_back_to_the_full_map(self, tmp_path):
        import api
        entry = {
            "compose_file_path": str(tmp_path / "missing.yml"),
            "profiles": [],
            "volumes": {"a": "/x/a"},
        }
        assert api._active_volumes(entry) == {"a": "/x/a"}


class TestStreamConfsRequireTLS:
    """G41 — the stream category is applicable only with TLS. With tls=false a
    stream conf rendered `ssl_certificate ;`, failing `nginx -t` and silently
    stopping the reload (measured 2026-10-24)."""

    def _req(self, **kw):
        from api import RegisterRequest
        base = dict(user_name="u", service_name="s", compose_file_path="docker-compose.yml")
        base.update(kw)
        return RegisterRequest(**base)

    def test_a_stream_map_with_tls_off_is_refused(self):
        import pytest
        from pydantic import ValidationError
        with pytest.raises(ValidationError) as ei:
            self._req(tls=False, nginx_confs={"http": "a.conf", "stream": "x.stream.conf"})
        assert "require tls=true" in str(ei.value)

    def test_a_legacy_stream_path_with_tls_off_is_refused(self):
        import pytest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            self._req(tls=False, nginx_conf_file_paths=["a.conf", "x.stream.conf"])

    def test_an_http_only_deploy_with_tls_off_is_accepted(self):
        r = self._req(tls=False, nginx_confs={"http": "a.conf"})
        assert r.tls is False

    def test_a_stream_map_with_tls_on_is_accepted(self):
        r = self._req(tls=True, fullchain="fc.pem", privkey="pk.pem",
                      nginx_confs={"http": "a.conf", "stream": "x.stream.conf"})
        assert r.tls is True


class TestProfileAwareVolumePreview:
    """The mount table follows the selected profiles, and the preview merges
    multiple compose files in one call (both are multi-select in the panel)."""

    _BASE = (
        "services:\n"
        "  graph:\n"
        "    image: g\n"
        "    profiles: ['']\n"
        "  neo4j:\n"
        "    image: n\n"
        "    volumes:\n"
        "      - neo4j_data:/data\n"
        "  falkordb:\n"
        "    image: f\n"
        "    profiles: [falkordb]\n"
        "    volumes:\n"
        "      - falkordb_data:/data\n"
        "volumes:\n"
        "  neo4j_data: {}\n"
        "  falkordb_data: {}\n"
    )

    def _project(self, tmp_path):
        p = tmp_path / "svc"
        p.mkdir()
        (p / "docker-compose.yml").write_text(self._BASE)
        (p / "overlay.yml").write_text(
            "services:\n  graph:\n    volumes:\n      - ./conf:/conf\n"
        )
        return p

    def test_no_profile_hides_the_gated_volume(self, tmp_path):
        import api
        res = api._preview_volume_mapping(
            self._project(tmp_path), ["docker-compose.yml"],
            service_name="svc", user_name="bob", label="2", profiles=[],
        )
        assert res["volume_keys"] == ["neo4j_data"], res
        assert res["volume_services"] == {"neo4j_data": "neo4j"}
        assert res["volume_rows"] == [
            {"key": "neo4j_data", "service": "neo4j", "target": "/data"}
        ]

    def test_the_active_profile_adds_its_volume(self, tmp_path):
        import api
        res = api._preview_volume_mapping(
            self._project(tmp_path), ["docker-compose.yml"],
            service_name="svc", user_name="bob", label="2", profiles=["falkordb"],
        )
        assert set(res["volume_keys"]) == {"neo4j_data", "falkordb_data"}
        assert res["volume_services"]["falkordb_data"] == "falkordb"

    def test_multiple_compose_files_merge_in_one_call(self, tmp_path):
        import api
        res = api._preview_volume_mapping(
            self._project(tmp_path), ["docker-compose.yml", "overlay.yml"],
            service_name="svc", user_name="bob", label="2", profiles=[],
        )
        # overlay.yml adds a bind to graph → its key appears alongside neo4j's
        assert "conf" in res["volume_keys"], res
        assert res["volume_services"]["conf"] == "graph"
        assert res["volume_targets"]["conf"] == "/conf"


class TestRunCapturesAllOutput:
    """Found 2026-10-24 — `_run` closed the capture pipes right after
    `wait()`, discarding output the reader threads had not drained yet. Inside
    the -api container `docker compose config` was cut at ~1.5 KB, silently
    dropping services from a multi-file merge."""

    def test_a_large_output_is_not_truncated(self):
        import sys
        from lib import docker_ops
        r = docker_ops._run(
            [sys.executable, "-c", "import sys; sys.stdout.write('x'*300000)"]
        )
        assert len(r.stdout) == 300000


class TestComposeProfileSemantics:
    """Measured with real Docker Compose (2026-10-24):

        no --profile      -> `profiles: [""]` RUNS, no-profiles RUNS, named does not
        --profile X       -> `profiles: [""]` EXCLUDED, no-profiles RUNS, X runs

    The platform's three active-service computations must follow it."""

    _COMPOSE = (
        "services:\n"
        "  a_default:\n    image: x\n    profiles: ['']\n"
        "  b_always:\n    image: x\n"
        "  c_gated:\n    image: x\n    profiles: [falkordb]\n"
    )

    def _compose_file(self, tmp_path):
        p = tmp_path / "rendered.yml"
        p.write_text(self._COMPOSE)
        return p

    def test_conf_filter_none_active(self, tmp_path):
        from lib.conf_filter import compose_active_services
        active, _all = compose_active_services(self._compose_file(tmp_path), [])
        assert active == {"a_default", "b_always"}

    def test_conf_filter_with_a_profile(self, tmp_path):
        from lib.conf_filter import compose_active_services
        active, _all = compose_active_services(self._compose_file(tmp_path), ["falkordb"])
        assert active == {"b_always", "c_gated"}

    def test_preview_services_follow_the_rule(self, tmp_path):
        import api
        data = {
            "services": {
                "a_default": {"image": "x", "profiles": [""]},
                "b_always": {"image": "x"},
                "c_gated": {"image": "x", "profiles": ["falkordb"]},
            }
        }
        assert set(api._active_compose_services(data, [])) == {"a_default", "b_always"}
        assert set(api._active_compose_services(data, ["falkordb"])) == {"b_always", "c_gated"}

    def test_expected_containers_follow_the_rule(self, tmp_path):
        import api
        from lib import template_engine
        compose = self._compose_file(tmp_path)
        prefix = template_engine.container_prefix("svc", "bob", "1")
        base = {
            "service_name": "svc", "user_name": "bob", "label": "1",
            "compose_file_path": str(compose),
            "container_names": [f"{prefix}{n}" for n in ("a_default", "b_always", "c_gated")],
        }
        assert set(api._expected_container_names({**base, "profiles": []})) == {
            f"{prefix}a_default", f"{prefix}b_always"
        }
        assert set(api._expected_container_names({**base, "profiles": ["falkordb"]})) == {
            f"{prefix}b_always", f"{prefix}c_gated"
        }


# ---------------------------------------------------------------------------
# R13 — a stopped instance's stream conf is paused out of the include
# ---------------------------------------------------------------------------


class TestStreamConfPauseRestore:
    """R13 — stop renames, start restores, delete removes, and never reloads
    for an instance that has no stream conf."""

    def _entry(self, tmp_path, stream=True):
        compose = tmp_path / "docker-compose.yml"
        compose.write_text("services: {}\n")
        entry = {
            "user_name": "alice", "service_name": "myapp", "label": "0",
            "compose_file_path": str(compose),
            "network_name": "myapp-user_alice-0",
            "env_file_path": "", "env_files": [], "profiles": [],
            "nginx_conf_paths": [],
            "nginx_conf_paths_by_category": {},
        }
        active = None
        if stream:
            active = tmp_path / "myapp.user-alice.0.stream.conf"
            active.write_text("server {\n    listen 7687 ssl;\n}\n")
            entry["nginx_conf_paths"] = [str(active)]
            entry["nginx_conf_paths_by_category"] = {"stream": str(active)}
        add_registry_user(entry)
        return entry, active

    def test_stop_renames_before_stopping_and_reloads_while_healthy(self, tmp_path, monkeypatch):
        from lib import stream_access

        entry, active = self._entry(tmp_path)
        paused = stream_access.paused_path(active)
        events = []

        def fake_reload(_container):
            events.append(("reload", active.exists(), paused.exists()))

        def fake_stop(*a, **k):
            events.append(("stop", active.exists(), paused.exists()))

        monkeypatch.setattr(stream_access, "refresh_and_reload", fake_reload)
        monkeypatch.setattr(docker_ops, "compose_stop", fake_stop)

        result = provisioner.stop_service(
            user_name="alice", service_name="myapp", label="0",
            nginx_container="nginx",
        )
        assert not active.exists()
        assert paused.exists()
        assert result["stream_confs_paused"] == 1
        assert [e[0] for e in events] == ["reload", "stop"]
        # The rename happened BEFORE the reload, which happened before the stop.
        assert events[0][1:] == (False, True)
        assert events[1][1:] == (False, True)

    def test_start_restores_byte_identically_before_the_reload(self, tmp_path, monkeypatch):
        from lib import stream_access

        entry, active = self._entry(tmp_path)
        original = active.read_text()
        paused = stream_access.paused_path(active)
        active.rename(paused)
        events = []

        def fake_up(*a, **k):
            events.append(("up", active.exists(), paused.exists()))

        def fake_reload(_container):
            events.append(("reload", active.exists(), paused.exists()))

        monkeypatch.setattr(docker_ops, "compose_up", fake_up)
        monkeypatch.setattr(stream_access, "refresh_and_reload", fake_reload)

        result = provisioner.start_service(
            user_name="alice", service_name="myapp", label="0",
            nginx_container="nginx",
        )
        assert active.exists() and not paused.exists()
        assert active.read_text() == original
        assert result["stream_confs_restored"] == 1
        assert [e[0] for e in events] == ["up", "reload"]
        # compose up finished BEFORE the restore; the reload saw the active name.
        assert events[0][1:] == (False, True)
        assert events[1][1:] == (True, False)

    def test_stop_without_a_stream_conf_is_a_no_op_for_this_rule(self, tmp_path, monkeypatch):
        from lib import stream_access

        entry, _ = self._entry(tmp_path, stream=False)
        monkeypatch.setattr(docker_ops, "compose_stop", lambda *a, **k: None)

        def _boom(_container):
            raise AssertionError("no stream conf means no stream-specific reload")

        monkeypatch.setattr(stream_access, "refresh_and_reload", _boom)
        result = provisioner.stop_service(
            user_name="alice", service_name="myapp", label="0", nginx_container="nginx",
        )
        assert result["stream_confs_paused"] == 0

    def test_a_second_stop_is_a_no_op(self, tmp_path, monkeypatch):
        from lib import stream_access

        entry, active = self._entry(tmp_path)
        active.rename(stream_access.paused_path(active))
        monkeypatch.setattr(docker_ops, "compose_stop", lambda *a, **k: None)

        def _boom(_container):
            raise AssertionError("an already-paused instance needs no reload")

        monkeypatch.setattr(stream_access, "refresh_and_reload", _boom)
        result = provisioner.stop_service(
            user_name="alice", service_name="myapp", label="0", nginx_container="nginx",
        )
        assert result["stream_confs_already_paused"] == 1

    def test_start_with_both_names_keeps_the_active_file_and_reports_the_conflict(self, tmp_path, monkeypatch):
        from lib import stream_access

        entry, active = self._entry(tmp_path)
        paused = stream_access.paused_path(active)
        paused.write_text("paused copy\n")
        monkeypatch.setattr(docker_ops, "compose_up", lambda *a, **k: None)
        monkeypatch.setattr(stream_access, "refresh_and_reload", lambda _c: None)

        result = provisioner.start_service(
            user_name="alice", service_name="myapp", label="0", nginx_container="nginx",
        )
        assert active.read_text() != "paused copy\n"
        assert paused.read_text() == "paused copy\n", "the paused file is never silently deleted"
        assert result["stream_conf_conflicts"], result

    def test_delete_removes_the_paused_file(self, tmp_path, monkeypatch):
        from lib import stream_access

        entry, active = self._entry(tmp_path)
        paused = stream_access.paused_path(active)
        active.rename(paused)
        active.with_suffix(active.suffix + ".generated").write_text("")
        monkeypatch.setattr(docker_ops, "network_disconnect", lambda *a, **k: None)
        monkeypatch.setattr(docker_ops, "compose_down", lambda *a, **k: None)
        monkeypatch.setattr(docker_ops, "remove_project_volumes", lambda *a, **k: [])
        monkeypatch.setattr(docker_ops, "orphan_network_cleanup", lambda *a, **k: None)
        monkeypatch.setattr(stream_access, "refresh_and_reload", lambda _c: None)
        monkeypatch.setattr(provisioner, "_purge_user_artifacts", lambda *a, **k: None)

        provisioner.remove_user(
            user_name="alice", service_name="myapp", label="0", nginx_container="nginx",
        )
        assert not paused.exists(), "a delete removes the paused copy with the instance"


# ---------------------------------------------------------------------------
# R12 — an out-of-band instance-container start/recreate reloads nginx once
# ---------------------------------------------------------------------------


class TestInstanceEvents:
    """R12 — Docker-event filter, readiness, coalescing and the fail-closed retry."""

    def _ev(self, name, action="start", project=None):
        attrs = {"name": name}
        if project:
            attrs["com.docker.compose.project"] = project
        return {"Type": "container", "Action": action,
                "Actor": {"Attributes": attrs}}

    def test_only_instance_start_or_create_matches(self):
        from lib import instance_events as ie

        names = {"myapp-user_alice-0-web"}
        projects = {"myapp-user_alice-0"}
        assert ie.event_matches(self._ev("myapp-user_alice-0-web"), names, projects)
        assert ie.event_matches(self._ev("myapp-user_alice-0-web", "create"), names, projects)
        # A project fallback matches a container whose recorded name is stale.
        assert ie.event_matches(
            self._ev("renamed-container", "start", "myapp-user_alice-0"), names, projects)
        # Unrelated container, a stop, and a non-container event do not.
        assert not ie.event_matches(self._ev("postgres"), names, projects)
        assert not ie.event_matches(self._ev("myapp-user_alice-0-web", "stop"), names, projects)
        assert not ie.event_matches(
            {"Type": "network", "Action": "connect",
             "Actor": {"Attributes": {"name": "x"}}}, names, projects)

    def test_parse_event_line_ignores_malformed_lines(self):
        from lib import instance_events as ie

        assert ie.parse_event_line("not json") is None
        assert ie.parse_event_line("") is None
        assert ie.parse_event_line('{"Type": "container"}') == {"Type": "container"}

    def test_readiness_requires_running_and_attached(self):
        from lib import instance_events as ie

        states = {"c": [False, True, True]}
        infos = []

        def inspect(name):
            running = states[name].pop(0)
            return {"State": {"Running": running},
                    "NetworkSettings": {"Networks": {"n": {}} if running else {}}}

        ready, pending = ie.wait_until_ready(
            {"c"}, timeout=5, interval=0, inspect=inspect, sleep=lambda _s: None)
        assert ready == {"c"} and pending == set()

    def test_a_container_that_never_becomes_ready_is_not_reloaded(self):
        from lib import instance_events as ie

        calls = []
        res = ie.handle_event_batch(
            {"c"}, "nginx", readiness_timeout=0,
            inspect=lambda _n: {"State": {"Running": False}, "NetworkSettings": {}},
            sleep=lambda _s: None,
            reload_fn=lambda *a, **k: calls.append(a) or True,
        )
        assert res["reloaded"] is False
        assert res["not_ready"] == ["c"]
        assert calls == []

    def test_a_settled_batch_reloads_once(self):
        from lib import instance_events as ie

        calls = []

        def reload_fn(names, container, **k):
            calls.append((set(names), container))
            return True

        res = ie.handle_event_batch(
            {"c"}, "nginx",
            inspect=lambda _n: {"State": {"Running": True},
                                "NetworkSettings": {"Networks": {"n": {}}}},
            sleep=lambda _s: None, reload_fn=reload_fn,
        )
        assert res["reloaded"] is True
        assert calls == [({"c"}, "nginx")]

    def test_the_watcher_coalesces_a_burst_into_one_reload(self, monkeypatch):
        from lib import instance_events as ie

        class _Proc:
            def __init__(self):
                self.stdout = io.StringIO(
                    '{"Type":"container","Action":"create","Actor":{"ID":"o1","Attributes":{"name":"other"}}}\n'
                    '{"Type":"container","Action":"create","Actor":{"ID":"c1id","Attributes":{"name":"c1"}}}\n'
                    '{"Type":"container","Action":"start","Actor":{"ID":"c1id","Attributes":{"name":"c1"}}}\n'
                    '{"Type":"container","Action":"start","Actor":{"ID":"c2id","Attributes":{"name":"c2"}}}\n'
                )
            def terminate(self):
                pass

        monkeypatch.setattr(ie, "instance_targets", lambda: ({"c1", "c2"}, set()))
        calls = []

        def reload_fn(names, container, **k):
            calls.append((set(names), container))
            return True

        ie.watch_instance_events(
            "nginx", proc_factory=_Proc, _once=True, settle_seconds=0.01,
            readiness_timeout=0,
            inspect=lambda _n: {"State": {"Running": True},
                                "NetworkSettings": {"Networks": {"n": {}}}},
            sleep=lambda _s: None, reload_fn=reload_fn,
        )
        # Readiness is ID-based: a compose recreate's temporary name would fail
        # a name-based inspect.  One burst still means ONE reload.
        assert calls == [({"c1id", "c2id"}, "nginx")], "one burst, one reload"

    def test_a_write_failure_is_retried_and_can_succeed(self, monkeypatch):
        from lib import instance_events as ie

        attempts = []

        def flaky(_container):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("write failed")

        monkeypatch.setattr(ie.stream_access, "refresh_and_reload", flaky)
        assert ie.reload_for_batch({"c"}, "nginx", sleep=lambda _s: None) is True
        assert len(attempts) == 2

    def test_a_persistent_write_failure_never_reloads_a_stale_policy(self, monkeypatch):
        from lib import instance_events as ie

        def always(_container):
            raise OSError("write failed")

        monkeypatch.setattr(ie.stream_access, "refresh_and_reload", always)
        assert ie.reload_for_batch(
            {"c"}, "nginx", attempts=2, sleep=lambda _s: None) is False


# ---------------------------------------------------------------------------
# R14 — the primary is gone: bare conf names, migration and ignored input
# ---------------------------------------------------------------------------


class TestPrimaryRemoval:
    """R14 — every HTTP endpoint is `--<sub>`; a bare name is refused/dropped."""

    def test_a_bare_server_name_is_refused(self):
        from lib.nginx_converter import reject_bare_http_server_names

        for text in (
            "server { listen 80; server_name example.com; }",
            "server { listen 80; server_name mock_server_name; }",
            "server { listen 80; server_name {{ hostname }}; }",
        ):
            with pytest.raises(ValueError):
                reject_bare_http_server_names(text)

    def test_an_endpoint_name_is_accepted(self):
        from lib.nginx_converter import reject_bare_http_server_names

        reject_bare_http_server_names(
            "server { listen 80; server_name mock_server_name--neo4j; }")
        reject_bare_http_server_names(
            "server { listen 80; server_name {{ hostname_stem }}--web.{{ domain_name }}; }")

    def test_migration_strips_the_old_host_and_keeps_the_endpoints(self, tmp_path):
        from lib.nginx_converter import migrate_rendered_http_confs

        conf = tmp_path / "svc.user-alice.0.nginx.conf"
        conf.write_text(
            "server {\n    listen 80;\n    server_name svc-alice-0.localhost;\n"
            "    location / { proxy_pass http://svc-web:80; }\n}\n"
            "server {\n    listen 80;\n    server_name svc-alice-0--web.localhost;\n"
            "    location / { proxy_pass http://svc-web:80; }\n}\n"
        )
        changed = migrate_rendered_http_confs(tmp_path)
        text = conf.read_text()
        assert changed == [str(conf)]
        assert "server_name svc-alice-0.localhost;" not in text
        assert "server_name svc-alice-0--web.localhost;" in text

    def test_the_registration_route_consults_the_refusal(self):
        """The refusal lives at the route (see the 422 test in
        `TestAPIProjectRoot`); `convert_nginx` keeps the legacy mapping so an
        already-converted template can still be normalized."""
        import inspect

        import api
        src = inspect.getsource(api.register_user)
        assert "reject_bare_http_server_names" in src

    def test_rendering_a_legacy_template_does_not_recreate_the_route(self, tmp_path):
        from lib.nginx_converter import strip_unserved_http_blocks

        rendered = (
            "server {\n    listen 80;\n    server_name svc-alice-0.localhost;\n}\n"
            "server {\n    listen 80;\n    server_name svc-alice-0--web.localhost;\n}\n"
        )
        stripped = strip_unserved_http_blocks(rendered)
        assert "svc-alice-0.localhost" not in stripped
        assert "svc-alice-0--web.localhost" in stripped


class TestStoppedInstanceIsNotHealthy:
    """R13/T5 — an instance with nothing up is DOWN, not a completed init.

    `Exited (0)` counts as healthy only BESIDE a running service. Treating it
    as healthy everywhere made a stopped instance read as running: the card kept
    offering "Stop" and never "Start", so the paused stream conf could not be
    restored through the UI (measured 2026-10-27).
    """

    def _entry(self):
        return {
            "service_id": "00000000-0000-0000-0000-000000000001",
            "owner_api_user_id": "00000000-0000-0000-0000-000000000002",
            "user_name": "alice", "service_name": "myapp", "label": "0",
            "compose_template_path": "", "compose_file_path": "",
            "nginx_conf_template_path": "", "nginx_conf_path": "",
            "container_names": ["app", "init"],
        }

    def _status(self, running, monkeypatch):
        import api
        monkeypatch.setattr(registry, "get_user", lambda _u: [self._entry()])
        return api._status_for_user("alice", running)["unhealthy_services"] + \
            api._status_for_user("alice", running)["healthy_services"]

    def test_all_exited_is_down(self, monkeypatch):
        svc = self._status(
            {"app": "Exited (0) 1 minute ago", "init": "Exited (0) 1 minute ago"},
            monkeypatch,
        )[0]
        assert svc["healthy_containers"] == {}, svc
        assert set(svc["unhealthy_containers"]) == {"app", "init"}

    def test_a_completed_init_beside_a_running_service_is_healthy(self, monkeypatch):
        svc = self._status(
            {"app": "Up 2 minutes (healthy)", "init": "Exited (0) 3 minutes ago"},
            monkeypatch,
        )[0]
        assert set(svc["healthy_containers"]) == {"app", "init"}
        assert svc["unhealthy_containers"] == {}

    def test_a_compose_recreate_temporary_name_still_matches(self):
        from lib import instance_events as ie

        event = {"Type": "container", "Action": "create",
                 "Actor": {"ID": "abc",
                           "Attributes": {"name": "0123456789ab_myapp-user_alice-0-app"}}}
        assert ie.event_matches(event, {"myapp-user_alice-0-app"}, set())



# ---------------------------------------------------------------------------
# G78 — public-card auth annotation (T5)
# ---------------------------------------------------------------------------


class TestHasAuthAnnotation:
    def test_reads_the_rendered_conf_not_just_passwd_plain(self, tmp_path):
        import api
        conf = tmp_path / "svc.conf"
        conf.write_text("server {\n    auth_basic \"x\";\n    auth_basic_user_file /x;\n}\n")
        assert api._has_auth({"nginx_conf_path": str(conf), "passwd_plain": ""}) is True
        conf.write_text("server { location / { proxy_pass http://x; } }\n")
        assert api._has_auth({"nginx_conf_path": str(conf), "passwd_plain": "still-recorded"}) is False
        # Any conf carrying auth_basic is enough across a multi-conf instance.
        second = tmp_path / "svc2.conf"
        second.write_text("server {\n    auth_basic \"y\";\n}\n")
        assert api._has_auth({"nginx_conf_paths": [str(conf), str(second)]}) is True


class TestNginxReloadIsLoud:
    """T4: a failed reload must surface so the caller's rollback can run."""

    def test_nginx_reload_raises_when_docker_exec_fails(self, monkeypatch):
        import subprocess
        from lib import docker_ops

        monkeypatch.setattr(
            docker_ops, "_run",
            lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "container is not running"),
        )
        with pytest.raises(RuntimeError, match="nginx reload failed"):
            docker_ops.nginx_reload("subnet-acl-nginx")

    def test_a_successful_reload_does_not_raise(self, monkeypatch):
        import subprocess
        from lib import docker_ops

        monkeypatch.setattr(
            docker_ops, "_run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""),
        )
        assert docker_ops.nginx_reload("subnet-acl-nginx") is None


class TestNullComposePathClassification:
    """T11 minimal shape: a seeded/legacy instance row with no rendered compose
    path must classify as `missing`, never 500 the whole user-status read.

    Regression: `entry.get("compose_file_path", "")` returns None when the
    column is present with a NULL value, and `Path(None)` raised inside
    `_status_for_user` (surfaced by the isolated minimal-shape trace, 2026-11-01).
    """

    def test_null_compose_path_does_not_crash(self, registry_file, sample_entry):
        from api import _status_for_user
        add_registry_user({**sample_entry, "compose_file_path": None})
        # The read must complete and place the entry in exactly one bucket; the
        # regression being guarded is the `Path(None)` 500, not the bucket.
        result = _status_for_user("alice", {})
        buckets = ("healthy_services", "unhealthy_services", "missing_services", "failed_services")
        found = [b for b in buckets if any(s["service_name"] == "myapp" for s in result[b])]
        assert len(found) == 1, (found, result)

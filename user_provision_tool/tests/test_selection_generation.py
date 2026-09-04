"""Tests for the file-selection & generation design implementation (GAP-3/5/11/14/15/17).

Covers: compose multi-file merge (golden), converter env_file tokenization,
render_compose per-user env-file copy + missing-stay-missing, copy-if-empty
bind bootstrap, repeatable --profile/--env-file command construction, and
registry recording of profiles/env_files/service_env_files.
"""

import shutil
import time
from pathlib import Path

import pytest
import yaml


def _retry_transient(fn, attempts=3, delay=1.0, backoff=2.0):
    """Run *fn* up to *attempts* times, tolerating transient failures.

    Tests that exercise real external processes (``docker compose config``)
    can hit a one-off CLI/daemon hiccup that resolves immediately; a single
    attempt would then fail the suite without any real regression (observed:
    test_merge_real_docker_golden failed 1/7 full-suite runs at
    2026-09-01T19:04Z on an otherwise idle daemon).  Retrying with backoff
    keeps the suite deterministic while still failing loudly — with the
    last error — once the attempts are exhausted (e.g. docker genuinely
    unavailable).
    """
    last_exc: Exception | None = None
    wait = delay
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - transient external failures
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(wait)
                wait *= backoff
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# GAP-3: multi-file merge
# ---------------------------------------------------------------------------

DIFY_BASE = """\
name: dify
services:
  api:
    image: langgenius/dify-api:1.0.0
    environment:
      MODE: api
      SECRET_KEY: ${SECRET_KEY}
  web:
    image: langgenius/dify-web:1.0.0
    profiles: [""]
x-common: &common
  restart: always
"""

DIFY_MIDDLEWARE = """\
services:
  db_postgres:
    image: postgres:15-alpine
    profiles: ["", "postgresql"]
    environment:
      POSTGRES_PASSWORD: ${DB_PASSWORD}
  redis:
    image: redis:7-alpine
x-custom: foo
"""

MERGED_EXPECTED = """\
services:
  api:
    image: langgenius/dify-api:1.0.0
  db_postgres:
    image: postgres:15-alpine
    profiles: ["", "postgresql"]
  redis:
    image: redis:7-alpine
  web:
    image: langgenius/dify-web:1.0.0
    profiles: [""]
"""


class TestComposeMerge:
    def test_merge_requires_two_files(self, tmp_path):
        from lib.compose_merge import merge_compose_files
        with pytest.raises(ValueError):
            merge_compose_files([str(tmp_path / "a.yml")], str(tmp_path / "m.yml"))

    def test_merge_strips_x_blocks_and_preserves_profiles(self, monkeypatch, tmp_path):
        """GAP-3 golden: merged output keeps per-service profiles verbatim and
        strips top-level x-* extension blocks."""
        from lib import docker_ops
        from lib.compose_merge import merge_compose_files

        a = tmp_path / "docker-compose.yaml"
        b = tmp_path / "docker-compose.middleware.yaml"
        a.write_text(DIFY_BASE)
        b.write_text(DIFY_MIDDLEWARE)

        captured: dict = {}

        def fake_run(args, check=True):
            captured["args"] = list(args)
            return docker_ops.subprocess.CompletedProcess(
                args, 0, stdout=MERGED_EXPECTED, stderr=""
            )

        monkeypatch.setattr(docker_ops, "_run", fake_run)
        out = tmp_path / "docker-compose.merged.yml"
        result = merge_compose_files([str(a), str(b)], str(out))

        assert result["merged"] is True
        # Both -f flags and both mandatory config flags present
        assert captured["args"][:6] == [
            "docker", "compose", "-f", str(a), "-f", str(b),
        ]
        assert "--no-interpolate" in captured["args"]
        assert "--no-path-resolution" in captured["args"]

        data = yaml.safe_load(out.read_text())
        assert "x-common" not in data and "x-custom" not in data
        # Profiles pass through verbatim (GAP-4 semantics preserved through merge)
        assert data["services"]["db_postgres"]["profiles"] == ["", "postgresql"]
        assert data["services"]["web"]["profiles"] == [""]

    def test_merge_failure_raises(self, monkeypatch, tmp_path):
        from lib import docker_ops
        from lib.compose_merge import merge_compose_files

        a = tmp_path / "a.yml"
        b = tmp_path / "b.yml"
        a.write_text("services: {}\n")
        b.write_text("services: {}\n")

        def fake_run(args, check=True):
            return docker_ops.subprocess.CompletedProcess(
                args, 1, stdout="", stderr="error: invalid compose"
            )

        monkeypatch.setattr(docker_ops, "_run", fake_run)
        with pytest.raises(RuntimeError, match="config failed"):
            merge_compose_files([str(a), str(b)], str(tmp_path / "m.yml"))

    @pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")
    def test_merge_real_docker_golden(self, tmp_path):
        """Real `docker compose config` golden: dify two-file merge keeps
        profile-gated db_postgres and strips nothing critical.

        The merge shells out to real ``docker compose config``; a transient
        docker CLI/daemon hiccup can fail a single attempt even though the
        daemon recovers immediately (observed 1/7 full-suite flake at
        2026-09-01T19:04Z on an idle daemon), so the merge is retried with
        backoff.  The golden semantics are unchanged — the test still fails
        with the real docker error if the merge genuinely cannot succeed.
        """
        from lib.compose_merge import merge_compose_files

        a = tmp_path / "docker-compose.yaml"
        b = tmp_path / "docker-compose.middleware.yaml"
        a.write_text(DIFY_BASE)
        b.write_text(DIFY_MIDDLEWARE)
        out = tmp_path / "docker-compose.merged.yml"
        _retry_transient(
            lambda: merge_compose_files([str(a), str(b)], str(out)),
            attempts=3,
        )
        data = yaml.safe_load(out.read_text())
        names = sorted(data["services"].keys())
        assert "db_postgres" in names  # profile-gated service SURVIVES the merge
        assert data["services"]["db_postgres"]["profiles"] == ["", "postgresql"]

    def test_merge_real_docker_retry_tolerates_transient_failure(self):
        """The retry wrapper recovers from transient failures (no real
        docker involved — exercises _retry_transient itself)."""
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("transient docker daemon hiccup")
            return "ok"

        assert _retry_transient(flaky, attempts=3, delay=0) == "ok"
        assert calls["n"] == 3

    def test_merge_real_docker_retry_raises_last_error_when_exhausted(self):
        """Once attempts run out the last error is re-raised (genuine
        breakage still fails the suite)."""
        def always_fail():
            raise RuntimeError("docker down")

        with pytest.raises(RuntimeError, match="docker down"):
            _retry_transient(always_fail, attempts=2, delay=0)


# ---------------------------------------------------------------------------
# GAP-14: converter env_file tokenization + render copy
# ---------------------------------------------------------------------------

class TestEnvFileTokenization:
    def _compose_with_env(self, env_value):
        return {
            "services": {
                "web": {
                    "image": "nginx:alpine",
                    "env_file": env_value,
                    "networks": ["mynet"],
                }
            },
            "networks": {"mynet": None},
        }

    def test_string_form_tokenized(self):
        from lib.compose_converter import convert
        transformed, _, tokens, env_to_key = convert(
            self._compose_with_env("./config/app.env")
        )
        assert "./config/app.env" in env_to_key
        assert "{{ env_files['" in tokens.detokenize(
            yaml.dump(transformed, sort_keys=False)
        )

    def test_long_form_path_substituted_only(self):
        from lib.compose_converter import convert
        transformed, _, tokens, env_to_key = convert(
            self._compose_with_env({"path": "./secrets/db.env", "required": True})
        )
        assert "./secrets/db.env" in env_to_key
        text = tokens.detokenize(yaml.dump(transformed, sort_keys=False))
        assert "required: true" in text  # long-form shape preserved
        assert "{{ env_files['" in text

    def test_list_form_all_tokenized(self):
        from lib.compose_converter import convert
        transformed, _, tokens, env_to_key = convert(
            self._compose_with_env([".env", "./extra.env"])
        )
        assert set(env_to_key) == {".env", "./extra.env"}

    def test_header_comments_written(self, tmp_path):
        from lib.compose_converter import compose_file_to_template
        src = tmp_path / "dc.yml"
        src.write_text(
            "services:\n  web:\n    image: nginx:alpine\n    env_file: ./config/app.env\n"
        )
        out = tmp_path / "dc.yml.j2"
        compose_file_to_template(str(src), str(out), "myapp")
        content = out.read_text()
        assert "env_files['" in content
        assert "← original path: ./config/app.env" in content


class TestRenderComposeServiceEnvFiles:
    def _template(self, tmp_path, body):
        tpl = tmp_path / "dc.yml.j2"
        tpl.write_text(
            "# myapp.yml.j2 — generated\n"
            "#     env_files['app_env']  ← original path: ./config/app.env\n"
            "#     env_files['missing_env']  ← original path: ./config/missing.env\n"
            "services:\n"
            "  web:\n"
            "    image: nginx:alpine\n"
            "    container_name: {{ container_prefix }}web\n"
            f"    env_file: {body}\n"
            "    networks:\n"
            "      - {{ network_name }}\n"
            "networks:\n"
            "  {{ network_name }}:\n"
            "    name: {{ network_name }}\n"
        )
        return tpl

    def test_existing_declared_file_copied_per_user(self, tmp_path):
        from lib.template_engine import render_compose

        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "app.env").write_text("FOO=bar\n")

        tpl = self._template(tmp_path, "{{ env_files['app_env'] }}")
        out = tmp_path / "dc.user-alice.0.yml"
        render_compose(
            str(tpl), str(out), "alice", "myapp", "0", volumes={},
        )
        content = out.read_text()
        assert "env_file: app.env.alice.0" in content
        assert (tmp_path / "app.env.alice.0").exists()
        assert (tmp_path / "app.env.alice.0").read_text() == "FOO=bar\n"

    def test_missing_declared_file_stays_missing(self, tmp_path):
        """Missing files stay missing — the token resolves to the declared path."""
        from lib.template_engine import render_compose

        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "app.env").write_text("FOO=bar\n")

        tpl = self._template(tmp_path, "{{ env_files['missing_env'] }}")
        out = tmp_path / "dc.user-bob.0.yml"
        render_compose(
            str(tpl), str(out), "bob", "myapp", "0", volumes={},
        )
        content = out.read_text()
        assert "env_file: ./config/missing.env" in content

    def test_operator_override_used_as_is(self, tmp_path):
        from lib.template_engine import render_compose

        override = tmp_path / "override.env"
        override.write_text("OPERATOR=1\n")
        tpl = self._template(tmp_path, "{{ env_files['app_env'] }}")
        out = tmp_path / "dc.user-carl.0.yml"
        render_compose(
            str(tpl), str(out), "carl", "myapp", "0", volumes={},
            env_files={"app_env": str(override)},
        )
        assert "env_file: " + str(override) in out.read_text()


# ---------------------------------------------------------------------------
# GAP-15: copy-if-empty bind bootstrap
# ---------------------------------------------------------------------------

class TestCopyIfEmpty:
    def _template_with_binds(self, tmp_path, comment_src, body_src):
        tpl = tmp_path / "dc.yml.j2"
        tpl.write_text(
            "# myapp.yml.j2 — generated\n"
            f"#     volumes['{comment_src[0]}']  ← original path: {comment_src[1]}\n"
            "services:\n"
            "  web:\n"
            "    image: nginx:alpine\n"
            f"    volumes: [\"{{{{ volumes['{body_src[0]}'] }}}}:/etc/nginx:ro\"]\n"
        )
        return tpl

    def test_dir_src_copied_when_target_empty(self, tmp_path):
        from lib.provisioner import _auto_volumes

        nginx_dir = tmp_path / "nginx"
        nginx_dir.mkdir()
        (nginx_dir / "conf.d").mkdir()
        (nginx_dir / "conf.d" / "default.conf").write_text("server {}")
        (nginx_dir / "docker-entrypoint.sh").write_text("#!/bin/sh\n")

        tpl = self._template_with_binds(tmp_path, ("nginx_conf", "./nginx"), ("nginx_conf", "./nginx"))
        user_data = tmp_path / "user_data"
        result = _auto_volumes(str(tpl), "alice", "dify", "0", user_data)

        target = Path(result["nginx_conf"])
        assert (target / "conf.d" / "default.conf").read_text() == "server {}"
        assert (target / "docker-entrypoint.sh").read_text() == "#!/bin/sh\n"

    def test_non_empty_target_never_clobbered(self, tmp_path):
        from lib.provisioner import _auto_volumes

        nginx_dir = tmp_path / "nginx"
        nginx_dir.mkdir()
        (nginx_dir / "ship.conf").write_text("# shipped\n")
        tpl = self._template_with_binds(tmp_path, ("nginx_conf", "./nginx"), ("nginx_conf", "./nginx"))
        user_data = tmp_path / "user_data"

        # Pre-existing per-user dir with runtime data
        runtime = user_data / "alice" / "dify" / "0" / "nginx_conf"
        runtime.mkdir(parents=True)
        (runtime / "runtime.conf").write_text("# runtime\n")

        result = _auto_volumes(str(tpl), "alice", "dify", "0", user_data)
        target = Path(result["nginx_conf"])
        assert (target / "runtime.conf").exists()
        assert not (target / "ship.conf").exists()  # shipped content NOT copied

    def test_missing_src_creates_empty_dir(self, tmp_path):
        from lib.provisioner import _auto_volumes

        tpl = self._template_with_binds(tmp_path, ("data", "./volumes/data"), ("data", "./volumes/data"))
        result = _auto_volumes(str(tpl), "alice", "dify", "0", tmp_path / "user_data")
        assert Path(result["data"]).is_dir()
        assert list(Path(result["data"]).iterdir()) == []

    def test_file_src_becomes_file_target(self, tmp_path):
        from lib.provisioner import _auto_volumes

        entrypoint = tmp_path / "docker-entrypoint.sh"
        entrypoint.write_text("#!/bin/sh\nnginx\n")
        tpl = self._template_with_binds(
            tmp_path, ("entrypoint", "./docker-entrypoint.sh"),
            ("entrypoint", "./docker-entrypoint.sh"),
        )
        result = _auto_volumes(str(tpl), "bob", "dify", "1", tmp_path / "user_data")
        target = Path(result["entrypoint"])
        assert target.is_file()  # file→file bind target is a FILE
        assert target.read_text() == "#!/bin/sh\nnginx\n"


# ---------------------------------------------------------------------------
# GAP-5/GAP-11: docker_ops repeatable --profile and --env-file
# ---------------------------------------------------------------------------

class TestDockerOpsSelectionFlags:
    def test_profiles_passed_repeatably(self, monkeypatch):
        from lib import docker_ops

        captured: dict = {}

        def fake_run(args, check=True):
            captured["args"] = args
            return docker_ops.subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(docker_ops, "_run", fake_run)
        docker_ops.compose_up("c.yml", project_name="p", profiles=["a", "b"])
        args = captured["args"]
        assert args.count("--profile") == 2
        assert args[args.index("--profile") + 1] == "a"
        assert args[args.index("--profile", args.index("--profile") + 1) + 1] == "b"

    def test_no_profiles_no_flag(self, monkeypatch):
        from lib import docker_ops

        captured: dict = {}
        monkeypatch.setattr(
            docker_ops, "_run",
            lambda args, check=True: (captured.update(args=args)
                                      or docker_ops.subprocess.CompletedProcess(args, 0, "", "")),
        )
        docker_ops.compose_up("c.yml")
        assert "--profile" not in captured["args"]

    def test_env_files_order_preserved(self, monkeypatch):
        from lib import docker_ops

        captured: dict = {}
        monkeypatch.setattr(
            docker_ops, "_run",
            lambda args, check=True: (captured.update(args=args)
                                      or docker_ops.subprocess.CompletedProcess(args, 0, "", "")),
        )
        docker_ops.compose_up("c.yml", env_file=["a.env", "b.env"])
        idx = [i for i, a in enumerate(captured["args"]) if a == "--env-file"]
        assert [captured["args"][i + 1] for i in idx] == ["a.env", "b.env"]

    def test_build_passes_profiles_and_env(self, monkeypatch):
        from lib import docker_ops

        captured: dict = {}
        monkeypatch.setattr(
            docker_ops, "_run",
            lambda args, check=True: (captured.update(args=args)
                                      or docker_ops.subprocess.CompletedProcess(args, 0, "", "")),
        )
        docker_ops.compose_build("c.yml", env_file=["e.env"], profiles=["pg"])
        assert "--profile" in captured["args"] and "--env-file" in captured["args"]
        assert "build" in captured["args"]


# ---------------------------------------------------------------------------
# GAP-17: registry records selection settings
# ---------------------------------------------------------------------------

class TestRegistryRecordsSelection:
    def _convert_template(self, tmp_path, src_text, name="dc.yml"):
        from lib.compose_converter import compose_file_to_template
        src = tmp_path / name
        src.write_text(src_text)
        out = tmp_path / f"{name}.j2"
        compose_file_to_template(str(src), str(out), "myapp")
        return str(out)

    def test_entry_records_profiles_env_files_service_env(self, tmp_path, monkeypatch, registry_file):
        from lib import docker_ops, provisioner, registry

        # Mock docker ops so no real docker is needed
        monkeypatch.setattr(docker_ops, "compose_build", lambda *a, **k: None)
        monkeypatch.setattr(docker_ops, "compose_up", lambda *a, **k: None)
        monkeypatch.setattr(docker_ops, "network_connect", lambda *a, **k: None)
        monkeypatch.setattr(docker_ops, "nginx_reload", lambda *a, **k: None)
        monkeypatch.setattr(docker_ops, "compose_down", lambda *a, **k: None)
        from lib import subnet_manager as _sm
        monkeypatch.setattr(_sm, "SUBNET_POOLS", False)

        env_src = tmp_path / "interp.env"
        env_src.write_text("A=1\n")
        tpl = self._convert_template(
            tmp_path,
            "services:\n"
            "  web:\n    image: nginx:alpine\n    profiles: [\"a\", \"\"]\n"
            "    env_file: ./svc.env\n",
        )
        (tmp_path / "svc.env").write_text("SVC=1\n")

        provisioner.register_user(
            user_name="alice",
            service_name="myapp",
            label="0",
            compose_template=tpl,
            output_dir=tmp_path,
            user_data_dir=tmp_path / "user_data",
            env_files=[str(env_src)],
            profiles=["a"],
            compose_sources=["docker-compose.yml"],
        )

        entry = registry.get_user_service("alice", "myapp", "0")
        assert entry["profiles"] == ["a"]
        assert entry["env_files"], "per-user env file must be recorded"
        assert entry["env_files"][0].endswith(".env.alice.0")
        assert entry["compose_sources"] == ["docker-compose.yml"]
        # service_env_files records declared paths that exist in the recipe
        assert entry["service_env_files"] == ["./svc.env"]

    def test_rebuild_reenjoys_recorded_profiles_and_env(self, tmp_path, monkeypatch, registry_file):
        """Rebuild re-applies the recorded profiles + env_files order."""
        from lib import docker_ops, provisioner, registry

        calls: list[list[str]] = []
        original_build = docker_ops.compose_build
        original_up = docker_ops.compose_up
        monkeypatch.setattr(
            docker_ops, "compose_build",
            lambda *a, **k: calls.append(("build", k)),
        )
        monkeypatch.setattr(
            docker_ops, "compose_up",
            lambda *a, **k: calls.append(("up", k)),
        )
        monkeypatch.setattr(docker_ops, "network_connect", lambda *a, **k: None)
        monkeypatch.setattr(docker_ops, "nginx_reload", lambda *a, **k: None)
        from lib import subnet_manager as _sm
        monkeypatch.setattr(_sm, "SUBNET_POOLS", False)

        tpl = self._convert_template(
            tmp_path, "services:\n  web:\n    image: nginx:alpine\n    profiles: [\"pg\"]\n"
        )
        env_src = tmp_path / "interp.env"
        env_src.write_text("A=1\n")
        provisioner.register_user(
            user_name="bob", service_name="myapp", label="1",
            compose_template=tpl, output_dir=tmp_path,
            user_data_dir=tmp_path / "user_data",
            env_files=[str(env_src)], profiles=["pg"],
        )
        calls.clear()

        provisioner.rebuild_user(user_name="bob", service_name="myapp", label="1")
        up_kwargs = [k for name, k in calls if name == "up"][0]
        assert up_kwargs.get("profiles") == ["pg"]
        assert up_kwargs.get("env_file"), "rebuild must pass the per-user env file"


# ---------------------------------------------------------------------------
# GAP-24: lightweight convert/preview — volume keys from converter src→key
# mapping (design §Implementation notes L284-286), not frontend .j2 parsing.
# ---------------------------------------------------------------------------

PREVIEW_COMPOSE = """\
name: demo
services:
  web:
    image: nginx:alpine
    volumes:
      - ./app_data:/usr/share/nginx/html
      - ./db_data:/var/lib/postgresql
    ports:
      - "8080:80"
  worker:
    image: busybox
    volumes:
      - ./app_data:/srv/data
"""


class TestComposePreview:
    """GET /services/{name}/compose/preview — converter in-call src→key map."""

    def _write_project(self, tmp_path, name="demo", content=PREVIEW_COMPOSE, sub=""):
        proj = tmp_path / name
        if sub:
            proj = proj / sub
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "docker-compose.yml").write_text(content)
        return proj

    def test_single_file_returns_src_to_key_and_volume_keys(self, tmp_path):
        from api import _preview_volume_mapping

        proj = self._write_project(tmp_path)
        result = _preview_volume_mapping(proj, ["docker-compose.yml"])

        assert result["compose_files"] == ["docker-compose.yml"]
        # Converter in-call mapping: bind-mount source path → template key.
        assert result["src_to_key"] == {
            "./app_data": "app_data",
            "./db_data": "db_data",
        }
        # Order = first appearance across services (app_data before db_data).
        assert result["volume_keys"] == ["app_data", "db_data"]
        # Pure preview: NO template or .generated artifacts are written.
        assert not (proj / "docker-compose.yml.j2").exists()
        assert not (proj / "docker-compose.yml.j2.generated").exists()

    def test_multi_file_merges_in_memory_without_artifact(self, monkeypatch, tmp_path):
        """≥2 files: ordered merge (same flags as deploy) then convert — no file written."""
        from api import _preview_volume_mapping
        from lib import docker_ops

        proj = tmp_path / "demo"
        proj.mkdir()
        (proj / "docker-compose.yml").write_text(
            "services:\n  api:\n    image: langgenius/dify-api:1.0.0\n    volumes:\n      - ./logs:/app/logs\n"
        )
        (proj / "docker-compose.middleware.yml").write_text(
            "services:\n  db:\n    image: postgres:15-alpine\n    volumes:\n      - ./db_data:/var/lib/postgresql\n"
        )
        merged_stdout = (
            "services:\n"
            "  api:\n    image: langgenius/dify-api:1.0.0\n    volumes:\n      - ./logs:/app/logs\n"
            "  db:\n    image: postgres:15-alpine\n    volumes:\n      - ./db_data:/var/lib/postgresql\n"
        )
        captured: dict = {}

        def fake_run(args, check=True):
            captured["args"] = list(args)
            return docker_ops.subprocess.CompletedProcess(
                args, 0, stdout=merged_stdout, stderr=""
            )

        monkeypatch.setattr(docker_ops, "_run", fake_run)
        result = _preview_volume_mapping(
            proj, ["docker-compose.yml", "docker-compose.middleware.yml"]
        )

        assert "--no-interpolate" in captured["args"]
        assert "--no-path-resolution" in captured["args"]
        assert result["volume_keys"] == ["logs", "db_data"]
        # No merged artifact left behind.
        assert not (proj / "docker-compose.merged.yml").exists()
        assert not (proj / "docker-compose.merged.yml.j2").exists()

    def test_j2_input_resolves_to_plain_source(self, tmp_path):
        """A .j2 template path is resolved to its plain source sibling first."""
        from api import _preview_volume_mapping

        proj = self._write_project(tmp_path)
        result = _preview_volume_mapping(proj, ["docker-compose.yml.j2"])
        assert result["volume_keys"] == ["app_data", "db_data"]

    def test_j2_without_source_extracts_tokens_server_side(self, tmp_path):
        """Template-only projects fall back to server-side token extraction."""
        from api import _preview_volume_mapping

        proj = tmp_path / "demo"
        proj.mkdir()
        (proj / "docker-compose.yml.j2").write_text(
            "services:\n  web:\n    image: nginx:alpine\n"
            "    volumes:\n      - {{ volumes['app_data'] }}:/usr/share/nginx/html\n"
        )
        result = _preview_volume_mapping(proj, ["docker-compose.yml.j2"])
        assert result["volume_keys"] == ["app_data"]

    def test_missing_file_404(self, tmp_path):
        from api import _preview_volume_mapping
        from fastapi import HTTPException

        proj = self._write_project(tmp_path)
        with pytest.raises(HTTPException) as exc:
            _preview_volume_mapping(proj, ["docker-compose.yml.bak"])
        assert exc.value.status_code == 404

    def test_absolute_path_rejected(self, tmp_path):
        from api import _preview_volume_mapping
        from fastapi import HTTPException

        proj = self._write_project(tmp_path)
        with pytest.raises(HTTPException) as exc:
            _preview_volume_mapping(proj, [str(tmp_path / "demo" / "docker-compose.yml")])
        assert exc.value.status_code == 422

    def test_endpoint_returns_mapping(self, tmp_path, monkeypatch):
        """GET /services/{name}/compose/preview via TestClient."""
        import api
        from fastapi.testclient import TestClient

        sp_dir = tmp_path / "source_projects"
        sp_dir.mkdir()
        monkeypatch.setattr(api, "SOURCE_PROJECTS_DIR", sp_dir)
        self._write_project(sp_dir, name="demo")

        client = TestClient(api.app)
        resp = client.get(
            "/services/demo/compose/preview",
            params={"compose_files": ["docker-compose.yml"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["src_to_key"] == {"./app_data": "app_data", "./db_data": "db_data"}
        assert data["volume_keys"] == ["app_data", "db_data"]

    def test_endpoint_unknown_service_404(self, tmp_path, monkeypatch):
        import api
        from fastapi.testclient import TestClient

        sp_dir = tmp_path / "source_projects"
        sp_dir.mkdir()
        monkeypatch.setattr(api, "SOURCE_PROJECTS_DIR", sp_dir)

        client = TestClient(api.app)
        resp = client.get(
            "/services/nope/compose/preview",
            params={"compose_files": ["docker-compose.yml"]},
        )
        assert resp.status_code == 404

    def test_endpoint_recipe_scoped(self, tmp_path, monkeypatch):
        """recipe_path resolves inside the recipe subdirectory."""
        import api
        from fastapi.testclient import TestClient

        sp_dir = tmp_path / "source_projects"
        sp_dir.mkdir()
        monkeypatch.setattr(api, "SOURCE_PROJECTS_DIR", sp_dir)
        self._write_project(sp_dir, name="demo", sub="sub/app")

        client = TestClient(api.app)
        resp = client.get(
            "/services/demo/compose/preview",
            params={"compose_files": ["docker-compose.yml"], "recipe_path": "sub/app"},
        )
        assert resp.status_code == 200
        assert resp.json()["volume_keys"] == ["app_data", "db_data"]

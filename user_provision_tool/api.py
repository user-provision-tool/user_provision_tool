"""FastAPI service for the user provision tool.

Endpoints:
  POST   /users                                           register user + start containers (async → task_id)
  DELETE /users/{user_name}/services/{service_name}/{label}   stop + deregister (async → task_id)
  POST   /users/{user_name}/services/{service_name}/{label}/rebuild  (async → task_id)
  GET    /users                                           status of all users
  GET    /users/{user_name}                               status of one user
  GET    /tasks                                           list all tasks in the pool
  GET    /tasks/{task_id}                                 query async task status / result
  DELETE /tasks/{task_id}                                 cancel a pending or running task

Long-running operations (register, rebuild, remove) now return a ``task_id``
immediately.  Poll ``GET /tasks/{task_id}`` for progress (status, result, error).

For backward compatibility, pass ``?sync=true`` to block until completion.

Environment variables:
  GENERATED_DIR   directory for generated compose/nginx files  (default: ./generated)
  REGISTRY_FILE   path to user_registry.yml                    (default: ./user_registry.yml)
"""

from __future__ import annotations

import re
import threading
import time
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

# Make lib/ importable when the file sits at the project root
sys.path.insert(0, str(Path(__file__).parent))

from lib import db, docker_ops, provisioner, reconciliation, registry, resolv_conf, stream_access, subnet_manager, template_engine, validation
from lib.compose_converter import compose_file_to_template, get_compose_service_names
from lib.nginx_converter import nginx_file_to_template
from lib.task_manager import TaskCapacityError, task_manager

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GENERATED_DIR = Path(
    os.environ.get("GENERATED_DIR", str(Path(__file__).parent / "generated"))
)
GENERATED_DIR.mkdir(parents=True, exist_ok=True)

# User volume data root: auto-created subdirectories are used when no volumes
# are explicitly provided at registration time.
USER_DATA_DIR = Path(
    os.environ.get("USER_DATA_DIR", str(GENERATED_DIR.parent / "user_data"))
)
USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

# Source projects root: operators clone / copy service repos here.
SOURCE_PROJECTS_DIR = Path(
    os.environ.get("SOURCE_PROJECTS_DIR", str(GENERATED_DIR.parent / "source_projects"))
)
SOURCE_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

# SSL certificates root: fullchain.pem and privkey.pem are copied here per domain.
SSL_DIR = Path(
    os.environ.get("SSL_DIR", str(GENERATED_DIR.parent / "ssl"))
)
SSL_DIR.mkdir(parents=True, exist_ok=True)

NGINX_CONTAINER = os.environ.get("NGINX_CONTAINER", "subnet-acl-nginx")

# The registry module reads REGISTRY_FILE from its own env var at import time.
# We additionally sync it here so both the API and the lib use the same path.
import lib.registry as _reg_mod
_reg_path = os.environ.get(
    "REGISTRY_FILE",
    str(Path(__file__).parent / "user_registry.yml"),
)
_reg_mod.REGISTRY_FILE = Path(_reg_path)

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    # T3 — the canonical body identifies the owner in exactly one of two ways:
    # `owner_api_user_id` (gateway deploy; must be live, never created here) or
    # `owner_username` (direct dashboard/CLI/minimal; ensured `mode='owner'` in
    # the services transaction). `user_name` is an optional compatibility alias
    # for `owner_username`. A username is never an email (R5.4).
    owner_api_user_id: str | None = None
    owner_username: str | None = None
    user_name: str | None = None
    service_id: str | None = None
    procedure_id: str | None = None
    recipe_path: str | None = None
    service_name: str
    # Optional project root — when provided, any relative path below is resolved against it
    # (equivalent to -pr in the CLI)
    project_root: str | None = None
    # Exactly one of these must be provided:
    #   compose_template_path  — path to an existing Jinja2 .yml.j2 template
    #   compose_file_path      — path to a plain docker-compose.yml (auto-converted)
    #   compose_file_paths     — ORDERED list of compose files, merged with
    #                            compose-spec semantics (design §Compose multi-file)
    compose_template_path: str | None = None
    compose_file_path: str | None = None
    compose_file_paths: list[str] | None = None
    # Optionally one of:
    nginx_conf_template_path: str | None = None
    nginx_conf_file_path: str | None = None
    # R9.2's second conf slot: the payload carries the instance's confs as a LIST,
    # exactly as the compose slot already does. An http conf and a stream conf
    # travel in one deploy, and the CATEGORY comes from the file name — which is
    # why the stream conf's name is load-bearing (`*.stream.conf` is the pattern
    # the internal nginx includes).
    #
    # G55 (2026-10-23): the LIST/SINGULAR shapes above are LEGACY. The request
    # model is now the per-category map below — one source per category, the
    # category explicit rather than inferred from the input's file name. The
    # legacy fields are still accepted and normalized (a list with two entries of
    # one category is a loud 422, never a silent overwrite).
    nginx_confs: dict[str, str] | None = None
    nginx_conf_templates: dict[str, str] | None = None
    nginx_conf_template_paths: list[str] | None = None
    nginx_conf_file_paths: list[str] | None = None
    # Repeatable interpolation env files (order preserved, later wins).
    env_file_path: str | None = None
    env_files: list[str] | None = None
    # Optional repeatable profiles (default = no flag → default + "" services).
    profiles: list[str] | None = None
    # Optional per-user override of declared service env files (declared path → host path).
    service_env_files: dict[str, str] | None = None
    # Label allocation (GRILLING G3 / task doc §1.4 Part B). The label is a REGISTRY KEY —
    # `(user_name, service_name, label)` identifies an entry and its uniqueness is enforced
    # here, under `registry.transaction()` (provisioner `register_service`). So the -api is
    # the authority: omit it (or send "auto") and the next free label is allocated. A
    # concrete label is still honoured verbatim, which is what the dashboard sends after
    # computing its hint, and what every existing caller (CLI, tests, rebuild) sends.
    label: str | None = None
    domain: str = "localhost"
    passwd: str = "123456"
    volumes: dict[str, str] = {}
    build_args: dict[str, str] | None = None
    # The TLS toggle (task-doc R9.1 / GRILLING G41). Its meaning is "TLS is in
    # play for this deployment": it gates the instance's TLS vhost AND its stream
    # confs — which is why it is no longer called `https` (the stream confs are
    # not https at all).
    #
    # `https` is kept as a DEPRECATED alias for one round: both keys, one value.
    # Supplying both with DIFFERENT values is refused rather than resolved — a
    # caller that says two things is not a caller to guess for.
    tls: bool | None = None
    https: bool | None = None
    fullchain: str | None = None
    privkey: str | None = None

    @field_validator("owner_username", "user_name", "service_name")
    @classmethod
    def _validate_name(cls, v: str | None, info) -> str | None:
        if v is None:
            return None
        try:
            validation.validate_name(v, info.field_name)
        except validation.ValidationError as e:
            raise ValueError(str(e))
        return v

    @field_validator("label")
    @classmethod
    def _validate_label(cls, v: str | None) -> str | None:
        # None / "" / "auto" all mean "allocate the next free label" (see the field comment);
        # they are not labels to validate.
        if v is None or v.strip() in ("", "auto"):
            return None
        try:
            validation.validate_label(v)
        except validation.ValidationError as e:
            raise ValueError(str(e))
        return v

    from pydantic import model_validator

    @model_validator(mode="after")
    def _check_compose_source(self) -> "RegisterRequest":
        has_tpl = bool(self.compose_template_path)
        has_file = bool(self.compose_file_path)
        has_paths = bool(self.compose_file_paths)
        if not has_tpl and not has_file and not has_paths:
            raise ValueError("one of compose_template_path, compose_file_path or compose_file_paths is required")
        if (has_tpl + has_file + has_paths) > 1:
            raise ValueError("compose_template_path, compose_file_path and compose_file_paths are mutually exclusive")
        if self.nginx_conf_template_path and self.nginx_conf_file_path:
            raise ValueError("nginx_conf_template_path and nginx_conf_file_path are mutually exclusive")
        _lists = bool(self.nginx_conf_template_paths) + bool(self.nginx_conf_file_paths)
        _singles = bool(self.nginx_conf_template_path) + bool(self.nginx_conf_file_path)
        if _lists and _singles:
            raise ValueError(
                "the conf LIST and the single conf fields are mutually exclusive — "
                "pass nginx_conf_template_paths/nginx_conf_file_paths (or one single field)"
            )
        if (self.nginx_conf_template_paths and self.nginx_conf_file_paths):
            raise ValueError("nginx_conf_template_paths and nginx_conf_file_paths are mutually exclusive")
        # Resolve the toggle once, here, so nothing downstream has to know about
        # the deprecated alias.
        if self.tls is not None and self.https is not None and bool(self.tls) != bool(self.https):
            raise ValueError("tls and https disagree — pass one (https is a deprecated alias of tls)")
        tls_on = bool(self.tls if self.tls is not None else self.https)
        object.__setattr__(self, "tls", tls_on)

        # G41 — the stream category is applicable ONLY when TLS is on. A stream
        # conf carries no certificates of its own: the renderer fills them from
        # the deploy's recorded TLS paths, and with tls=false that rendered
        # `ssl_certificate ;`, failing `nginx -t` and (silently) the reload.
        # Refuse the incoherent combination rather than deploy a dead vhost.
        _has_stream = bool(
            (self.nginx_confs or {}).get("stream")
            or (self.nginx_conf_templates or {}).get("stream")
        )
        if not _has_stream:
            _legacy = list(self.nginx_conf_file_paths or []) + list(
                self.nginx_conf_template_paths or []
            )
            for _one in (
                [self.nginx_conf_file_path] if self.nginx_conf_file_path else []
            ) + ([self.nginx_conf_template_path] if self.nginx_conf_template_path else []):
                _legacy.append(_one)
            _has_stream = any(_conf_category(p) == "stream" for p in _legacy)
        if _has_stream and not tls_on:
            raise ValueError(
                "stream nginx confs require tls=true — the stream category is "
                "only applicable with TLS (its blocks carry the instance's certificates)"
            )

        has_certs = bool(self.fullchain) or bool(self.privkey)
        if tls_on:
            # Both certs, or the vhost cannot be rendered (unchanged behaviour).
            if not self.fullchain:
                raise ValueError("fullchain is required when tls=True")
            if not self.privkey:
                raise ValueError("privkey is required when tls=True")
        elif has_certs:
            # R9.1's second incoherent combination, and the one real footgun in
            # the old shape: certs supplied with the toggle OFF used to be
            # silently ignored — the operator believed they had TLS and got
            # plain HTTP with no hint at all. Loud, never silent.
            raise ValueError(
                "certificates were supplied but tls=false — either set tls=true "
                "or drop the certificates (they are ignored otherwise)"
            )

        # Owner form: at least one; the alias must agree when both are present.
        alias = self.owner_username or self.user_name
        if self.owner_username and self.user_name and self.owner_username != self.user_name:
            raise ValueError("owner_username and user_name disagree — pass one")
        if not self.owner_api_user_id and not alias:
            raise ValueError("one of owner_api_user_id or owner_username is required")
        return self


# The conf categories (G55). The category — not the input file name — decides
# the converter and the rendered output suffix (`*.nginx.conf` / `*.stream.conf`
# / `*.mail.conf`, which is what the internal nginx's includes select on).
NGINX_CATEGORIES = ("http", "stream", "mail")


def _conf_category(path: str) -> str:
    """The category of a LEGACY conf path, by its file name (G55's shim)."""
    name = Path(path).name
    if name.endswith(".j2"):
        name = name[:-3]
    if name.endswith(".stream.conf"):
        return "stream"
    if name.endswith(".mail.conf"):
        return "mail"
    return "http"


def _requested_conf_maps(req: "RegisterRequest") -> tuple[dict[str, str], dict[str, str]]:
    """Normalize the conf fields into ``(plain, templates)`` category maps.

    G55: the map is the new shape; the singular/list fields are legacy shims.
    A legacy LIST carrying two entries of one category is a loud 422 — the old
    renderer would have silently overwritten one with the other.
    """
    legacy_present = bool(
        req.nginx_conf_file_path or req.nginx_conf_template_path
        or req.nginx_conf_file_paths or req.nginx_conf_template_paths
    )
    if (req.nginx_confs or req.nginx_conf_templates) and legacy_present:
        raise HTTPException(
            422,
            "nginx_confs/nginx_conf_templates cannot be combined with the legacy "
            "nginx_conf_file_path(s)/nginx_conf_template_path(s) fields",
        )

    if req.nginx_confs is not None or req.nginx_conf_templates is not None:
        plain = dict(req.nginx_confs or {})
        templates = dict(req.nginx_conf_templates or {})
        for cat, path in list(plain.items()) + list(templates.items()):
            if cat not in NGINX_CATEGORIES:
                raise HTTPException(
                    422,
                    f"unknown nginx conf category {cat!r} — expected one of "
                    f"{', '.join(NGINX_CATEGORIES)}",
                )
            if not isinstance(path, str) or not path.strip():
                raise HTTPException(422, f"nginx conf for category {cat!r} must be a non-empty path")
        both = set(plain) & set(templates)
        if both:
            raise HTTPException(
                422,
                f"category {sorted(both)[0]!r} is supplied as both a plain conf and a template — "
                "one source per category",
            )
        return plain, templates

    plain: dict[str, str] = {}
    templates: dict[str, str] = {}
    for path in list(req.nginx_conf_file_paths or []) + (
        [req.nginx_conf_file_path] if req.nginx_conf_file_path else []
    ):
        cat = _conf_category(path)
        if cat in plain:
            raise HTTPException(
                422,
                f"two nginx confs of category {cat!r} ({plain[cat]!r} and {path!r}) — "
                "one file per category",
            )
        plain[cat] = path
    for path in list(req.nginx_conf_template_paths or []) + (
        [req.nginx_conf_template_path] if req.nginx_conf_template_path else []
    ):
        cat = _conf_category(path)
        if cat in templates:
            raise HTTPException(
                422,
                f"two nginx conf templates of category {cat!r} "
                f"({templates[cat]!r} and {path!r}) — one file per category",
            )
        templates[cat] = path
    both = set(plain) & set(templates)
    if both:
        raise HTTPException(
            422,
            f"category {sorted(both)[0]!r} is supplied as both a plain conf and a template — "
            "one source per category",
        )
    return plain, templates


class RebuildRequest(BaseModel):
    no_cache: bool = False
    build_args: dict[str, str] | None = None


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

import logging
from contextlib import asynccontextmanager

_log = logging.getLogger("provision-api")


def _resolv_startup_pass() -> None:
    """One re-apply over services with ``extra_resolver`` set (G83).

    Not a periodic reconcile: it runs once per process start, repairing a
    restart burst that happened while the -api was down. Errors are logged per
    service and never stop the pass.
    """
    try:
        result = resolv_conf.startup_pass()
        print(
            f"[provision-api] extra-resolver startup pass: "
            f"{result['applied']} service(s) re-applied, "
            f"{len(result['errors'])} error(s)",
            flush=True,
        )
    except Exception:
        _log.exception("extra-resolver startup pass failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: reconnect nginx to all user networks from registry.

    v5 (decision 1/6): ``-api`` no longer reads ``ENABLE_ACL`` / ``PORTAL_MODE``
    and no longer writes the v4 ``env.d`` / ``portal.d`` dirs — the internal
    nginx is services-only and the portal/ACL moved to the edge ``-nginx-acl``.
    Recovery regenerates the simple ACL-free per-service confs.
    """
    # T1 — wait for PostgreSQL and apply the registry migrations before any
    # registry read. The compose order is retry-based, so this blocks on a
    # fresh stack until the database healthcheck passes and the -api schema is
    # usable; it fails the process closed if the database never arrives.
    from lib import db as _db
    try:
        _db.migrate()
        _db.get_pool()
        # The keyring must exist before anything is encrypted; if ciphertext
        # exists without it, refuse rather than generate a replacement key.
        from lib import keys as _keys
        _keys.ensure_for_startup(registry.encrypted_basic_count())
        # A runner does not survive a restart: live rows become `unknown`, and
        # the recovery sweep terminalizes them `failed` (R4.2/R6.6).
        task_manager.recover_on_startup()
        _log.info("registry database ready")
    except Exception:
        _log.exception("registry database unavailable — refusing to start")
        raise
    _log.info("provision-api starting — nginx recovery (v5 simple confs)")
    try:
        result = reconciliation.recover_on_startup()
        _log.info(
            "Recovery: %d/%d networks reconnected, nginx %s",
            result["networks_reconnected"],
            result["networks_total"],
            "reloaded" if result["nginx_reloaded"] else "NOT reloaded",
        )
    except Exception:
        _log.exception("Startup recovery failed")
    try:
        terminalized = task_manager.terminalize_unknown(
            "imported/restarted task had no surviving runner"
        )
        if terminalized:
            _log.info("terminalized %d unknown task(s) as failed", terminalized)
    except Exception:
        _log.exception("Task recovery failed")
    # Fail-closed policy refresh even when recovery failed: write the file from
    # the current network and reload only if the content changed. With no file
    # the stream context's static `deny all;` already applies.
    try:
        stream_access.refresh_and_reload(NGINX_CONTAINER, only_if_changed=True)
    except Exception:
        _log.exception("Startup stream-policy refresh failed")
    # The recovery above runs when THIS process starts; it cannot run when the
    # INTERNAL NGINX is recreated underneath it — and a recreate is what destroys
    # the per-user attachments in the first place (GRILLING G46; measured
    # 2026-10-11 — see `watch_for_recreates`). The watcher closes that hole: it
    # holds Docker's own event stream and repairs the moment the container
    # starts, so the outage lasts seconds instead of until the next restart or
    # the next scheduled reconcile — which is disabled by default.
    import threading
    threading.Thread(
        target=reconciliation.watch_for_recreates,
        kwargs={"nginx_container": NGINX_CONTAINER},
        name="nginx-recreate-watch",
        daemon=True,
    ).start()
    # `print`, not `_log`: this module's logger has no handler under uvicorn, so
    # an INFO line goes nowhere and a watcher that failed to start would be
    # indistinguishable from one that is quietly working.
    print(f"[provision-api] nginx recreate watcher started "
          f"(container={NGINX_CONTAINER})", flush=True)
    # R12 — an INSTANCE container started/recreated outside the -api changes the
    # address a static stream upstream resolves to, with no reload.  This second
    # watcher observes those events (filtered to registered instance containers)
    # and reloads after the endpoint is current.  It is the out-of-band
    # complement of R16's reloads, which stay.
    from lib import instance_events as _instance_events
    _instance_events.start_watcher(NGINX_CONTAINER)
    # G83 — the extra-resolver manager. The restart monitor repairs a container
    # whose /etc/resolv.conf and membership Docker regenerated; the one-time
    # startup pass repairs a burst that happened while this process was down.
    resolv_conf.start_restart_monitor()
    threading.Thread(target=_resolv_startup_pass, name="extra-resolver-startup",
                     daemon=True).start()
    threading.Thread(target=_account_reaper_loop, name="api-users-reaper",
                     daemon=True).start()
    print(f"[provision-api] instance-container watcher started "
          f"(container={NGINX_CONTAINER})", flush=True)
    print("[provision-api] extra-resolver restart monitor started", flush=True)
    yield
    try:
        _db.close_pool()
    except Exception:
        pass


app = FastAPI(title="User Provision Tool", version="1.0.0", lifespan=lifespan)


@app.exception_handler(TaskCapacityError)
async def _capacity_refusal_handler(request, exc: TaskCapacityError):
    """A saturated pool must NOT look like an accepted task (doc §4d).

    Without this, every submit site would 202 a task id that waits forever.
    A distinct non-2xx lets the gateway propagate it and the dashboard branch
    on it and show the persistent at-capacity alert.
    """
    return JSONResponse(
        status_code=503,
        content={
            "detail": str(exc),
            "error": "at_capacity",
            "running": exc.running,
            "capacity": exc.capacity,
        },
    )


# ---------------------------------------------------------------------------
# GET /health  — liveness probe (no docker call)
# ---------------------------------------------------------------------------

def reap_pending_once(grace_minutes: int = 15) -> int:
    """Delete service-less pending api_users rows older than the lease.

    Takes the same ``account:<name>`` lock as ensure/claim and re-checks both
    preconditions under it. A `users.api_user_id` FK rejection means a gateway
    row exists, so the row is left for the claim. Returns the number deleted.
    """
    deleted = 0
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT id, username FROM registry.api_users "
            "WHERE state='pending' AND deleted_at IS NULL "
            "AND pending_since < now() - make_interval(mins => %s)",
            (grace_minutes,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"account:{row['username'].casefold()}",),
            )
            fresh = conn.execute(
                "SELECT state FROM registry.api_users WHERE id=%s FOR UPDATE",
                (row["id"],),
            ).fetchone()
            if not fresh or fresh["state"] != "pending":
                continue
            services = conn.execute(
                "SELECT count(*) AS n FROM registry.services "
                "WHERE owner_api_user_id=%s AND deleted_at IS NULL",
                (row["id"],),
            ).fetchone()["n"]
            if services:
                continue
            try:
                conn.execute("DELETE FROM registry.api_users WHERE id=%s", (row["id"],))
                deleted += 1
            except Exception:
                # FK: a gateway users row owns it; the claim handles it.
                continue
    return deleted


def _account_reaper_loop() -> None:
    import time as _time
    while True:
        _time.sleep(300)
        try:
            reap_pending_once()
        except Exception:
            _log.exception("api_users reaper pass failed")


@app.get("/health")
def health() -> dict[str, str]:
    """Readiness: the registry database must be usable (T1).

    A process without its schema is not ready; reporting ok while every write
    fails is how a broken deploy looks healthy.
    """
    from lib import db as _db
    state = _db.health()
    if state["status"] != "ok":
        raise HTTPException(503, f"registry database unavailable: {state.get('error')}")
    return {"status": "ok", "database": state["database"]}


# ---------------------------------------------------------------------------
# api_users interface (T3/T6). The -api owns the pending/live lifecycle; the
# gateway calls ensure before its own users insert and claim after the commit.
# ---------------------------------------------------------------------------

class ApiUserEnsureRequest(BaseModel):
    username: str
    mode: str = Field("account", pattern="^(account|owner)$")


def _api_user_payload(row: dict) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "username": row["username"],
        "state": row["state"],
        "deleted_at": row["deleted_at"].isoformat() if row.get("deleted_at") else None,
    }


@app.post("/api-users")
def ensure_api_user(req: ApiUserEnsureRequest) -> dict[str, Any]:
    try:
        validation.validate_name(req.username, "username")
    except validation.ValidationError as e:
        raise HTTPException(422, str(e))
    with db.connection() as conn:
        try:
            oid, created = registry.ensure_owner(conn, req.username, mode=req.mode)
        except registry.RegistryError as e:
            raise HTTPException(409, str(e))
        row = conn.execute(
            "SELECT id, username, state, deleted_at FROM registry.api_users WHERE id=%s",
            (oid,),
        ).fetchone()
    payload = _api_user_payload(row)
    payload["created"] = created
    return payload


@app.post("/api-users/{api_user_id}/claim")
def claim_api_user(api_user_id: str) -> dict[str, Any]:
    try:
        uid = uuid.UUID(api_user_id)
    except (ValueError, TypeError):
        raise HTTPException(422, "api_user_id is not a UUID")
    with db.connection() as conn:
        row = conn.execute(
            "SELECT id, username, state, deleted_at FROM registry.api_users WHERE id=%s",
            (uid,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "api user not found")
        if row["deleted_at"] is not None:
            raise HTTPException(409, "api user is tombstoned")
        if row["state"] == "pending":
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"account:{row['username'].casefold()}",),
            )
            conn.execute(
                "UPDATE registry.api_users SET state='live', pending_since=%s WHERE id=%s",
                (registry.utcnow(), uid),
            )
            row = dict(row)
            row["state"] = "live"
    return _api_user_payload(row)


@app.get("/api-users/{api_user_id}")
def get_api_user(api_user_id: str) -> dict[str, Any]:
    try:
        uid = uuid.UUID(api_user_id)
    except (ValueError, TypeError):
        raise HTTPException(422, "api_user_id is not a UUID")
    row = db.fetch_one(
        "SELECT id, username, state, deleted_at FROM registry.api_users WHERE id=%s", (uid,)
    )
    if not row:
        raise HTTPException(404, "api user not found")
    return _api_user_payload(row)


@app.delete("/api-users/{api_user_id}")
def delete_api_user(api_user_id: str, scope: str = Query("soft", pattern="^(soft|pending)$")):
    try:
        uid = uuid.UUID(api_user_id)
    except (ValueError, TypeError):
        raise HTTPException(422, "api_user_id is not a UUID")
    with db.connection() as conn:
        row = conn.execute(
            "SELECT id, username, state, deleted_at FROM registry.api_users WHERE id=%s",
            (uid,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "api user not found")
        if row["deleted_at"] is not None:
            return _api_user_payload(row)
        if scope == "pending":
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"account:{row['username'].casefold()}",),
            )
            row = conn.execute(
                "SELECT id, username, state, deleted_at FROM registry.api_users "
                "WHERE id=%s FOR UPDATE", (uid,),
            ).fetchone()
            services = conn.execute(
                "SELECT count(*) AS n FROM registry.services "
                "WHERE owner_api_user_id=%s AND deleted_at IS NULL", (uid,),
            ).fetchone()["n"]
            if row["state"] != "pending" or services:
                raise HTTPException(409, "row is not an unreferenced pending row")
            try:
                conn.execute("DELETE FROM registry.api_users WHERE id=%s", (uid,))
            except Exception as exc:  # noqa: BLE001 — FK means a gateway row exists
                raise HTTPException(409, f"gateway row owns this api_user: {exc}")
            return {"id": str(uid), "deleted": True, "scope": "pending"}
        conn.execute(
            "UPDATE registry.api_users SET deleted_at=%s WHERE id=%s",
            (registry.utcnow(), uid),
        )
        row = dict(row)
        row["deleted_at"] = registry.utcnow()
    return _api_user_payload(row)


@app.post("/api-users/{api_user_id}/stop-services")
def stop_api_user_services(api_user_id: str) -> dict[str, Any]:
    try:
        uid = uuid.UUID(api_user_id)
    except (ValueError, TypeError):
        raise HTTPException(422, "api_user_id is not a UUID")
    entries = registry.owner_services(uid)
    busy = [e["service_id"] for e in entries
            if e.get("status") in ("building", "deleting")]
    if busy:
        raise HTTPException(409, {"reason": "busy", "services": busy})
    results = []
    for entry in entries:
        try:
            provisioner.stop_service(service_id=entry["service_id"],
                                     nginx_container=NGINX_CONTAINER)
            results.append({"service_id": entry["service_id"], "ok": True})
        except Exception as exc:  # noqa: BLE001 — report every failure
            results.append({"service_id": entry["service_id"], "ok": False, "error": str(exc)})
    return {"api_user_id": str(uid), "ok": all(r["ok"] for r in results), "results": results}


# ---------------------------------------------------------------------------
# GET /docker/ps  — list all containers
# ---------------------------------------------------------------------------

@app.get("/docker/ps")
def docker_ps_list() -> list[dict[str, Any]]:
    """Return all Docker containers (docker ps -a)."""
    return docker_ops.docker_ps()


# ---------------------------------------------------------------------------
# GET /docker/stats  — per-container resource stats
# ---------------------------------------------------------------------------

@app.get("/docker/stats")
def docker_stats() -> list[dict[str, Any]]:
    """Return docker stats snapshot."""
    return docker_ops.docker_stats_snapshot()


# ---------------------------------------------------------------------------
# GET /docker/info  — docker host info
# ---------------------------------------------------------------------------

@app.get("/docker/info")
def docker_info() -> dict[str, Any]:
    """Return docker system info (container counts, etc.)."""
    return docker_ops.docker_info()


# ---------------------------------------------------------------------------
# GET /host/stats  — host-level CPU/memory/disk
# ---------------------------------------------------------------------------

@app.get("/host/stats")
def host_stats() -> dict[str, Any]:
    """Return host-level CPU, memory, and disk usage."""
    import shutil, re
    stats: dict[str, Any] = {}

    # Memory
    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    mem[parts[0].strip()] = int(parts[1].strip().split()[0])
        total = mem.get("MemTotal", 1)
        available = mem.get("MemAvailable", mem.get("MemFree", 0))
        stats["mem_percent"] = round((1 - available / total) * 100, 1)
        stats["mem_total_kb"] = total
        stats["mem_used_kb"] = total - available
    except Exception:
        stats["mem_percent"] = 0

    # CPU
    try:
        with open("/proc/stat") as f:
            cpu_line = f.readline()
        parts = [int(x) for x in cpu_line.split()[1:]]
        idle = parts[3]
        total_cpu = sum(parts)
        stats["cpu_percent"] = round((1 - idle / total_cpu) * 100, 1) if total_cpu > 0 else 0
    except Exception:
        stats["cpu_percent"] = 0

    # Disk
    try:
        usage = shutil.disk_usage("/")
        stats["disk_percent"] = round((1 - usage.free / usage.total) * 100, 1)
        stats["disk_total_gb"] = round(usage.total / (1024**3), 1)
        stats["disk_free_gb"] = round(usage.free / (1024**3), 1)
    except Exception:
        stats["disk_percent"] = 0

    return stats


# ---------------------------------------------------------------------------
# Reconciliation helpers (called by gateway)
# ---------------------------------------------------------------------------

@app.get("/docker/container/{container}/exists")
def container_exists_ep(container: str) -> dict[str, Any]:
    return {"exists": docker_ops.container_exists(container)}

@app.get("/docker/container/{container}/running")
def container_running_ep(container: str) -> dict[str, Any]:
    return {"running": docker_ops.container_running(container)}

@app.get("/docker/container/{container}/env")
def container_env_ep(container: str) -> dict[str, Any]:
    """Return a container's environment as a dict (used to read e.g. ENABLE_ACL)."""
    inspect = docker_ops.container_inspect(container)
    env: dict[str, str] = {}
    if inspect:
        for kv in (inspect.get("Config", {}).get("Env") or []):
            if "=" in kv:
                k, v = kv.split("=", 1)
                env[k] = v
    return {"env": env, "exists": inspect is not None}

@app.post("/docker/network/{network}/connect/{container}")
def network_connect_ep(network: str, container: str) -> dict[str, Any]:
    docker_ops.network_connect(container, network)
    return {"connected": True}

@app.post("/docker/nginx/reload")
def nginx_reload_ep(container: str = "subnet-acl-nginx") -> dict[str, Any]:
    result = stream_access.refresh_and_reload(container)
    return {"reloaded": True, **result}


@app.post("/docker/nginx/env")
def nginx_env_ep(container: str = "subnet-acl-nginx") -> dict[str, Any]:
    """DEPRECATED (v5, decision 1/16) — removed.

    v5 ``-api`` no longer reads ``ENABLE_ACL`` / ``PORTAL_MODE`` and no longer
    writes ``env.d`` / ``portal.d`` (the internal nginx is services-only).
    ``ENABLE_ACL`` touches exactly two components — the gateway and the edge
    ``-nginx-acl`` — and toggling it means recreating the edge (env is baked at
    start) + restarting the gateway, with NO per-service conf change.
    """
    raise HTTPException(410, "POST /docker/nginx/env is removed in v5: ENABLE_ACL "
                             "touches only the gateway and the edge -nginx-acl.")


# ---------------------------------------------------------------------------
# POST /users  — register (async by default; ?sync=true to block)
# ---------------------------------------------------------------------------

@app.post("/users", status_code=202)
def register_user(
    req: RegisterRequest,
    sync: bool = Query(False, description="If true, block until registration completes"),
) -> dict[str, Any]:
    # --- RACE WINDOW (documented, not mitigated — design §Impl notes L270-272) ---
    # Generation-save vs deploy is CROSS-PROCESS: the gateway job writes the
    # per-recipe files (atomic write-then-marker), while this register flow
    # reads them at render time.  There is no shared lock between the two
    # processes; a deploy that reads a compose/env file mid-generation simply
    # sees the pre-write (or pre-marker) snapshot — the narrow acceptable
    # race window is intentional and documented, NOT mitigated.
    # --- Resolve project_root: bare name → SOURCE_PROJECTS_DIR/{name} ---
    resolved_root: Path | None = None
    if req.project_root:
        raw = Path(req.project_root)
        if raw.is_absolute() or raw.is_dir():
            resolved_root = raw
        else:
            resolved_root = SOURCE_PROJECTS_DIR / req.project_root
        if not resolved_root.is_dir():
            raise HTTPException(404, f"project_root not found: {resolved_root}")

    # --- Resolve paths relative to project_root when given ---
    def _resolve(p: str | None) -> str | None:
        if p is None:
            return None
        if resolved_root and not Path(p).is_absolute():
            return str(resolved_root / p)
        return p

    compose_file_path      = _resolve(req.compose_file_path)
    compose_file_paths     = [_resolve(p) for p in (req.compose_file_paths or [])]
    compose_template_path  = _resolve(req.compose_template_path)
    # G55 — one source per category. The legacy singular/list fields are
    # normalized here (and a duplicate category is refused), so everything
    # downstream has one shape: {category: path}.
    _plain_by_cat, _tpl_by_cat = _requested_conf_maps(req)
    nginx_conf_files_by_cat: dict[str, str] = {
        cat: _resolve(p) for cat, p in _plain_by_cat.items()
    }
    nginx_conf_templates_by_cat: dict[str, str] = {
        cat: _resolve(p) for cat, p in _tpl_by_cat.items()
    }
    env_file_path          = _resolve(req.env_file_path)
    env_files_resolved     = [_resolve(p) for p in (req.env_files or [])]

    # --- Label allocation (R2.3) ----------------------------------------------
    # A concrete label is honoured; otherwise `prepare_registration` allocates
    # max(label)+1 over active AND historical rows under the per-owner/service
    # advisory lock. A concurrent loser hits the partial unique index (409).
    label = req.label
    owner_username = req.owner_username or req.user_name

    # --- Recipe path (R2.3) ----------------------------------------------------
    # Sent by the gateway; a direct/CLI deploy derives it from the recorded
    # compose/env paths. More than one distinct recipe is a 422, never a guess.
    recipe_path = req.recipe_path
    if recipe_path is None:
        candidates: list[str] = []
        for p in ([compose_template_path] if compose_template_path else []) + \
                 ([compose_file_path] if compose_file_path else []) + compose_file_paths:
            if p:
                candidates.append(p)
        project = (SOURCE_PROJECTS_DIR / req.service_name).resolve()
        found: set[str] = set()
        for c in candidates:
            try:
                rel = Path(c).resolve().parent.relative_to(project)
            except (ValueError, OSError):
                continue
            found.add("" if str(rel) == "." else rel.as_posix())
        if len(found) > 1:
            raise HTTPException(
                422, f"ambiguous recipe path for {req.service_name}: {sorted(found)}"
            )
        recipe_path = next(iter(found)) if found else ""

    # --- Resolve compose template (convert plain file(s) if needed) ---
    compose_sources: list[str] = []
    if compose_file_paths:
        for p in compose_file_paths:
            if not Path(p).exists():
                raise HTTPException(404, f"compose_file_paths entry not found: {p}")
        compose_sources = compose_file_paths
        if len(compose_file_paths) == 1:
            # Single file: direct conversion path unchanged (design F15).
            src = Path(compose_file_paths[0])
            template_out = str(src.parent / f"{src.stem}.yml.j2")
            try:
                compose_file_to_template(str(src), template_out, service_name_hint=req.service_name)
            except Exception as e:
                raise HTTPException(422, f"could not convert compose file: {e}")
            compose_template = template_out
        else:
            # ≥2 files: ordered merge → ONE template named <first>.merged.yml.j2
            # (design §Compose multi-file L68-80, §Impl notes L262-266).
            from lib.compose_merge import merge_compose_files
            first = Path(compose_file_paths[0])
            merged_out = str(first.parent / f"{first.stem}.merged.yml")
            template_out = str(first.parent / f"{first.stem}.merged.yml.j2")
            try:
                merge_compose_files(compose_file_paths, merged_out)
                compose_file_to_template(merged_out, template_out, service_name_hint=req.service_name)
            except RuntimeError as e:
                raise HTTPException(422, f"compose merge failed: {e}")
            except Exception as e:
                raise HTTPException(422, f"could not convert merged compose file: {e}")
            compose_template = template_out
    elif compose_file_path:
        src = Path(compose_file_path)
        if not src.exists():
            raise HTTPException(404, f"compose_file_path not found: {compose_file_path}")
        compose_sources = [compose_file_path]
        template_out = str(src.parent / f"{src.stem}.yml.j2")
        try:
            compose_file_to_template(str(src), template_out, service_name_hint=req.service_name)
        except Exception as e:
            raise HTTPException(422, f"could not convert compose file: {e}")
        compose_template = template_out
    else:
        if not Path(compose_template_path).exists():
            raise HTTPException(404, f"compose_template_path not found: {compose_template_path}")
        compose_template = compose_template_path

    # --- Resolve nginx template (convert plain file if needed) ---
    # Extract compose service names so proxy_pass targets matching a compose
    # service name can be rewritten to use {{ container_prefix }}.
    _compose_svc_names: list[str] = []
    try:
        _compose_src = compose_sources[0] if compose_sources else compose_template_path
        if _compose_src:
            _compose_svc_names = get_compose_service_names(_compose_src)
    except Exception:
        pass

    # Each conf is CONVERTED BY CATEGORY (G55): an http/mail conf takes the http
    # substitutions, a stream conf takes the stream's own (no auth_basic, no ssl
    # guard, a templated `server_name` — G56). The category, not the input file
    # name, decides both the converter and the rendered output suffix.
    converted_by_cat: dict[str, str] = {}
    for _cat, _path in nginx_conf_files_by_cat.items():
        src = Path(_path)
        if not src.exists():
            raise HTTPException(404, f"nginx conf not found: {_path}")
        template_out = str(src.parent / f"{src.name}.j2")
        if _cat == "stream":
            from lib.nginx_converter import convert_stream_conf
            try:
                Path(template_out).write_text(
                    convert_stream_conf(
                        src.read_text(encoding="utf-8", errors="replace"),
                        req.service_name,
                        compose_service_names=_compose_svc_names or None,
                    ),
                    encoding="utf-8",
                )
            except Exception as e:
                raise HTTPException(422, f"could not convert stream conf: {e}")
        else:
            # --- Validate: every proxy_pass host must reference a compose service ---
            if _compose_svc_names:
                _validate_nginx_proxy_targets(src, _compose_svc_names)
            try:
                # R14 — a NEW selection naming the instance host (or any name
                # without `--<sub_service>`) is refused before anything is
                # written; every HTTP endpoint is an equal `--<sub>` route.
                from lib.nginx_converter import reject_bare_http_server_names
                reject_bare_http_server_names(
                    src.read_text(encoding="utf-8", errors="replace")
                )
                nginx_file_to_template(
                    str(src), template_out, req.service_name,
                    compose_service_names=_compose_svc_names or None,
                )
            except Exception as e:
                raise HTTPException(422, f"could not convert nginx conf file: {e}")
        converted_by_cat[_cat] = template_out
    for _cat, _tpl in nginx_conf_templates_by_cat.items():
        if not Path(_tpl).exists():
            raise HTTPException(404, f"nginx conf template not found: {_tpl} ({_cat})")
        # A SELECTED `.j2` is normalized with the same certificate and policy
        # rules as a converted plain conf (R5/R8/R11) — the operator is not
        # asked to know the platform's certificate store or its network. The
        # source file is never rewritten: a changed copy is written beside it.
        from lib.nginx_converter import (
            normalize_http_template, normalize_stream_template,
            reject_bare_http_server_names,
        )
        _src = Path(_tpl)
        _text = _src.read_text(encoding="utf-8", errors="replace")
        if _cat == "stream":
            _normalize = normalize_stream_template
        elif _cat == "http":
            _normalize = normalize_http_template
        else:
            # Mail is outside this feature's scope; its template is used as-is.
            converted_by_cat[_cat] = _tpl
            continue
        try:
            # R14 — the same refusal for a selected `.j2`: a template that still
            # names the instance host (or any bare name) is not a valid new
            # selection. A legacy normalized copy is not rewritten here.
            if _cat == "http":
                reject_bare_http_server_names(_text)
            _norm = _normalize(_text)
        except ValueError as e:
            raise HTTPException(422, f"could not normalize {_cat} template {_tpl}: {e}")
        if _norm != _text:
            _out = _src.parent / f"{_src.stem}.normalized{_src.suffix}"
            try:
                _out.write_text(_norm, encoding="utf-8")
                Path(str(_out) + ".generated").write_text("")
            except OSError as e:
                raise HTTPException(500, f"could not write normalized template: {e}")
            _tpl = str(_out)
        converted_by_cat[_cat] = _tpl

    nginx_templates_by_cat: dict[str, str] = {
        cat: converted_by_cat[cat] for cat in NGINX_CATEGORIES if cat in converted_by_cat
    }
    nginx_templates: list[str] = list(nginx_templates_by_cat.values())
    nginx_template: str | None = (
        nginx_templates_by_cat.get("http") or (nginx_templates[0] if nginx_templates else None)
    )

    # R10 — a STREAM registration fails closed when the platform network cannot
    # be resolved: its conf would carry `allow {{ stream_allow_subnet }}`, and an
    # unfillable value is an unscoped or unloadable door, never a default. An
    # HTTP registration never asks and proceeds.
    if "stream" in nginx_templates_by_cat:
        try:
            docker_ops.shared_network_subnet()
        except Exception as e:  # noqa: BLE001 — every cause refuses the same way
            raise HTTPException(422, f"stream registration refused: {e}")

    # --- Build kwargs for provisioner call ---
    prov_kwargs = dict(
        owner_api_user_id=req.owner_api_user_id,
        owner_username=owner_username,
        recipe_path=recipe_path,
        service_name=req.service_name,
        label=label,
        compose_template=compose_template,
        output_dir=Path(compose_template).parent,
        nginx_output_dir=GENERATED_DIR,
        volumes=req.volumes or None,
        user_data_dir=USER_DATA_DIR,
        passwd=req.passwd,
        nginx_template=nginx_template,
        nginx_conf_templates=list(nginx_templates),
        nginx_conf_templates_by_category=nginx_templates_by_cat,
        domain=req.domain,
        env_file=env_file_path,
        env_files=env_files_resolved or None,
        profiles=req.profiles or None,
        service_env_files=req.service_env_files or None,
        compose_sources=compose_sources or None,
        nginx_container=NGINX_CONTAINER,
        build_args=req.build_args,
        # The REGISTRY's stored key stays `https`: it is read by three services
        # and by nine existing entries, and the doc's rename covers the request,
        # the CLI and the template variable — not the stored field. The mapping
        # lives here, in one place.
        https=bool(req.tls),
        fullchain=req.fullchain,
        privkey=req.privkey,
        ssl_base_dir=str(SSL_DIR),
    )

    # --- Reserve the identity BEFORE 202 (R6.1) --------------------------------
    task_id = task_manager.new_task_id()
    try:
        prepared = provisioner.prepare_registration(
            service_id=req.service_id,
            procedure_id=req.procedure_id,
            task_id=task_id,
            owner_api_user_id=req.owner_api_user_id,
            owner_username=owner_username,
            service_name=req.service_name,
            label=label,
            recipe_path=recipe_path,
            domain=req.domain,
        )
    except registry.RegistryError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    if prepared.get("replayed"):
        return {
            "status": "accepted",
            "service_id": prepared["service_id"],
            "procedure_id": prepared["procedure_id"],
            "task_id": prepared["task_id"],
            "message": "Identical deploy replay: the existing instance/task is current.",
        }
    prov_kwargs.update(
        service_id=prepared["service_id"],
        procedure_id=prepared["procedure_id"],
    )
    label = prepared["label"]

    if sync:
        # Backward-compatible blocking call
        try:
            result = provisioner.register_user(**prov_kwargs)
        except ValueError as e:
            task_manager.finish(task_id, "failed", str(e))
            raise HTTPException(409, str(e))
        except RuntimeError as e:
            task_manager.finish(task_id, "failed", str(e))
            raise HTTPException(500, f"docker compose up failed: {e}")
        task_manager.finish(task_id, "completed")
        return {
            "status": "registered",
            "service_id": prepared["service_id"],
            "procedure_id": prepared["procedure_id"],
            "task_id": task_id,
            "entry": result["entry"],
            "volume_warnings": result["volume_warnings"],
            "copied_env": result.get("copied_env"),
        }

    # Async: submit the worker under the already-inserted task row.
    submit_kwargs = dict(prov_kwargs)
    submit_service_id = submit_kwargs.pop("service_id")
    submit_procedure_id = submit_kwargs.pop("procedure_id")
    try:
        task_manager.submit(
            "register", provisioner.register_user,
            task_id=task_id,
            procedure_id=submit_procedure_id,
            service_id=submit_service_id,
            persist=False,
            **submit_kwargs,
        )
    except TaskCapacityError:
        provisioner.abort_new_registration(
            prepared["service_id"], task_id, created=prepared.get("created_service", False)
        )
        raise
    return {
        "service_id": prepared["service_id"],
        "procedure_id": prepared["procedure_id"],
        "task_id": task_id,
        "status": "queued",
        "type": "register",
        "message": f"Registration queued.  Poll GET /tasks/{task_id} for status.",
    }


# ---------------------------------------------------------------------------
# DELETE /users/{user_name}/services/{service_name}/{label}  — remove (async)
# ---------------------------------------------------------------------------

@app.delete("/users/{user_name}/services/{service_name}/{label}")
def remove_user(
    user_name: str, service_name: str, label: str,
    sync: bool = Query(False, description="If true, block until removal completes"),
) -> dict[str, Any]:
    sid = registry.resolve_service_id(
        None, user_name=user_name, service_name=service_name, label=label
    )
    if not sid:
        raise HTTPException(404, f"No registration found for {user_name}/{service_name}/{label}.")
    return delete_instance_route(sid, "service", sync)


# ---------------------------------------------------------------------------
# POST /users/{user_name}/services/{service_name}/{label}/rebuild  (async)
# ---------------------------------------------------------------------------

@app.post("/users/{user_name}/services/{service_name}/{label}/rebuild")
def rebuild_user(
    user_name: str, service_name: str, label: str,
    req: RebuildRequest = RebuildRequest(),
    sync: bool = Query(False, description="If true, block until rebuild completes"),
) -> dict[str, Any]:
    sid = registry.resolve_service_id(
        None, user_name=user_name, service_name=service_name, label=label
    )
    if not sid:
        raise HTTPException(404, f"No registration found for {user_name}/{service_name}/{label}.")
    return rebuild_instance_route(sid, req, sync)


# ---------------------------------------------------------------------------
# P5: PUT /users/{user_name}/{service_name}/{label}/password  — change password
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# POST /users/{user_name}/services/{service_name}/{label}/up  — start containers
# ---------------------------------------------------------------------------

@app.post("/users/{user_name}/services/{service_name}/{label}/up")
def start_user_service(
    user_name: str, service_name: str, label: str,
) -> dict[str, Any]:
    """Start a user's service containers (docker compose up -d)."""
    try:
        provisioner.start_service(
            user_name=user_name,
            service_name=service_name,
            label=label,
            nginx_container=NGINX_CONTAINER,
        )
    except KeyError as e:
        raise HTTPException(404, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return {"message": "Service started.", "status": "up"}


# ---------------------------------------------------------------------------
# POST /users/{user_name}/services/{service_name}/{label}/down  — stop containers
# ---------------------------------------------------------------------------

@app.post("/users/{user_name}/services/{service_name}/{label}/down")
def stop_user_service(
    user_name: str, service_name: str, label: str,
) -> dict[str, Any]:
    """Stop a user's service containers (docker compose stop)."""
    try:
        result = provisioner.stop_service(
            user_name=user_name,
            service_name=service_name,
            label=label,
            nginx_container=NGINX_CONTAINER,
        )
    except KeyError as e:
        raise HTTPException(404, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return {"message": "Service stopped.", "status": "down", **(result or {})}


@app.post("/users/{user_name}/stop")
def stop_all_user_services(user_name: str) -> dict[str, Any]:
    """Stop every service of one username; keep registry/files/containers.

    Account deletion (G78) calls this BEFORE touching the account. It is
    deliberately a **200 with per-service results** even when some stops fail:
    the caller must abort on ``ok: false`` and report which services failed,
    not lose that list in a generic HTTP error. Every entry is attempted; a
    failure does not skip the rest, and an already-stopped service stays
    stopped (``stop_service`` is idempotent).
    """
    entries = registry.get_user(user_name)
    results: list[dict[str, Any]] = []
    for entry in entries:
        service_name = str(entry.get("service_name", ""))
        label = str(entry.get("label", ""))
        try:
            detail = provisioner.stop_service(
                user_name=user_name,
                service_name=service_name,
                label=label,
                nginx_container=NGINX_CONTAINER,
            )
            results.append({
                "service_name": service_name,
                "label": label,
                "ok": True,
                "detail": detail or {},
            })
        except Exception as exc:  # noqa: BLE001 — report every failure, keep going
            results.append({
                "service_name": service_name,
                "label": label,
                "ok": False,
                "error": str(exc),
            })

    failed = [r for r in results if not r["ok"]]
    return {
        "user_name": user_name,
        "ok": not failed,
        "results": results,
        "failed_services": [
            {"service_name": r["service_name"], "label": r["label"],
             "error": r["error"]}
            for r in failed
        ],
    }


# ---------------------------------------------------------------------------
# Canonical ID routes (R7.7). The name routes above stay as compatibility
# aliases that resolve exactly one active service_id before mutating.
# ---------------------------------------------------------------------------


class InstancePasswordChangeRequest(BaseModel):
    passwd: str = Field(..., min_length=1)


def _insert_sync_task(task_type: str, procedure_id: str, service_id: str,
                      operation: str | None = None) -> str:
    task_id = task_manager.new_task_id()
    kind, op = {"register": ("deploy", "register"),
                "rebuild": ("rebuild", "rebuild"),
                "remove": ("remove", "remove"),
                "fix_remove": ("remove", "fix_remove")}[task_type]
    with db.connection() as conn:
        registry.insert_task(
            conn, task_id=task_id, procedure_id=uuid.UUID(procedure_id),
            service_id=uuid.UUID(service_id), kind=kind, operation=operation or op,
        )
    return task_id


@app.get("/instances/{service_id}")
def get_instance_status(service_id: str) -> dict[str, Any]:
    entry = registry.get_service_by_id(service_id)
    if not entry:
        raise HTTPException(404, f"Service not found: {service_id}")
    running = {c["name"]: c["status"] for c in docker_ops.docker_ps_all()}
    data = _status_for_user(entry["user_name"], running)
    for bucket in ("healthy_services", "unhealthy_services", "missing_services",
                   "failed_services"):
        for svc in data.get(bucket, []):
            if svc.get("service_id") == service_id:
                return svc
    return {
        "service_id": service_id, "owner_api_user_id": entry["owner_api_user_id"],
        "service_name": entry["service_name"], "label": entry["label"],
        "status": entry.get("status") or "unknown",
    }


class ExtraResolverRequest(BaseModel):
    resolver_ip: str = Field(..., min_length=1)
    network: str = Field(..., min_length=1)


@app.get("/instances/{service_id}/extra-resolver")
def get_extra_resolver(service_id: str) -> dict[str, Any]:
    """The opaque per-service pair the gateway supplied (G83)."""
    entry = registry.get_service_by_id(service_id)
    if not entry:
        raise HTTPException(404, f"Service not found: {service_id}")
    return {
        "service_id": service_id,
        "extra_resolver": entry.get("extra_resolver"),
        "extra_network": entry.get("extra_network"),
    }


@app.post("/instances/{service_id}/extra-resolver", status_code=202)
def set_extra_resolver(service_id: str, req: ExtraResolverRequest) -> dict[str, Any]:
    """Async update: two lines + membership for **every container** (G83).

    The request is addressed by source service; the ``-api`` fans it out and
    stores the pair after every RUNNING container was updated. G91 — a service
    with no running container still stores the pair (the restart monitor applies
    it on start). The caller polls the returned task id.
    """
    entry = registry.get_service_by_id(service_id)
    if not entry:
        raise HTTPException(404, f"Service not found: {service_id}")
    task_id = task_manager.submit(
        "resolv_conf_set", resolv_conf.apply_service,
        service_id=service_id, resolver_ip=req.resolver_ip, network=req.network,
    )
    return {"service_id": service_id, "task_id": task_id, "status": "queued"}


@app.delete("/instances/{service_id}/extra-resolver", status_code=202)
def clear_extra_resolver(service_id: str) -> dict[str, Any]:
    """Async removal: restore the default, disconnect, clear both fields."""
    entry = registry.get_service_by_id(service_id)
    if not entry:
        raise HTTPException(404, f"Service not found: {service_id}")
    task_id = task_manager.submit(
        "resolv_conf_clear", resolv_conf.clear_service, service_id=service_id,
    )
    return {"service_id": service_id, "task_id": task_id, "status": "queued"}


@app.delete("/instances/{service_id}")
def delete_instance_route(
    service_id: str,
    scope: str = Query("service", pattern="^(instance|service)$"),
    sync: bool = Query(False),
) -> dict[str, Any]:
    entry = registry.get_service_by_id(service_id)
    if not entry:
        raise HTTPException(404, f"Service not found: {service_id}")
    procedure_id = str(uuid.uuid4())
    fn = provisioner.remove_user if scope == "service" else provisioner.remove_instance_only
    task_type = "remove" if scope == "service" else "fix_remove"
    if sync:
        task_id = _insert_sync_task(task_type, procedure_id, service_id)
        try:
            fn(service_id=service_id, procedure_id=procedure_id)
        except KeyError as e:
            task_manager.finish(task_id, "failed", str(e)); raise HTTPException(404, str(e))
        except ValueError as e:
            task_manager.finish(task_id, "failed", str(e)); raise HTTPException(409, str(e))
        except RuntimeError as e:
            task_manager.finish(task_id, "failed", str(e)); raise HTTPException(500, str(e))
        task_manager.finish(task_id, "completed")
        return {"service_id": service_id, "task_id": task_id, "scope": scope,
                "status": "completed"}
    task_id = task_manager.submit(
        task_type, fn, procedure_id=procedure_id, service_id=service_id,
    )
    return {"service_id": service_id, "task_id": task_id, "scope": scope,
            "status": "queued"}


@app.post("/instances/{service_id}/stop")
def stop_instance_route(service_id: str) -> dict[str, Any]:
    try:
        return {"status": "down", **provisioner.stop_service(service_id=service_id)}
    except KeyError as e:
        raise HTTPException(404, str(e))
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(404 if isinstance(e, FileNotFoundError) else 500, str(e))


@app.post("/instances/{service_id}/start")
def start_instance_route(service_id: str) -> dict[str, Any]:
    try:
        return {"status": "up", **provisioner.start_service(service_id=service_id)}
    except KeyError as e:
        raise HTTPException(404, str(e))
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(404 if isinstance(e, FileNotFoundError) else 500, str(e))


@app.post("/instances/{service_id}/rebuild")
def rebuild_instance_route(
    service_id: str,
    req: RebuildRequest = RebuildRequest(),
    sync: bool = Query(False),
) -> dict[str, Any]:
    procedure_id = str(uuid.uuid4())
    if sync:
        task_id = _insert_sync_task("rebuild", procedure_id, service_id)
        try:
            provisioner.rebuild_user(service_id=service_id, procedure_id=procedure_id,
                                     no_cache=req.no_cache, build_args=req.build_args)
        except KeyError as e:
            task_manager.finish(task_id, "failed", str(e)); raise HTTPException(404, str(e))
        except FileNotFoundError as e:
            task_manager.finish(task_id, "failed", str(e)); raise HTTPException(404, str(e))
        except RuntimeError as e:
            task_manager.finish(task_id, "failed", str(e)); raise HTTPException(500, str(e))
        task_manager.finish(task_id, "completed")
        return {"service_id": service_id, "task_id": task_id, "status": "rebuilt"}
    task_id = task_manager.submit(
        "rebuild", provisioner.rebuild_user,
        procedure_id=procedure_id, service_id=service_id,
        no_cache=req.no_cache, build_args=req.build_args,
    )
    return {"service_id": service_id, "task_id": task_id, "status": "queued"}


@app.put("/instances/{service_id}/password")
def change_instance_password(service_id: str, req: InstancePasswordChangeRequest) -> dict[str, Any]:
    try:
        return {"message": "Password updated.", **provisioner.change_password(
            service_id=service_id, passwd=req.passwd)}
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/instances/{service_id}/password")
def remove_instance_password(service_id: str) -> dict[str, Any]:
    try:
        return {"message": "Password removed.", **provisioner.remove_password(
            service_id=service_id)}
    except KeyError as e:
        raise HTTPException(404, str(e))


# ---------------------------------------------------------------------------
# P5: PUT /users/{user_name}/{service_name}/{label}/password  — change password
# ---------------------------------------------------------------------------

class PasswordChangeRequest(BaseModel):
    # Empty is REJECTED: hash_password("") returns "", which would write `user:`
    # into the htpasswd and lock the service out. Removing protection is the
    # DELETE below.
    passwd: str = Field(..., min_length=1)

@app.put("/users/{user_name}/services/{service_name}/{label}/password")
def change_user_password(
    user_name: str, service_name: str, label: str,
    req: PasswordChangeRequest,
) -> dict[str, Any]:
    try:
        result = provisioner.change_password(
            user_name=user_name,
            service_name=service_name,
            label=label,
            passwd=req.passwd,
            nginx_container=NGINX_CONTAINER,
        )
    except KeyError as e:
        raise HTTPException(404, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"message": "Password updated. Nginx reloaded.", **result}


@app.delete("/users/{user_name}/services/{service_name}/{label}/password")
def remove_user_password(
    user_name: str, service_name: str, label: str,
) -> dict[str, Any]:
    """Drop a service's password protection (auth_basic) and reload nginx.

    The exit for a protected service whose plaintext was never recorded or was
    lost: the gate can never satisfy that auth_basic again, and bcrypt gives no
    way back to the original password. The ACL gate still protects the instance.
    """
    try:
        result = provisioner.remove_password(
            user_name=user_name,
            service_name=service_name,
            label=label,
            nginx_container=NGINX_CONTAINER,
        )
    except KeyError as e:
        raise HTTPException(404, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    return {"message": "Password removed. Nginx reloaded.", **result}


# ---------------------------------------------------------------------------
# P3: GET /nginx/connections  — nginx connection state
# ---------------------------------------------------------------------------

@app.get("/nginx/connections")
def get_nginx_connections() -> dict[str, Any]:
    """Return nginx connection state:
    (a) list of networks provision-nginx is connected to
    (b) list of *.nginx.conf files in GENERATED_DIR
    (c) parsed upstreams
    """
    from pathlib import Path
    import re

    # (a) Networks provision-nginx is connected to
    nginx_info = docker_ops.container_inspect(NGINX_CONTAINER)
    connected_networks = {}
    if nginx_info:
        connected_networks = nginx_info.get("NetworkSettings", {}).get("Networks", {})

    # (b) List nginx conf files
    conf_files = sorted(Path(GENERATED_DIR).glob("*.nginx.conf"))
    conf_file_names = [f.name for f in conf_files]

    # (c) Parse upstreams from each conf file
    upstreams = []
    for cf in conf_files:
        try:
            content = cf.read_text()
            server_name = None
            proxy_pass = None
            m = re.search(r"server_name\s+([^;]+);", content)
            if m:
                server_name = m.group(1).strip().split()[0]
            m = re.search(r"proxy_pass\s+(https?://[^;]+);", content)
            if m:
                proxy_pass = m.group(1).strip()
            upstreams.append({
                "conf_file": cf.name,
                "server_name": server_name,
                "proxy_pass": proxy_pass,
            })
        except Exception:
            pass

    return {
        "nginx_container": NGINX_CONTAINER,
        "connected_networks": list(connected_networks.keys()),
        "conf_files": conf_file_names,
        "upstreams": upstreams,
    }


# ---------------------------------------------------------------------------
# P4: POST /nginx/reconnect-all  — reconnect nginx to all user networks
# ---------------------------------------------------------------------------

@app.post("/nginx/reconnect-all")
def reconnect_all() -> dict[str, Any]:
    """Reconnect provision-nginx to every network in the registry, then reload.

    ONE implementation with the recreate watcher (`watch_for_recreates`) — the
    operator's manual repair and the automatic one must be the same rule, or the
    two drift and only one of them is ever right.
    """
    return reconciliation.repair_nginx_attachments(NGINX_CONTAINER)


# ---------------------------------------------------------------------------
# POST /nginx/regenerate  — v3→v4 stale-conf migration + nginx restart
# ---------------------------------------------------------------------------

@app.post("/nginx/regenerate")
def regenerate_nginx() -> dict[str, Any]:
    """Re-render all registered per-service nginx confs with the v5 renderer,
    surgically strip any leftover ``$is_browser`` / v4 ACL scaffold, then
    restart nginx.

    v5 (decision 16): regeneration always produces the simple, ACL-free
    per-service conf (§5) — there is no v4-scaffold compatibility path.
    Recovery path for a crash-looping nginx: stale v3/v4 confs are rewritten
    in place (no Docker state change) and nginx is restarted to load them.
    """
    try:
        report = reconciliation.regenerate_nginx_confs()
    except Exception as e:
        raise HTTPException(500, f"Conf regeneration failed: {e}")
    docker_ops.nginx_restart(NGINX_CONTAINER)
    return {
        "message": "nginx confs regenerated; nginx restarted.",
        "report": report,
    }


# ---------------------------------------------------------------------------
# Reconciliation — live nginx state (derived from user_registry.yml + Docker)
# ---------------------------------------------------------------------------

@app.post("/reconcile")
def trigger_reconciliation() -> dict[str, Any]:
    """Run a live nginx upstream reconciliation pass.

    Reads all *.nginx.conf files, verifies each upstream container is
    running, reconnects nginx to every network in the registry, and
    reloads nginx.  Returns a live report — nothing is persisted to disk.
    """
    try:
        report = reconciliation.run_reconciliation()
    except Exception as e:
        raise HTTPException(500, f"Reconciliation failed: {e}")
    return {"message": "Reconciliation completed.", "report": report}


@app.get("/reconcile/status")
def reconciliation_status() -> dict[str, Any]:
    """Get a live snapshot of nginx network/upstream state.

    Derived from user_registry.yml + live Docker queries — no cached state file.
    """
    return reconciliation.get_nginx_state()


@app.get("/nginx-state")
def get_nginx_state() -> dict[str, Any]:
    """Get a live snapshot of nginx network/upstream state."""
    return reconciliation.get_nginx_state()


# ---------------------------------------------------------------------------
# P6: GET /users/{user}/{service}/{label}/containers/{container}/logs
# ---------------------------------------------------------------------------

@app.get("/users/{user_name}/services/{service_name}/{label}/containers/{container}/logs")
def get_container_logs(
    user_name: str, service_name: str, label: str, container: str,
    tail: int = Query(100, description="Number of log lines to return"),
) -> dict[str, Any]:
    """Get container logs for a specific container."""
    entry = registry.get_user_service(user_name, service_name, label)
    if not entry:
        raise HTTPException(404, f"No registration found for {user_name}/{service_name}/{label}.")
    
    prefix = template_engine.container_prefix(service_name, user_name, label)
    full_container_name = f"{prefix}{container}"
    
    # Verify container exists
    if not docker_ops.container_exists(full_container_name):
        raise HTTPException(404, f"Container not found: {full_container_name}")
    
    logs = docker_ops.container_logs(full_container_name, tail=tail)
    return {
        "container": full_container_name,
        "tail": tail,
        "logs": logs.splitlines(),
    }


# ---------------------------------------------------------------------------
# GET /tasks  — list all tasks in the pool
# ---------------------------------------------------------------------------

@app.get("/tasks")
def list_tasks() -> dict[str, Any]:
    tasks = task_manager.list_all()
    return {
        "count": len(tasks),
        "tasks": tasks,
    }


# ---------------------------------------------------------------------------
# GET /tasks/{task_id}  — query async task status
# ---------------------------------------------------------------------------

@app.get("/tasks/{task_id}")
def get_task_status(task_id: str) -> dict[str, Any]:
    task = task_manager.get(task_id)
    if task is None:
        raise HTTPException(404, f"Task not found: {task_id}")
    return task


# ---------------------------------------------------------------------------
# DELETE /tasks/{task_id}  — cancel a pending or running task
# ---------------------------------------------------------------------------

@app.delete("/tasks/{task_id}")
def cancel_task(task_id: str) -> dict[str, Any]:
    cancelled = task_manager.cancel(task_id)
    if not cancelled:
        task = task_manager.get(task_id)
        if task is None:
            raise HTTPException(404, f"Task not found: {task_id}")
        raise HTTPException(409, f"Task already in terminal state: {task['status']}")
    return {"task_id": task_id, "status": "cancelled"}


# ---------------------------------------------------------------------------
# Custom-task endpoints — external processes (gateway generation sessions)
# create a Tasks-page record and append session log lines (design §2).
# ---------------------------------------------------------------------------

@app.get("/tasks/{task_id}/logfile")
def get_task_log_file(task_id: str) -> dict[str, Any]:
    """The task's log, WHOLE (llm-based-fix-todo.md §1).

    The `LLM based fix` job feeds the failed task's log to the generation
    pipeline in full — not truncated, not summarised — and the gateway needs it
    as one string, not as an SSE stream. 404 when the task (or its log) is gone,
    which is exactly the condition the card renders as "task log is gone".
    """
    task = task_manager.get(task_id)
    if task is None:
        raise HTTPException(404, f"Task not found: {task_id}")
    if task.get("deleted_at"):
        raise HTTPException(410, f"Task was pruned; its log is gone: {task_id}")
    log_file = task_manager.get_log_file(task_id)
    if not log_file or not Path(log_file).exists():
        # A live row with a missing log is 404 — never another task's global log.
        raise HTTPException(404, f"Task log is gone: {task_id}")
    try:
        text = Path(log_file).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise HTTPException(404, f"Task log is gone: {task_id}") from e
    return {
        "task_id": task_id,
        "status": task.get("status"),
        "target": task.get("target") or {},
        "log": text,
        "bytes": len(text),
    }


@app.get("/tasks/{task_id}/log")
async def stream_task_log(
    task_id: str,
    tail: int = Query(200, description="Number of recent lines to send first"),
    follow: bool = Query(True, description="Keep streaming new lines"),
):
    """Stream per-task build log via Server-Sent Events.

    Streams from the task's dedicated log file (``task-{task_id}.log``)
    under ``TASK_LOG_DIR``.  Falls back to the global ``DOCKER_OPS_LOG``
    if the per-task log does not exist.
    """
    import asyncio
    from pathlib import Path

    task = task_manager.get(task_id)
    if task is None:
        raise HTTPException(404, f"Task not found: {task_id}")
    if task.get("deleted_at"):
        raise HTTPException(410, f"Task was pruned; its log is gone: {task_id}")
    task_log = task_manager.get_log_file(task_id)
    if not task_log or not Path(task_log).exists():
        raise HTTPException(404, f"Task log is gone: {task_id}")
    log_file = Path(task_log)

    async def log_generator():
        # Send initial tail
        if log_file.exists():
            try:
                lines = log_file.read_text().splitlines()
                recent = lines[-tail:] if len(lines) > tail else lines
                for line in recent:
                    yield f"data: {line}\n\n"
            except Exception:
                pass

        if not follow:
            yield "event: done\ndata: {}\n\n"
            return

        # Poll for new lines
        last_size = log_file.stat().st_size if log_file.exists() else 0
        while True:
            await asyncio.sleep(1)
            try:
                if log_file.exists():
                    current_size = log_file.stat().st_size
                    if current_size > last_size:
                        with open(log_file, "r") as f:
                            f.seek(last_size)
                            new_data = f.read()
                            for line in new_data.splitlines():
                                if line.strip():
                                    yield f"data: {line}\n\n"
                        last_size = current_size
            except Exception:
                break

    return StreamingResponse(
        log_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Service deployment files readiness check
# ---------------------------------------------------------------------------

class CheckMissingFilesResponse(BaseModel):
    service_name: str
    project_root: str | None = None
    ready: bool
    missing: list[str]
    existing: list[str]
    needs_env: bool = False


def _is_compose_name(name: str) -> bool:
    """True when ``name`` is a compose file by the platform's naming convention.

    The convention — not "any YAML" — is what identifies a compose. Counting
    unrelated YAML (a CI config, a linter config, a codecov config) as a compose
    is not harmless: this presence scan then reports compose as PRESENT and the
    deploy skips compose generation, while the gateway's candidate scan
    (correctly) offers no compose to select — leaving nothing to deploy.
    Mirrors ``file_sets._is_compose_name`` in the gateway; the two MUST agree.
    A ``.j2`` template counts, since a recipe shipping a compose template does
    have a compose for presence purposes.
    """
    if name.endswith(".j2"):
        name = name[:-3]
    return (name.startswith("docker-compose") or name.startswith("compose")) and (
        name.endswith(".yml") or name.endswith(".yaml")
    )


@app.get("/services/{service_name}/check-missing-files")
def check_missing_files(
    service_name: str,
    recipe_path: str = Query("", description="Recipe subdirectory path for multi-recipe projects"),
) -> CheckMissingFilesResponse:
    """Check which essential deployment files are missing for a service.

    Essential files for deployment:
      - docker-compose.yml (or .yml.j2 template)
      - nginx.conf (or .conf.j2 template)
      - .env (recommended, but not strictly required)
      - Dockerfile (or build context)

    Returns a list of missing file types so the gateway can offer
    LLM-based generation or manual upload before deployment.

    Args:
        recipe_path: Optional subdirectory for multi-recipe projects.
    """
    project_dir = SOURCE_PROJECTS_DIR / service_name
    if recipe_path:
        project_dir = project_dir / recipe_path
    if not project_dir.is_dir():
        raise HTTPException(404, f"Service '{service_name}' recipe '{recipe_path}' not found" if recipe_path else f"Service '{service_name}' not found")

    files = [f.name for f in project_dir.iterdir() if f.is_file()]

    has_compose = any(_is_compose_name(f) for f in files)
    # The serving-ports artifact (task-doc R1.1 / GRILLING G20): a recipe without
    # one cannot deploy, so this scan reports it missing exactly as it does a
    # compose. Same naming convention as the gateway's candidate scan — the two
    # are a MIRROR (separate services), and if they disagree the deploy has a
    # phase that never runs and no file to deploy.
    has_serving_ports = any(
        (f.startswith("serving_ports") or f.startswith("serving-ports"))
        and (f.endswith(".yml") or f.endswith(".yaml"))
        and not f.endswith(".generated") and not f.endswith(".j2")
        for f in files
    )
    has_nginx = any(
        f.endswith(".conf.j2") or
        (f.endswith(".conf") and not f.endswith(".j2"))
        for f in files
    )
    has_dockerfile = any(
        f == "Dockerfile" or f.lower().endswith("dockerfile")
        for f in files
    )
    has_env = any(
        f == ".env" for f in files
    )

    # needs_env signal (design §Env story L156-159): any ${VAR} interpolation
    # in the recipe's compose files (union scan — the gateway post-processes
    # this with the actual selection).  Fixes the unconditional .env
    # missing-flag: .env is only missing when interpolation is actually used.
    from lib.var_scan import needs_env as _needs_env
    _needs_env_scan = False
    for f in files:
        if f.endswith((".yml", ".yaml", ".yml.j2", ".yaml.j2")):
            try:
                if _needs_env((project_dir / f).read_text()):
                    _needs_env_scan = True
                    break
            except OSError:
                continue

    missing: list[str] = []
    existing: list[str] = []

    if has_compose:
        existing.append("docker-compose")
    else:
        missing.append("docker-compose")

    if has_serving_ports:
        existing.append("serving_ports")
    else:
        missing.append("serving_ports")

    if has_nginx:
        existing.append("nginx.conf")
    else:
        missing.append("nginx.conf")

    # Dockerfile is required ONLY when compose is missing (it is the build
    # context for compose generation — design §8.1); when compose exists it is
    # NOT flagged as missing.
    if has_dockerfile:
        existing.append("Dockerfile")
    elif not has_compose:
        missing.append("Dockerfile")

    # The recipe-level .env is no longer a generation target — per-user env is
    # generated at deploy time (design §2/§4). It is never listed as missing;
    # needs_env remains reported for the gateway's own use.
    if has_env:
        existing.append(".env")

    return CheckMissingFilesResponse(
        service_name=service_name,
        project_root=str(project_dir),
        ready=len(missing) == 0,
        missing=missing,
        existing=existing,
        needs_env=_needs_env_scan,
    )


# ---------------------------------------------------------------------------
# GET /service-url-base — pre-deploy service URL host (serving-URL contract)
# ---------------------------------------------------------------------------

@app.get("/service-url-base")
def service_url_base(
    service_name: str = Query(...),
    user_name: str = Query(...),
    label: str = Query("0"),
    domain: str = Query("localhost"),
    scheme: str = Query("http"),
) -> dict[str, str]:
    """Return the instance IDENTITY host and the canonical endpoint pattern.

    R14 — the returned ``hostname`` is the instance's identity (registry key,
    container prefix, canonical-name stem); it is **not a route**.  Every HTTP
    endpoint is ``<hostname_stem>--<sub_service>.<domain>``, so callers that
    need a reachable name use ``served_name_pattern`` (or the serving-ports
    artifact's own rows), never the bare host.

    The identity is still what the deploy path uses as the stem
    (:func:`template_engine.service_hostname`), so the two cannot drift.
    """
    host = template_engine.service_hostname(service_name, user_name, label, domain)
    stem = f"{service_name}-{user_name}-{label}"
    return {
        "hostname": host,
        "hostname_stem": stem,
        "served_name_pattern": f"{stem}--<sub_service>.{domain}",
        "scheme": scheme,
        "domain": domain,
    }


# ---------------------------------------------------------------------------
# GET /services/{service_name}/compose/preview — lightweight convert preview
# (design §Implementation notes L284-286)
# ---------------------------------------------------------------------------

def _preview_volume_paths(
    service_name: str, user_name: str, label: str, keys: list[str]
) -> dict[str, dict[str, str]]:
    """The golden ``volume_paths`` for an identity, or nothing without one (G60).

    "Before an identity exists it gets the keys only" — no user/label means no
    path, not a guessed one.
    """
    if not (service_name and user_name and label):
        return {}
    return {
        "volume_paths": {
            key: template_engine.volume_path(
                USER_DATA_DIR, user_name, service_name, str(label), key
            )
            for key in keys
        }
    }


def _active_compose_services(data: dict, profiles: list[str] | None) -> dict[str, Any]:
    """The compose rule: no non-empty profiles, or one active; `""` ungated."""
    services = (data or {}).get("services") or {}
    if not isinstance(services, dict):
        return {}
    active = {str(p) for p in (profiles or []) if p}
    out: dict[str, Any] = {}
    for name, svc in services.items():
        if not isinstance(svc, dict):
            out[name] = svc
            continue
        profs = [str(p) for p in (svc.get("profiles") or [])]
        # Compose's REAL rule (measured 2026-10-24): `profiles: [""]` is the
        # DEFAULT-STACK marker — it runs only with NO active profile, and is
        # excluded as soon as one is. No key at all → always runs.
        if not profs:
            out[name] = svc
        elif active:
            if any(p in active for p in profs if p):
                out[name] = svc
        elif "" in profs:
            out[name] = svc
    return out


def _volume_rows(
    data: dict, src_to_key: dict[str, str], profiles: list[str] | None
) -> list[dict[str, str]]:
    """The mount rows of the ACTIVE services: ``{key, service, target}``.

    The profile filter lives here because the preview is the panel's only source
    of the table: showing `falkordb_data` on a no-profile deploy was wrong.
    """
    rows: list[dict[str, str]] = []
    for svc_name, svc in _active_compose_services(data, profiles).items():
        if not isinstance(svc, dict):
            continue
        for v in svc.get("volumes") or []:
            if isinstance(v, str) and ":" in v:
                src, rest = v.split(":", 1)
                target = rest.split(":", 1)[0]
            elif isinstance(v, dict):
                src = v.get("source")
                target = v.get("target")
            else:
                continue
            key = src_to_key.get(src)
            if key and target:
                rows.append({"key": key, "service": str(svc_name), "target": target})
    return rows


def _volume_targets(data: dict, src_to_key: dict[str, str]) -> dict[str, str]:
    """``{volume key: container target}`` from the compose, first appearance.

    The panel shows a real MOUNT TABLE (host path → container path); the target
    is a fact of the compose and the -api remains its only source. A key used
    by several services/targets keeps the first target (the panel row is one
    per key; a duplicated target is a compose the operator wrote, not an
    override the panel can express).
    """
    out: dict[str, str] = {}
    services = (data or {}).get("services") or {}
    if not isinstance(services, dict):
        return out
    for svc in services.values():
        if not isinstance(svc, dict):
            continue
        for v in svc.get("volumes") or []:
            if isinstance(v, str) and ":" in v:
                src, rest = v.split(":", 1)
                target = rest.split(":", 1)[0]
            elif isinstance(v, dict):
                src = v.get("source")
                target = v.get("target")
            else:
                continue
            key = src_to_key.get(src)
            if key and target and key not in out:
                out[key] = target
    return out


def _preview_volume_mapping(
    project_dir: Path,
    compose_files: list[str],
    *,
    service_name: str = "",
    user_name: str = "",
    label: str = "",
    profiles: list[str] | None = None,
) -> dict[str, Any]:
    """Run the converter in-call on compose file(s) and return the src→key map.

    Pure preview — NO files are written and no templates are produced.  This
    is the converter's in-call src→key mapping exposed via a lightweight
    convert/preview response, so the deploy panel's advanced volume-override
    rows obtain their keys from the mapping instead of parsing ``.j2`` files
    in the frontend (design §Implementation notes L284-286).

    - Single file: direct conversion path unchanged (F15) — ``convert()``
      in memory.
    - ≥2 files: ordered merge via :func:`lib.compose_merge.merge_compose_data`
      (same flags as the deploy-time path, no artifact written), then convert.
    - A ``.j2`` input is first resolved to its plain source sibling (e.g.
      ``docker-compose.yml.j2`` → ``docker-compose.yml``); when the source is
      gone the template's ``volumes['key']`` tokens are extracted server-side
      as a fallback (the *frontend* never parses templates).

    Returns
    -------
    dict with ``compose_files`` (resolved list), ``src_to_key``
    (source path → volume key), ``volume_keys`` (ordered keys, first-appearance)
    and — when an identity is supplied — ``volume_paths`` (key → the golden
    per-user host path).

    G60 (D8): the -api is the ONLY source of the volume paths. The formula is the
    pure helper in ``template_engine.volume_path``, shared with the provisioner
    that creates the directories; this preview still creates nothing.
    """
    import re as _re
    import yaml as _yaml
    from lib.compose_converter import convert as _convert

    sources: list[str] = []
    templates: list[str] = []
    for entry in compose_files:
        p = Path(entry)
        if p.is_absolute():
            raise HTTPException(422, f"compose preview path must be project-relative: {entry}")
        target = project_dir / p
        if entry.endswith(".j2"):
            # Prefer the plain source sibling; only the template itself
            # (no source left) is kept and handled by the token fallback.
            source = target.with_name(target.name[: -len(".j2")])
            if source.exists():
                target = source
        if not target.exists():
            raise HTTPException(404, f"compose file not found: {entry}")
        rel = str(target.relative_to(project_dir))
        (sources if not rel.endswith(".j2") else templates).append(rel)

    src_to_key: dict[str, str] = {}
    used: list[str] = []

    def _add(mapping: dict[str, str]) -> None:
        for src, key in mapping.items():
            if key not in used:
                used.append(key)
            src_to_key[src] = key

    if templates:
        if sources:
            # Mixing source + template inputs is ambiguous — reject.
            raise HTTPException(
                422, f"cannot mix source compose files and .j2 templates: {templates}"
            )
        # Template-only fallback: extract the keys the converter would assign.
        content = (project_dir / Path(templates[0])).read_text()
        seen: list[str] = []
        for m in _re.finditer(
            r"\{\{-?\s*volumes\[['\"]([^'\"]+)['\"]\]\s*-?\}\}", content
        ):
            if m.group(1) not in seen:
                seen.append(m.group(1))
        # Named-volumes block fallback (server-side, mirrors the old behavior).
        named = _re.search(r"^volumes:\s*\n((?:\s+\w+:\s*\n)+)", content, _re.M)
        if named:
            for nv in _re.findall(r"^\s+(\w+):\s*$", named.group(1), _re.M):
                if nv not in seen:
                    seen.append(nv)
        return {
            "compose_files": templates,
            "src_to_key": {k: k for k in seen},
            "volume_keys": seen,
            # A template-only recipe has no parsed compose to read targets or
            # services from.
            "volume_targets": {},
            "volume_services": {},
            "volume_rows": [],
            **_preview_volume_paths(service_name, user_name, label, seen),
        }

    if not sources:
        raise HTTPException(422, "compose_files is required for the preview")

    if len(sources) == 1:
        with (project_dir / Path(sources[0])).open() as f:
            data = _yaml.safe_load(f)
        if not isinstance(data, dict) or "services" not in data:
            raise HTTPException(
                422, f"'{sources[0]}' does not look like a docker-compose file "
                "(missing 'services:' key)"
            )
        _transformed, src_to_key, _tokens, _env_to_key = _convert(data)
    else:
        from lib.compose_merge import merge_compose_data
        merged = merge_compose_data([str(project_dir / Path(p)) for p in sources])
        _transformed, src_to_key, _tokens, _env_to_key = _convert(merged)

    _add(src_to_key)
    _data = data if len(sources) == 1 else merged
    rows = _volume_rows(_data, src_to_key, profiles)
    active_keys = []
    for r in rows:
        if r["key"] not in active_keys:
            active_keys.append(r["key"])
    services: dict[str, str] = {}
    targets: dict[str, str] = {}
    for r in rows:
        services.setdefault(r["key"], r["service"])
        targets.setdefault(r["key"], r["target"])
    return {
        "compose_files": sources,
        "src_to_key": src_to_key,
        # Only the ACTIVE services' volumes: the selected profiles decide.
        "volume_keys": [k for k in used if k in active_keys],
        "volume_targets": {k: targets[k] for k in active_keys},
        "volume_services": {k: services[k] for k in active_keys},
        "volume_rows": rows,
        **_preview_volume_paths(service_name, user_name, label, active_keys),
    }


@app.get("/services/{service_name}/compose/preview")
def compose_preview(
    service_name: str,
    compose_files: list[str] = Query(default=[], description="Recipe-relative compose file paths (ordered)"),
    recipe_path: str = Query("", description="Recipe subdirectory path"),
    user_name: str = Query("", description="Target user, for the golden volume paths"),
    label: str = Query("", description="Target label, for the golden volume paths"),
    profiles: list[str] = Query(default=[], description="Active compose profiles"),
) -> dict[str, Any]:
    """Lightweight convert/preview: converter in-call src→key mapping.

    Runs the converter on the given compose file(s) IN-CALL and returns the
    bind-mount source→volume-key mapping — no template is written.  The
    deploy panel's advanced volume-override rows consume ``volume_keys``
    from this response instead of parsing ``.j2`` files in the frontend
    (design §Implementation notes L284-286).

    Args:
        compose_files: Ordered recipe-relative compose paths (the same set
            that would be passed to register as ``compose_file_paths``).
        recipe_path: Optional subdirectory for multi-recipe projects.
    """
    project_dir = SOURCE_PROJECTS_DIR / service_name
    if recipe_path:
        project_dir = project_dir / recipe_path
    if not project_dir.is_dir():
        raise HTTPException(
            404,
            f"Service '{service_name}' recipe '{recipe_path}' not found"
            if recipe_path
            else f"Service '{service_name}' not found",
        )
    return _preview_volume_mapping(
        project_dir,
        compose_files,
        service_name=service_name,
        user_name=user_name,
        label=label,
        profiles=profiles,
    )


# ---------------------------------------------------------------------------
# GET /users  — all users status
# ---------------------------------------------------------------------------

@app.get("/users")
def get_all_users_status() -> dict[str, Any]:
    return _compute_status(None)


# ---------------------------------------------------------------------------
# GET /users/{user_name}  — single user status
# ---------------------------------------------------------------------------

@app.get("/users/{user_name}")
def get_user_status(user_name: str) -> dict[str, Any]:
    entries = registry.get_user(user_name)
    if not entries:
        raise HTTPException(404, f"No registrations found for user '{user_name}'.")
    return _compute_status(user_name)


# ---------------------------------------------------------------------------
# Nginx proxy_pass validation
# ---------------------------------------------------------------------------

def _validate_nginx_proxy_targets(
    nginx_conf: Path, compose_service_names: list[str]
) -> None:
    """Validate that every proxy_pass host in *nginx_conf* is a compose service.

    Raises :class:`HTTPException` (422) with a message listing any hosts
    that don't match a known compose service name.
    """
    import re
    content = nginx_conf.read_text()
    hosts: list[str] = []
    for m in re.finditer(r'proxy_pass\s+https?://([a-zA-Z0-9_-]+)', content):
        hosts.append(m.group(1))

    compose_set = {n.lower() for n in compose_service_names}
    unknown = [h for h in hosts if h.lower() not in compose_set]

    if unknown:
        raise HTTPException(
            422,
            f"nginx conf proxy_pass target(s) do not match any compose service name. "
            f"Compose services: {sorted(compose_service_names)}. "
            f"Unknown proxy_pass host(s): {sorted(unknown)}. "
            f"Update {nginx_conf.name} so every proxy_pass host matches a "
            f"service key from docker-compose.yml.",
        )


# ---------------------------------------------------------------------------
# POST /nginx/validate  — generated-nginx validation surface (design §Impl
# notes L259-261, GAP-20).  The gateway calls this after generating/reviewing
# an nginx conf; the service-name set is the MERGED compose service set
# (including profile-gated services) — activation awareness is the LLM's job.
# ---------------------------------------------------------------------------

class NginxValidateRequest(BaseModel):
    nginx_conf_path: str
    compose_service_names: list[str] = []


class NginxValidateResponse(BaseModel):
    valid: bool
    errors: list[str] = []


@app.post("/nginx/validate")
def validate_nginx_conf(req: NginxValidateRequest) -> NginxValidateResponse:
    conf = Path(req.nginx_conf_path)
    if not conf.is_file():
        raise HTTPException(404, f"nginx_conf_path not found: {conf}")
    errors: list[str] = []
    try:
        _validate_nginx_proxy_targets(conf, req.compose_service_names)
    except HTTPException as e:
        errors.append(str(e.detail))
    return NginxValidateResponse(valid=not errors, errors=errors)


# ---------------------------------------------------------------------------
# Status computation (shared logic)
# ---------------------------------------------------------------------------

def _compute_status(filter_user: str | None) -> dict[str, Any]:
    # docker_ps_all: include exited-0 one-shots (e.g. dify init_permissions) so
    # they are not miscounted as "down"/unhealthy (design §9). _status_for_user
    # treats "Exited (0)" as healthy and any other stopped state as unhealthy.
    running = {c["name"]: c["status"] for c in docker_ops.docker_ps_all()}
    all_users = registry.get_all_users()

    if filter_user:
        user_names = list({u["user_name"] for u in all_users if u.get("user_name") == filter_user})
    else:
        user_names = list({u["user_name"] for u in all_users})

    return {"user_status": [_status_for_user(name, running) for name in sorted(user_names)]}


_compose_parse_cache: dict[str, tuple[int, dict | None]] = {}
_compose_cache_lock = threading.Lock()
_compose_monitor_started = False


def _parse_compose(path: str) -> tuple[int, dict | None]:
    try:
        m = Path(path).stat().st_mtime_ns
    except OSError:
        return 0, None
    try:
        import yaml
        with open(path) as f:
            data = yaml.safe_load(f)
    except Exception:
        data = None
    return m, data


def _compose_monitor_loop() -> None:
    """Watch known rendered-compose files; reload only when they change."""
    while True:
        time.sleep(1.0)
        with _compose_cache_lock:
            paths = list(_compose_parse_cache)
        for p in paths:
            try:
                m = Path(p).stat().st_mtime_ns
            except OSError:
                continue
            with _compose_cache_lock:
                hit = _compose_parse_cache.get(p)
                if hit is None or hit[0] == m:
                    continue
                _compose_parse_cache[p] = _parse_compose(p)


def _compose_services(path: str) -> dict | None:
    """Return a cached parse of a rendered compose. Files are monitored and
    reloaded ONLY when they change — reads never re-yaml-parse (per-poll
    re-parsing saturated the -api CPU)."""
    global _compose_monitor_started
    with _compose_cache_lock:
        hit = _compose_parse_cache.get(path)
        if hit is not None:
            return hit[1]
        _compose_parse_cache[path] = _parse_compose(path)
    if not _compose_monitor_started:
        _compose_monitor_started = True
        threading.Thread(target=_compose_monitor_loop, daemon=True, name="compose-watch").start()
    return _compose_parse_cache[path][1]


def _expected_container_names(entry: dict) -> list[str]:
    """Return the full container names for a service — from registry or compose file.

    Profile-filtered (design §9): services whose compose ``profiles:`` list does
    not intersect the entry's recorded (activated) ``profiles`` are NOT
    expected — with no profile activated, only unprofiled services are
    expected to run.

    An EMPTY-STRING profile (``profiles: [""]``, the idiom graphiti uses) counts
    as *ungated*, which is what compose does in practice: the service starts
    with no profile activated. Treating it as gated made ``active_svcs`` empty
    while compose had in fact started the containers, so nothing was expected,
    no container was probed, and the card collapsed to "unknown".
    """
    compose_file = entry.get("compose_file_path", "")
    active_profiles = set(entry.get("profiles") or [])
    active_svcs: set[str] | None = None
    if compose_file and Path(compose_file).exists():
        data = _compose_services(compose_file) or {}
        services = data.get("services", {}) or {}
        # Compose's REAL rule (measured 2026-10-24):
        #   no profiles key → always runs; `profiles: [""]` → runs only with NO
        #   active profile; a named profile → runs only when active.
        def _is_active(svc: Any) -> bool:
            if not isinstance(svc, dict):
                return True
            profs = [str(p) for p in (svc.get("profiles") or [])]
            if not profs:
                return True
            if active_profiles:
                return any(p in active_profiles for p in profs if p)
            return "" in profs

        active_svcs = {name for name, svc in services.items() if _is_active(svc)}

    stored = entry.get("container_names")
    if stored:
        if active_svcs is None:
            return stored
        prefix = template_engine.container_prefix(
            entry["service_name"], entry["user_name"], entry["label"]
        )
        return [c for c in stored if c[len(prefix):] in active_svcs]

    # Fallback: derive from compose file (backward compat with pre-container_names entries)
    if active_svcs is not None:
        prefix = template_engine.container_prefix(
            entry["service_name"], entry["user_name"], entry["label"]
        )
        return [f"{prefix}{k}" for k in sorted(active_svcs)]
    return []


def _has_auth(entry: dict[str, Any]) -> bool:
    """Whether the instance's rendered HTTP conf actually carries auth_basic.

    The card's public/unguarded warning must describe the CONFIG, not merely
    whether a plaintext password happens to still be recorded. A lost
    ``passwd_plain`` does not remove a rendered ``auth_basic`` directive, and a
    passwd-less deployment has none. Read the rendered conf first and fall back
    to the recorded credential only when the file is unavailable.
    """
    paths: list[str] = []
    for key in ("nginx_conf_path",):
        v = entry.get(key)
        if isinstance(v, str) and v:
            paths.append(v)
    for v in (entry.get("nginx_conf_paths") or []):
        if isinstance(v, str) and v:
            paths.append(v)
    read_any = False
    for p in paths:
        try:
            text = Path(p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        read_any = True
        if re.search(r'^[ \t]*auth_basic[ \t]+', text, re.MULTILINE):
            return True
    if read_any:
        return False
    return bool(entry.get("passwd_plain"))


def _recipe_path_for_entry(entry: dict[str, Any]) -> str:
    """Recipe subdirectory of one instance, relative to its project dir.

    ``''`` = the project-root recipe; otherwise the recipe dir (e.g. ``docker``
    for dify). Derived from the recorded per-user file paths so the dashboard's
    Deployment-Files links resolve to the SAME directory the deploy wrote into
    (single source of truth = the registry entry).
    """
    service_name = entry.get("service_name") or ""
    try:
        project_dir = (SOURCE_PROJECTS_DIR / service_name).resolve()
    except OSError:
        return ""
    candidates: list[str] = []
    for key in ("compose_file_path", "compose_template_path", "nginx_conf_path", "env_file_path"):
        v = entry.get(key)
        if isinstance(v, str) and v:
            candidates.append(v)
    for v in (entry.get("compose_file_paths") or []):
        if isinstance(v, str) and v:
            candidates.append(v)
    for c in candidates:
        try:
            rel = Path(c).resolve().parent.relative_to(project_dir)
        except (ValueError, OSError):
            continue
        return "" if str(rel) == "." else rel.as_posix()
    return ""


def _fix_available(task_id: str) -> bool:
    """Whether `LLM based fix` can run for an entry (llm-based-fix-todo.md §5).

    True only while the entry's task still resolves AND its log file is present.
    Task records are TTL-pruned, so once the log is gone the fix cannot be fed
    the failure — the card disables the button and says so instead. The log is
    deliberately NOT persisted anywhere else to make the fix durable.
    """
    if not task_id:
        return False
    try:
        log_file = task_manager.get_log_file(task_id)
    except Exception:
        return False
    return bool(log_file) and Path(log_file).exists()


def _task_available(task_id: str) -> bool:
    """Whether the entry's associated task record still resolves.

    This is what makes the card's `Task: <id>` link **clickable while the record
    exists and greyed once it is gone** (GRILLING G24/G34). Task records leave by
    the cleanup contract (G30: 7-day TTL / 1000 cap) and are then simply absent —
    ids are never reused — so "can this link resolve?" is a fact only this side
    knows, and the dashboard reads it off the status payload instead of probing
    once per card. Same shape as `fix_available`, deliberately: a card must not
    have to guess a second time about the same record.
    """
    if not task_id:
        return False
    try:
        return task_manager.get(task_id) is not None
    except Exception:
        return False


def _active_volume_keys(entry: dict[str, Any]) -> set[str] | None:
    """The volume keys the ACTIVE compose services actually mount.

    ``None`` when the rendered compose cannot be read (callers then show the
    full recorded map rather than hiding something on a guess). G61's rule for
    containers applies to volumes too: a deploy with no active profile must not
    show a ``falkordb_data`` row for a service that was never started.
    """
    compose_file = entry.get("compose_file_path", "")
    if not compose_file or not Path(compose_file).exists():
        return None
    data = _compose_services(compose_file) or {}
    services = data.get("services", {}) or {}
    if not isinstance(services, dict):
        return None
    profiles = set(entry.get("profiles") or [])
    volumes = entry.get("volumes") or {}
    path_to_key = {str(v): k for k, v in volumes.items()}
    keys: set[str] = set()
    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        svc_profiles = [p for p in (svc.get("profiles") or []) if p]
        if svc_profiles and not any(p in profiles for p in svc_profiles):
            continue
        for v in svc.get("volumes") or []:
            if isinstance(v, str) and ":" in v:
                src = v.split(":", 1)[0]
            elif isinstance(v, dict):
                src = v.get("source")
            else:
                continue
            key = path_to_key.get(str(src))
            if key:
                keys.add(key)
    return keys


def _active_volumes(entry: dict[str, Any]) -> dict[str, str]:
    """The card's Volumes section: only the active services' mounts."""
    volumes = dict(entry.get("volumes") or {})
    keys = _active_volume_keys(entry)
    if keys is None:
        return volumes
    return {k: v for k, v in volumes.items() if k in keys}


def _status_for_user(user_name: str, running: dict[str, str]) -> dict[str, Any]:
    entries = registry.get_user(user_name)
    healthy_services, unhealthy_services, missing_services = [], [], []
    failed_services: list[dict[str, Any]] = []

    for entry in entries:
        # A NULL column reads back as None (dict.get's default only covers a
        # MISSING key), and `Path(None)` raises — a seeded/legacy row without a
        # rendered compose path must classify as `missing`, never 500 the whole
        # user-status read (T11 minimal-shape trace).
        compose_file = entry.get("compose_file_path") or ""
        expected_names = _expected_container_names(entry)

        healthy: dict[str, str] = {}
        unhealthy: dict[str, str] = {}
        missing: dict[str, str] = {}

        # R13/T5 — `Exited (0)` is a completed ONE-SHOT init when something else
        # of the instance is still up; when NOTHING is up, those same containers
        # are a STOPPED instance. Treating every Exited(0) as healthy made a
        # stopped instance read as running: the card kept offering "Stop" and
        # never "Start", and the stream probe had no way back through the UI.
        _any_up = any("up" in str(running.get(c, "")).lower() for c in expected_names)
        for cname in expected_names:
            if cname in running:
                status = running[cname]
                if "unhealthy" in status.lower():
                    unhealthy[cname] = status
                elif "up" in status.lower() or "healthy" in status.lower():
                    healthy[cname] = status
                elif "exited (0)" in status.lower() and _any_up:
                    # one-shot completed successfully beside a running service
                    healthy[cname] = status
                else:
                    unhealthy[cname] = status
            else:
                missing[cname] = "not running"

        svc: dict[str, Any] = {
            "service_id": entry["service_id"],
            "owner_api_user_id": entry["owner_api_user_id"],
            "service_name": entry["service_name"],
            "label": entry["label"],
            "compose_template_path": entry.get("compose_template_path", ""),
            "compose_file_path": compose_file,
            # The conf this instance's nginx was RENDERED FROM, beside the
            # rendered output: the rendered one lives in `generated/` (outside
            # every recipe), so the SOURCE template is the file in the recipe dir
            # the card can open and edit — the same choice the compose row makes.
            # nginx is one of the three deployed paths (G13) and this was the
            # only one the card could not show; the field was read from the
            # registry by the passthrough endpoints but never sent here.
            "nginx_conf_template_path": entry.get("nginx_conf_template_path", ""),
            "nginx_conf_path": entry.get("nginx_conf_path", ""),
            # R6.1: the sub-services this instance exposes a STREAM endpoint for,
            # as the canonical names its rendered stream conf selects on. The card
            # decides what to offer from THIS — the recorded state — never by
            # re-deriving "TLS is on and a row exists" in the panel. Empty (or
            # absent) means nothing is exposed, which is also the TLS-off case by
            # construction: nothing was recorded.
            # R1.5/R6.2 — the instance's OWN host, from the registry entry, which
            # is the single source of truth for it (`service_hostname`). The card
            # needs it to build every `/go/` link; without it the panel composed
            # the name itself and hard-coded `.localhost`, so on any other domain
            # the service URL — and each extra endpoint — pointed at a host that
            # does not exist.
            "hostname": entry.get("hostname") or "",
            # The public card's unguarded warning and the gateway's public URL
            # annotation both read these recorded facts. `has_auth` is true
            # only when the deploy recorded a plaintext Basic credential, which
            # is exactly the condition under which the rendered conf carries
            # auth_basic.
            "has_auth": _has_auth(entry),
            "tls": bool(entry.get("tls") if "tls" in entry else entry.get("https")),
            "stream_sub_services": entry.get("stream_sub_services") or [],
            # R1.5/R6.2 — the EXTRA http endpoints the instance serves
            # (`<instance host>--<sub_service>.<domain>`), read from the rendered
            # conf at deploy time — the same shape `stream_sub_services` uses.
            # The card's URL section keys its `/go/` rows by these; the primary is
            # not among them (it is the instance's own URL, keyed `primary`).
            "http_endpoints": entry.get("http_endpoints") or [],
            # Every conf the deploy carried (R9.2), beside the singular fields the
            # card reads today.
            "nginx_conf_paths": entry.get("nginx_conf_paths") or [],
            # Recipe subdir of THIS instance ('' = project root) + the effective
            # per-user env file — the dashboard's Deployment-Files links must
            # resolve inside the recipe dir, not the project root.
            "recipe_path": _recipe_path_for_entry(entry),
            "env_file_path": entry.get("env_file_path", ""),
            "container_names": expected_names,
            "healthy_containers": healthy,
            "unhealthy_containers": unhealthy,
            "missing_containers": missing,
            # G61 — a volume mounted only by an inactive, profile-gated service
            # is not part of this deployment; do not show it on the card.
            "volumes": _active_volumes(entry),
            "subnet": entry.get("subnet", ""),
            # The task this instance came from, whether its log still exists, and
            # whether its record still resolves — all three facts live here
            # (registry + tasks), so the dashboard keeps depending on the -api
            # alone (llm-based-fix-todo.md §4/§5). `task_available` drives the
            # card's greyed `Task:` link (G24/G34).
            "task_id": entry.get("task_id", ""),
            "fix_available": _fix_available(entry.get("task_id", "")),
            "task_available": _task_available(entry.get("task_id", "")),
        }
        # If subnet not in registry, try to discover from Docker (legacy)
        if not svc["subnet"]:
            net_name = entry.get("network_name", "")
            if net_name:
                discovered = subnet_manager.discover_subnet_from_docker(net_name)
                if discovered:
                    svc["subnet"] = discovered["subnet"]

        # Check if the service is currently being built
        svc["status"] = entry.get("status", "unknown")

        # A recorded failure is its OWN bucket (GRILLING G10 / doc §3): the
        # status comes from the registry, never from container health, so a
        # failed entry does not render as an ordinary "0 up, 1 down". The
        # bounded reason rides along for the card.
        if svc["status"] == "failed":
            svc["failure"] = entry.get("failure") or {}
            failed_services.append(svc)
        elif not Path(compose_file).exists():
            missing_services.append(svc)
        elif entry.get("status") == "building":
            # During build, containers don't exist yet — don't classify as missing/unhealthy
            unhealthy_services.append(svc)
        elif len(healthy) == len(expected_names) and not unhealthy and not missing:
            healthy_services.append(svc)
        elif not healthy and not unhealthy:
            missing_services.append(svc)
        else:
            unhealthy_services.append(svc)

    total = len(entries)
    return {
        "user_name": user_name,
        "summary": {
            "expected_services_#": total,
            "healthy_services_#": len(healthy_services),
            "unhealthy_services_#": len(unhealthy_services) + len(missing_services),
            "failed_services_#": len(failed_services),
        },
        "healthy_services": healthy_services,
        "unhealthy_services": unhealthy_services,
        "missing_services": missing_services,
        "failed_services": failed_services,
    }


# ---------------------------------------------------------------------------
# GET /container-stats  —  container-level statistics (registry only)
# ---------------------------------------------------------------------------

@app.get("/container-stats")
def get_container_stats() -> dict[str, Any]:
    """Return container statistics for **only** the containers referenced
    in ``user_registry.yml``.

    Categories
    ----------
    - healthy_running   — container is running (and health check OK if present)
    - unhealthy_running — container is running but health check reports unhealthy
    - restarting        — container is in restarting state
    - down              — container exists but is stopped / exited / paused
    - missing           — container does not exist at all

    The gateway dashboard uses this instead of running ``docker ps`` directly.
    """
    return _compute_container_stats()


def _compute_container_stats() -> dict[str, Any]:
    all_entries = registry.get_all_users()

    # Collect all expected container names from the registry
    expected: set[str] = set()
    for entry in all_entries:
        names = _expected_container_names(entry)
        expected.update(names)

    # Single docker ps -a call (NOT per-container inspect)
    all_containers = {c["name"]: c["status"] for c in docker_ops.docker_ps_all()}

    healthy_running = 0
    unhealthy_running = 0
    restarting = 0
    completed = 0
    down = 0
    missing = 0

    for cname in expected:
        status = all_containers.get(cname)
        if status is None:
            missing += 1
            continue

        status_lower = status.lower()
        if "restarting" in status_lower:
            restarting += 1
        elif "exited (0)" in status_lower:
            # One-shot init container completed successfully — NOT "down"
            # (feature 10; mirrors _status_for_user's exited-(0)-healthy rule).
            completed += 1
        elif "up" in status_lower:
            if "(unhealthy)" in status_lower:
                unhealthy_running += 1
            else:
                # "(healthy)", "(health: starting)", or no health check
                healthy_running += 1
        else:
            # "Exited", "Created", "Dead", "Paused", "Removing"
            down += 1

    return {
        "container_stats": {
            "healthy_running": healthy_running,
            "unhealthy_running": unhealthy_running,
            "restarting": restarting,
            "completed": completed,
            "down": down,
            "missing": missing,
            "total_expected": len(expected),
        }
    }


# ---------------------------------------------------------------------------
# GET /service-stats  —  service-level statistics (registry only)
# ---------------------------------------------------------------------------

@app.get("/service-stats")
def get_service_stats() -> dict[str, Any]:
    """Return service-level statistics computed from ``user_registry.yml`` only.

    Categories
    ----------
    - healthy   — all expected containers are running and healthy
    - unhealthy — at least one container is unhealthy, restarting, or down
    - expected  — total number of service instances in the registry

    The gateway dashboard uses this instead of running ``docker ps`` directly.
    """
    return _compute_service_stats()


def _compute_service_stats() -> dict[str, Any]:
    all_entries = registry.get_all_users()

    # Single docker ps -a call
    all_containers = {c["name"]: c["status"] for c in docker_ops.docker_ps_all()}

    healthy = 0
    unhealthy = 0

    for entry in all_entries:
        expected_names = _expected_container_names(entry)
        if not expected_names:
            unhealthy += 1
            continue

        # R13/T5 — the same rule as `_status_for_user`: Exited(0) is a completed
        # init only beside a running service; an instance with nothing up is
        # DOWN, not healthy.
        _any_up = any(
            "up" in str(all_containers.get(c, "")).lower() for c in expected_names
        )
        all_ok = True
        for cname in expected_names:
            status = all_containers.get(cname)
            if status is None:
                all_ok = False
                break
            status_lower = status.lower()
            if "restarting" in status_lower:
                all_ok = False
                break
            if "(unhealthy)" in status_lower:
                all_ok = False
                break
            # A completed one-shot init (Exited (0)) counts as healthy — only
            # genuinely stopped/errored states make the service unhealthy
            # (feature 10; mirrors _status_for_user).
            if ("up" not in status_lower) and not (
                "exited (0)" in status_lower and _any_up
            ):
                all_ok = False
                break

        if all_ok:
            healthy += 1
        else:
            unhealthy += 1

    return {
        "service_stats": {
            "healthy": healthy,
            "unhealthy": unhealthy,
            "expected": len(all_entries),
        }
    }


# ---------------------------------------------------------------------------
# GET /subnet-pool  —  subnet pool usage statistics
# ---------------------------------------------------------------------------

@app.get("/subnet-pool")
def get_subnet_pool() -> dict[str, Any]:
    """Return subnet pool usage statistics for the dashboard.

    Data is computed from environment (SUBNET_POOLS) and registry entries.
    When SUBNET_POOLS is not set, returns disabled state.
    """
    all_entries = registry.get_all_users()
    return subnet_manager.get_pool_stats(all_entries)


# ---------------------------------------------------------------------------
# GET /ssl-certs  —  list available SSL certificate domains
# ---------------------------------------------------------------------------

import datetime
import subprocess

class SSLCertUploadRequest(BaseModel):
    domain: str
    fullchain: str = ""    # PEM content (paste mode)
    privkey: str = ""      # PEM content (paste mode)
    ssl_path: str = ""     # Path to dir containing fullchain.pem + privkey.pem (path mode)


def _cert_dns_names(cert_pem: str) -> list[str]:
    """The DNS names a certificate claims: every SAN DNS entry + the CN.

    Parsed by openssl (the same tool `_get_cert_expiry` uses) on a temporary
    file, because openssl is what this image has and a hand-rolled DER parser is
    not worth its bugs. Returns [] when the certificate cannot be read — the
    caller treats that as "not a wildcard", which is the fail-closed direction.
    """
    # Imported HERE, like the file's other helpers: `re` and `subprocess` are not
    # module-level names in this module, and a missing import inside a
    # `try/except` that returns [] is invisible — it made this function answer
    # "no wildcard" for every certificate, including a real wildcard (measured).
    import re
    import subprocess
    import tempfile

    names: list[str] = []
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as fh:
            fh.write(cert_pem)
            tmp = fh.name
        try:
            out = subprocess.run(
                ["openssl", "x509", "-noout", "-subject", "-ext", "subjectAltName", "-in", tmp],
                capture_output=True, text=True, timeout=10,
            )
        finally:
            try:
                Path(tmp).unlink()
            except OSError:
                pass
        text = (out.stdout or "") + (out.stderr or "")
        for m in re.finditer(r"DNS:([^,\s]+)", text):
            names.append(m.group(1).strip())
        m = re.search(r"CN\s*=\s*([^,\n/]+)", text)
        if m:
            names.append(m.group(1).strip())
    except Exception:
        return []
    seen: list[str] = []
    for n in names:
        if n and n not in seen:
            seen.append(n)
    return seen


def _get_cert_expiry(cert_path: Path) -> tuple[str, int]:
    """Return (iso8601-date, days_until_expiry) for a certificate file.

    Returns ("unknown", -1) on any error.
    """
    try:
        cp = subprocess.run(
            ["openssl", "x509", "-enddate", "-noout", "-in", str(cert_path)],
            capture_output=True, text=True, timeout=10,
        )
        if cp.returncode != 0:
            return ("unknown", -1)
        # output format: "notAfter=Oct  5 14:12:06 2026 GMT"
        date_str = cp.stdout.strip().split("=", 1)[-1]
        end_dt = datetime.datetime.strptime(date_str, "%b %d %H:%M:%S %Y %Z")
        iso = end_dt.strftime("%Y-%m-%d")
        days = (end_dt - datetime.datetime.utcnow()).days
        return (iso, days)
    except Exception:
        return ("unknown", -1)


@app.get("/ssl-certs")
def list_ssl_certs() -> dict[str, Any]:
    """List available SSL certificate domains.

    Returns domain names that have both fullchain.pem and privkey.pem
    in ``SSL_DIR/{domain}/``, plus expiry info for each cert.
    """
    domains = []
    if SSL_DIR.exists():
        for d in sorted(SSL_DIR.iterdir()):
            if not d.is_dir():
                continue
            chain = d / "fullchain.pem"
            key = d / "privkey.pem"
            if chain.is_file() and key.is_file():
                expiry_date, days_left = _get_cert_expiry(chain)
                domains.append({
                    "domain": d.name,
                    "fullchain_path": str(chain),
                    "privkey_path": str(key),
                    "created_at": "",  # could stat the files
                    "expiry_date": expiry_date,
                    "days_left": days_left,
                })
    return {"domains": domains}


@app.post("/ssl-certs", status_code=201)
def upload_ssl_cert(req: SSLCertUploadRequest) -> dict[str, Any]:
    """Upload SSL certificates for a domain.

    Supports two modes:
    - **Paste mode**: provide ``fullchain`` and ``privkey`` PEM content.
    - **Path mode**: provide ``ssl_path`` (directory containing
      fullchain.pem and privkey.pem).  The files are read from that path.

    Saves fullchain.pem and privkey.pem to ``SSL_DIR/{domain}/``.
    Overwrites existing files.
    """
    domain = req.domain.strip()
    if not domain or "/" in domain or ".." in domain:
        raise HTTPException(400, "Invalid domain name")

    cert_dir = SSL_DIR / domain

    fullchain_content: str
    privkey_content: str
    # R9.4 — filled in path mode only, and PERSISTED only once the certificate has
    # been accepted. A refusal must leave the store byte-for-byte as it was: the
    # directory is created below, after the check, for the same reason.
    _source_path: str | None = None

    if req.ssl_path:
        # Path mode: read files from the provided directory path
        src = Path(req.ssl_path).resolve()
        # Basic safety: prevent traversal outside reasonable paths
        if not src.is_dir():
            raise HTTPException(400, f"SSL path is not a directory: {req.ssl_path}")
        chain_src = src / "fullchain.pem"
        key_src = src / "privkey.pem"
        if not chain_src.is_file():
            raise HTTPException(400, f"fullchain.pem not found in: {req.ssl_path}")
        if not key_src.is_file():
            raise HTTPException(400, f"privkey.pem not found in: {req.ssl_path}")
        fullchain_content = chain_src.read_text()
        privkey_content = key_src.read_text()
        _source_path = str(src)
    else:
        # Paste mode: use the PEM content from the request
        fullchain_content = req.fullchain
        privkey_content = req.privkey

    # ---------------------------------------------------------------
    # R9.4 — WILDCARDS ONLY on the upload path.
    #
    # The exposure handle is minted AFTER the certificate exists ("the name comes
    # only after selecting the certs, never the reverse" — GRILLING G41), so the
    # only shape the platform can actually use is `*.<domain>`: one certificate
    # per domain, covering every one-label name under it. Anything else is
    # refused HERE, with an actionable message, rather than discovered later as a
    # handshake that presents nothing.
    #
    # Scope, deliberately narrow: this gates UPLOADS. Entries already in the
    # store are grandfathered, and the portal's own certificate does not come
    # through this path.
    # ---------------------------------------------------------------
    _names = _cert_dns_names(fullchain_content)
    if not any(n.startswith("*.") for n in _names):
        raise HTTPException(
            400,
            "only WILDCARD certificates are accepted (a CN or SAN entry of the form "
            "`*.<domain>`): the platform serves every one-label name under the domain "
            f"from a single certificate, so a single-host certificate would be "
            f"unusable. Got: {', '.join(_names) or 'no readable DNS names'}",
        )

    # ONLY NOW does anything reach the store (R2-F5): the directory, the
    # `.source_path` marker and the two PEM files are all written after the
    # certificate has been accepted, so a refused upload is a no-op on disk.
    cert_dir.mkdir(parents=True, exist_ok=True)
    if _source_path is not None:
        (cert_dir / ".source_path").write_text(_source_path)

    (cert_dir / "fullchain.pem").write_text(fullchain_content)
    (cert_dir / "privkey.pem").write_text(privkey_content)

    expiry_date, days_left = _get_cert_expiry(cert_dir / "fullchain.pem")

    return {
        "domain": domain,
        "fullchain_path": str(cert_dir / "fullchain.pem"),
        "privkey_path": str(cert_dir / "privkey.pem"),
        "expiry_date": expiry_date,
        "days_left": days_left,
        "message": f"SSL certificates saved for {domain}",
    }


@app.post("/ssl-certs/{domain}/refresh")
def refresh_ssl_cert(domain: str) -> dict[str, Any]:
    """Refresh SSL certificates for a domain from its original source path.

    Reads the stored ``.source_path`` file inside ``SSL_DIR/{domain}/``
    and re-imports fullchain.pem + privkey.pem from that directory.
    """
    cert_dir = SSL_DIR / domain
    if not cert_dir.exists():
        raise HTTPException(404, f"No certificates found for domain: {domain}")

    source_file = cert_dir / ".source_path"
    if not source_file.exists():
        raise HTTPException(
            400,
            f"No source path stored for {domain}. "
            "Re-upload using ssl_path mode to enable refresh.",
        )

    src = Path(source_file.read_text().strip())
    if not src.is_dir():
        raise HTTPException(400, f"Source path no longer exists: {src}")

    chain_src = src / "fullchain.pem"
    key_src = src / "privkey.pem"
    if not chain_src.is_file():
        raise HTTPException(400, f"fullchain.pem not found in: {src}")
    if not key_src.is_file():
        raise HTTPException(400, f"privkey.pem not found in: {src}")

    (cert_dir / "fullchain.pem").write_text(chain_src.read_text())
    (cert_dir / "privkey.pem").write_text(key_src.read_text())

    expiry_date, days_left = _get_cert_expiry(cert_dir / "fullchain.pem")

    return {
        "domain": domain,
        "fullchain_path": str(cert_dir / "fullchain.pem"),
        "privkey_path": str(cert_dir / "privkey.pem"),
        "expiry_date": expiry_date,
        "days_left": days_left,
        "message": f"SSL certificates refreshed for {domain}",
    }


@app.delete("/ssl-certs/{domain}")
def delete_ssl_cert(domain: str) -> dict[str, Any]:
    """Delete SSL certificates for a domain."""
    cert_dir = SSL_DIR / domain
    if not cert_dir.exists():
        raise HTTPException(404, f"No certificates found for domain: {domain}")

    import shutil
    shutil.rmtree(cert_dir)
    return {"domain": domain, "message": f"SSL certificates deleted for {domain}"}

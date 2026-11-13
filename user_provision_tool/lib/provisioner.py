"""lib/provisioner.py — core provisioning workflow shared by CLI and API.

All three operations (register, remove, rebuild) are implemented here so that
both ``cli/`` scripts and ``api.py`` delegate to the same logic without
duplicating it.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

from . import auth, conf_filter, db, docker_ops, keys, registry, stream_access, subnet_manager, template_engine
from .compose_converter import ensure_subnet_ipam_block, get_compose_service_names

_log = logging.getLogger(__name__)

# Registry writes are serialised by lib.registry.transaction() — Tier 1
# (in-process RLock) + Tier 2 (cross-process flock on the registry sidecar
# lock file) — around every read-modify-write.


class DeployFailure(RuntimeError):
    """A deploy step failed; carries the *stage* so the entry can record it.

    Subclasses RuntimeError so existing ``except RuntimeError`` rollback paths
    keep catching it.
    """

    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


#: Bounded failure message — the build log can be megabytes, and the entry is
#: an inventory record, not a log sink. The full text stays in the task log.
FAILURE_MESSAGE_MAX = 500


def _bounded_failure_message(message: str) -> str:
    """Bound a failure message to a few hundred chars (inventory-sized).

    Keeps the HEAD **and** the TAIL: a docker failure reads
    ``Command failed (exit 1): <the whole command>\\n<the real cause>`` — the
    real cause is last, so a head-only truncation would store the command and
    throw away the only useful line. The full text stays in the task log.
    """
    text = " ".join(str(message or "").split())
    if len(text) <= FAILURE_MESSAGE_MAX:
        return text
    head = FAILURE_MESSAGE_MAX // 3
    tail = FAILURE_MESSAGE_MAX - head - len(" … ")  # 3 chars for the separator
    return text[:head] + " … " + text[-tail:]


#: Machine-readable failure reasons the UI branches on, so a card never has to
#: pattern-match prose that may be reworded. Extend as the -api names more.
_REASON_PATTERNS = (
    ("at_capacity", ("at capacity",)),
)


def classify_failure_reason(message: str) -> str:
    """A stable reason code for *message* — `"unknown"` when none matches.

    The dashboard must not regex the -api's wording — the wording is for humans
    and may change; this code is the contract (user ruling 2026-09-20). The
    fallback is the literal ``"unknown"`` so the field is never empty and the UI
    has one value to render.
    """
    text = str(message or "").lower()
    for reason, needles in _REASON_PATTERNS:
        if any(n in text for n in needles):
            return reason
    return "unknown"


def _mark_failed(
    service_id: str,
    stage: str,
    message: str,
    reason: str | None = None,
) -> None:
    """Record a failure on the instance row instead of dropping it (G10/G17).

    Failure is instance-scoped state: the service identity stays and the card
    survives with a bounded ``failure`` payload. There is no ``error`` status.
    """
    import datetime

    failure = {
        "stage": stage,
        "message": _bounded_failure_message(message),
        "reason": reason or classify_failure_reason(message),
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    with db.connection() as conn:
        registry.update_instance(conn, service_id, status="failed", failure=failure)


def _update_registry_endpoints(
    service_id: str,
    pairs: list[tuple[str, str]], instance_stem: str,
) -> None:
    """Record the endpoints the RENDERED confs serve (R6.1/R6.2)."""
    stream = _stream_sub_services(pairs)
    http = _http_endpoints(pairs, instance_stem)
    with db.connection() as conn:
        registry.set_endpoints(
            conn, service_id, http, stream,
            _stream_port(pairs),
        )


def _after_successful_transition(service_id: str, status: str) -> None:
    """A successful transition clears a stale failure payload."""
    with db.connection() as conn:
        registry.update_instance(conn, service_id, status=status, failure=None)


def _stream_port(pairs: list[tuple[str, str]]) -> int | None:
    """The stream row's target port from the rendered stream conf."""
    import re as _re
    for _tpl, out in pairs or []:
        if not str(out).endswith(".stream.conf"):
            continue
        try:
            text = Path(out).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = _re.search(r"proxy_pass\s+(?:https?://)?[^:;\s]+:(\d+)", text)
        if m:
            return int(m.group(1))
    return None


def _copy_bind_source_if_empty(
    vol_dir: Path, declared_src: str, template_dir: Path
) -> None:
    """Bootstrap a per-user bind target by copy-if-empty (design §Bind mounts).

    For each bind key whose declared source path exists in the repo, copy its
    content (file→file, dir→dir recursive) into the per-user target ONLY when
    that target is empty or absent.  Skip silently when the source doesn't
    exist (dify ``./volumes/**`` → correct empty dir).  Runtime data written
    by containers is never clobbered (non-empty targets are left untouched).
    """
    if vol_dir.exists() and not vol_dir.is_dir():
        return
    src = template_dir / declared_src
    if not src.exists():
        return
    if src.is_file():
        # File→file bind: the per-user target must be a FILE, not a dir.
        if vol_dir.exists() and vol_dir.is_dir():
            # An empty dir (e.g. mkdir'd by an earlier deploy) would shadow
            # the file mount — replace it ONLY when it is empty.
            if any(vol_dir.iterdir()):
                return
            vol_dir.rmdir()
        if not vol_dir.exists():
            vol_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, vol_dir)
        return
    if not src.is_dir():
        return
    # Dir→dir recursive copy, only when absent or empty.
    if vol_dir.exists() and any(vol_dir.iterdir()):
        return  # runtime data present — never clobber
    vol_dir.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        dst = vol_dir / item.name
        if item.is_dir():
            shutil.copytree(item, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dst)


def _bootstrap_volumes_from_template(
    volumes: dict[str, str], compose_template: str
) -> None:
    """Copy-if-empty bootstrap for an EXPLICIT volume mapping.

    Each key's declared source (parsed from the converter header comments in
    the template) is copied into the per-user target when absent or empty —
    file→file sources land as FILES (never directories), so container
    entrypoints that ``cp`` their mount target keep working.  Keys without a
    declared source (runtime data dirs) are mkdir'd as before.
    """
    sources = template_engine.extract_template_volume_sources(compose_template)
    template_dir = Path(compose_template).parent
    for key, host_path in list(volumes.items()):
        vol_dir = Path(host_path)
        declared_src = sources.get(key)
        if declared_src:
            _copy_bind_source_if_empty(vol_dir, declared_src, template_dir)
        if not vol_dir.exists():
            vol_dir.mkdir(parents=True, exist_ok=True)


def _materialize_env_examples(recipe_dir: Path) -> None:
    """Copy `*.env.example` → the declared `*.env` when the latter is missing.

    Repo-shipped examples become the service env files compose loads (marked
    ``.generated``); an existing declared file (e.g. an LLM-generated ``.env``)
    is never overwritten.
    """
    if not recipe_dir.is_dir():
        return
    for example in recipe_dir.rglob("*.env.example"):
        declared = example.with_suffix("")
        if declared.exists():
            continue
        try:
            shutil.copy2(example, declared)
            Path(str(declared) + ".generated").write_text("")
        except OSError:
            continue


def _register_render_and_start(
    nginx_conf_pairs: dict[str, tuple[str, str]] | list[tuple[str, str]] | None = None,
    *,
    service_id: str,
    user_name: str,
    service_name: str,
    label: str,
    entry: dict[str, Any],
    compose_template: str,
    compose_out: str,
    volumes: dict[str, str],
    env_file: str | None,
    env_files: list[str] | None,
    service_env_files: dict[str, str] | None,
    nginx_template: str | None,
    nginx_out: str | None,
    htpasswd_out: str | None,
    passwd_hash: str,
    domain: str,
    https: bool,
    ssl_certificate_path: str,
    ssl_certificate_key_path: str,
    build_args: dict[str, str] | None,
    profiles: list[str] | None,
    _subnet: str,
    _gateway: str,
) -> str | None:
    """Render the per-user compose/nginx files and start the containers.

    Part of ``register_user`` — separated so the caller can roll the registry
    entry back when ANY step here fails (a render error must never leave a
    stale registration behind). Returns the primary per-user env copy path
    (``copied_env``) or None.
    """
    # --- Render compose file ---
    # Effective interpolation env files: repeatable --env-file order preserved
    # (env_files list first, legacy env_file as the primary).
    _all_env: list[str] = list(env_files or [])
    if env_file and env_file not in _all_env:
        _all_env.insert(0, env_file)
    try:
        copied_env = template_engine.render_compose(
            compose_template, compose_out,
            user_name, service_name, label, volumes,
            env_file=_all_env[0] if _all_env else None,
            env_files_all=_all_env or None,
            env_files=service_env_files or None,
            subnet=_subnet,
            gateway=_gateway,
        )
    except Exception as exc:
        raise DeployFailure("render", str(exc)) from exc

    # --- Always-pass explicit per-user --env-file invariant (design G17) ---
    # Compose auto-reads <project-dir>/.env when no --env-file is given; an
    # EMPTY per-user env file fully overrides that auto-read. When nothing is
    # selected we still create and pass the empty per-user file.
    _env_files_used: list[str] = []
    if copied_env:
        _env_files_used.append(copied_env)
    for _idx, _ef in enumerate(_all_env[1:], start=1):
        _dest = (
            Path(compose_out).parent
            / template_engine.canonical_env_file_name(_idx, user_name, label)
        )
        if _dest.exists():
            _env_files_used.append(str(_dest))
    if not _env_files_used:
        _empty_env = Path(compose_out).parent / template_engine.canonical_env_file_name(0, user_name, label)
        if not _empty_env.exists():
            _empty_env.write_text("")
            Path(str(_empty_env) + ".generated").write_text("")
        _env_files_used = [str(_empty_env)]
        copied_env = str(_empty_env)

    # --- Record container names from the rendered compose ---
    prefix = template_engine.container_prefix(service_name, user_name, label)
    compose_svc_names = get_compose_service_names(compose_out)
    container_names = [f"{prefix}{svc}" for svc in compose_svc_names]
    entry["container_names"] = container_names
    entry["env_files"] = _env_files_used

    # Update the instance projection with the per-user copied env path and
    # container names so rebuild/remove/reconciliation resolve them by ID.
    if copied_env:
        entry["env_file_path"] = copied_env
    with db.connection() as conn:
        registry.update_instance(
            conn, service_id,
            env_file_path=copied_env or entry.get("env_file_path") or "",
            env_files=_env_files_used,
            container_names=container_names,
        )

    # --- Render nginx conf + htpasswd ---
    # --- Render nginx conf(s) + htpasswd ---
    # R9.2: the payload may carry several confs (an http one and a stream one).
    # The PAIRS arrive from the caller, which knows the instance identity and the
    # category — the stream conf must land as `*.stream.conf` (the pattern the
    # internal nginx's include matches) and nothing here has to know that rule.
    _pairs = (
        list(nginx_conf_pairs.values())
        if isinstance(nginx_conf_pairs, dict)
        else list(nginx_conf_pairs or [])
    )
    if nginx_template and nginx_out and not _pairs:
        _pairs = [(nginx_template, nginx_out)]

    if htpasswd_out and _pairs:
        auth.write_htpasswd_file(htpasswd_out, user_name, passwd_hash)
        # Mark htpasswd as generated
        Path(htpasswd_out + ".generated").write_text("")
    # G61 (D6) — the confs follow the compose's ACTIVE services. Each rendered
    # block is resolved to the compose service it proxies to; a block whose
    # service cannot run under the deployment's profiles is dropped BEFORE the
    # file is written, so the recorded endpoints (read from these files below)
    # list exactly what is served. Applies to a selected conf and a generated
    # one, http and stream alike — there is no gateway-side profile logic.
    _active = conf_filter.compose_active_services(compose_out, profiles)
    for _tpl, _out in _pairs:
        text = template_engine.render_nginx_conf(
            _tpl, _out,
            user_name, service_name, label,
            domain, htpasswd_out or "",
            https=https,
            ssl_certificate_path=ssl_certificate_path,
            ssl_certificate_key_path=ssl_certificate_key_path,
            write=False,
        )
        if _active is not None:
            text, _dropped = conf_filter.filter_conf_blocks(
                text,
                active_services=_active[0],
                all_services=_active[1],
                container_prefix=prefix,
            )
        Path(_out).write_text(text, encoding="utf-8")
        Path(str(_out) + ".generated").write_text("")

    # R3-F53 — record the served endpoints from the confs this function JUST
    # rendered. The entry was built earlier, and reading a rendered conf before
    # it exists yields NOTHING: measured 2026-10-10, where a live deployment
    # served `graphiti-local-shared2-1--neo4j.snaprovision.com` and
    # `…--graph-falkordb.snaprovision.com` and recorded NEITHER, so the card
    # offered no `/go/` row for an endpoint that existed. `stream_sub_services`
    # (round 1's R6.1) had the same hole for the same reason.
    _update_registry_endpoints(
        service_id, _pairs,
        f"{service_name}-{user_name}-{label}",
    )

    # --- Mark as building (container not yet started) ---
    _after_successful_transition(service_id, "building")

    # --- Start containers ---
    # Profiles: explicit per-user --profile flags; default (none) activates
    # no profile plus the implicit "" services.
    def _stage_for(exc: Exception, default: str) -> str:
        # An operator cancel is not a build failure (§4b) — it gets its own
        # stage so the card can say *cancelled by operator*.
        if isinstance(exc, docker_ops.CommandCancelled):
            return "cancelled"
        return default

    if build_args:
        try:
            docker_ops.compose_build(compose_out, env_file=_env_files_used, project_name=entry["network_name"], build_args=build_args, profiles=profiles)
        except Exception as exc:
            raise DeployFailure(_stage_for(exc, "build"), str(exc)) from exc
    try:
        docker_ops.compose_up(compose_out, env_file=_env_files_used, project_name=entry["network_name"], profiles=profiles)
    except Exception as exc:
        raise DeployFailure(_stage_for(exc, "up"), str(exc)) from exc
    return copied_env


def _auto_volumes(
    compose_template: str,
    user_name: str,
    service_name: str,
    label: str,
    user_data_dir: Path,
) -> dict[str, str]:
    """Create per-volume host directories and return the volume → host-path mapping.

    Directories are created at:
        ``{user_data_dir}/{user_name}/{service_name}/{label}/{volume_key}/``

    Declared source paths (parsed from the converter header comments) are
    bootstrapped by copy-if-empty — repo-shipped configuration (e.g. dify's
    ``./nginx/*`` bind mounts) is copied into an empty/absent per-user target,
    so empty bind dirs never delete the shipped config.

    The returned dict can be passed directly to ``register_user`` as ``volumes``.
    """
    keys = template_engine.extract_template_volumes(compose_template)
    sources = template_engine.extract_template_volume_sources(compose_template)
    template_dir = Path(compose_template).parent
    base = user_data_dir / user_name / service_name / label
    result: dict[str, str] = {}
    for key in keys:
        vol_dir = base / key
        declared_src = sources.get(key)
        if declared_src:
            # Bootstrap FIRST so a file→file bind target is created as a FILE
            # (never mkdir'd into a directory that would shadow the mount).
            _copy_bind_source_if_empty(vol_dir, declared_src, template_dir)
        if not vol_dir.exists():
            vol_dir.mkdir(parents=True, exist_ok=True)
        result[key] = template_engine.volume_path(
            user_data_dir, user_name, service_name, label, key
        )
    return result


def _stream_sub_services(pairs: list[tuple[str, str]]) -> list[str]:
    """The canonical names in the instance's RENDERED stream conf, if any.

    Read from the rendered file rather than from the artifact: the rendered conf
    is what the internal nginx actually loads, so a name that appears here is a
    name that routes. An unreadable or absent file yields an empty list — the
    card then offers nothing, which is the honest state (R6.1: the surface shows
    what the deployment created, and nothing else).
    """
    import re as _re

    names: list[str] = []
    for tpl, out in pairs or []:
        # G55/G56 — the category of a pair is the RENDERED file's, not the
        # source's: the stream source is now a recipe-level template
        # (`nginx.stream.base.<job>.conf[.j2]`), whose name does not carry the
        # `.stream.conf` suffix. The rendered name always does.
        name = str(out)
        if not name.endswith(".stream.conf"):
            continue
        try:
            text = Path(out).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _re.finditer(r'^[ \t]*server_name[ \t]+([^;\n]+);', text, _re.MULTILINE):
            for token in m.group(1).split():
                token = token.strip()
                if token and token not in names:
                    names.append(token)
    return names


def _http_endpoints(pairs: list[tuple[str, str]], instance_stem: str) -> list[str]:
    """The EXTRA http endpoints the instance's RENDERED http conf serves.

    R6.2: the card lists one `/go/` row per exposed HTTP endpoint, keyed by the
    sub-service name. Like `_stream_sub_services`, the list is read from the
    RENDERED conf — a name that appears there is one the internal nginx actually
    routes — and it records the SAME thing that function records: the whole
    served name (`<instance host>--<sub_service>.<domain>`), not a port and not a
    bare label. The card slices the key off it, exactly as it does for the stream
    side, so the two surfaces read one convention.

    The PRIMARY is deliberately absent: its endpoint is the instance's own URL,
    which the card already shows, keyed `primary` (R6.2). An unreadable or absent
    conf yields an empty list, which is the honest state.
    """
    import re as _re

    # R1.5's reserved separator — `<instance host>--<sub_service>.<domain>`.
    prefix = f"{instance_stem}--"
    found: list[str] = []
    for tpl, out in pairs or []:
        # G55 — the RENDERED name carries the category (`*.nginx.conf`,
        # `*.stream.conf`, `*.mail.conf`); the source may be a recipe-level
        # template whose name does not. Classifying by the source put the stream
        # conf in the http bucket.
        name = str(out)
        if not name.endswith(".conf") or name.endswith(".stream.conf") or name.endswith(".mail.conf"):
            continue
        try:
            text = Path(out).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _re.finditer(r'^[ \t]*server_name[ \t]+([^;\n]+);', text, _re.MULTILINE):
            for token in m.group(1).split():
                token = token.strip()
                if token.startswith(prefix) and token not in found:
                    found.append(token)
    return found


def prepare_registration(
    *,
    service_id: str | None = None,
    procedure_id: str | None = None,
    task_id: str,
    owner_api_user_id: str | None = None,
    owner_username: str | None = None,
    user_name: str | None = None,
    service_name: str,
    label: str | None,
    recipe_path: str,
    compose_service_count: int = 0,
    domain: str = "localhost",
) -> dict[str, Any]:
    """Synchronously reserve the deploy: task + services + instance(building).

    Called by the register handler BEFORE it returns 202 (R6.1). No Docker work
    happens here; the worker only builds and starts. The owner is resolved (and
    a direct/CLI owner is ensured) in the same transaction as ``services``.

    Adoption rules (R2.2): a supplied ``service_id`` is adopted only for a
    failed row (retry) or a paused-fix redeploy; an identical replay while
    building/running returns the existing instance/task; a different
    tuple/recipe or a tombstone is 409; a new deploy procedure for the same
    tuple while building/running is 409.
    """
    if owner_username is None and user_name:
        owner_username = user_name
    if not recipe_path and recipe_path != "":
        raise ValueError("recipe_path is required")
    supplied_service_id = uuid.UUID(str(service_id)) if service_id else None
    new_service_id = supplied_service_id or uuid.uuid4()
    new_procedure_id = uuid.UUID(str(procedure_id)) if procedure_id else uuid.uuid4()
    subnet_manager._load_env()
    template_engine._load_env()

    with db.connection() as conn:
        owner_id = registry.resolve_owner(
            conn, owner_api_user_id, owner_username, ensure_mode="owner"
        )
        owner = conn.execute(
            "SELECT username FROM registry.api_users WHERE id=%s", (owner_id,)
        ).fetchone()
        username = owner["username"]

        # Allocate the label under the per-(owner,service) lock so two
        # concurrent creates cannot pick the same one; the partial unique index
        # is the final backstop.
        registry.label_lock(conn, owner_id, service_name)
        if label is None:
            labels = conn.execute(
                "SELECT label FROM registry.services WHERE owner_api_user_id=%s "
                "AND service_name=%s", (owner_id, service_name)
            ).fetchall()
            ints = []
            for r in labels:
                try:
                    ints.append(int(str(r["label"])))
                except (TypeError, ValueError):
                    continue
            label = str(max(ints) + 1) if ints else "0"

        existing = conn.execute(
            "SELECT id, recipe_path, deleted_at FROM registry.services "
            "WHERE owner_api_user_id=%s AND service_name=%s AND label=%s "
            "AND deleted_at IS NULL",
            (owner_id, service_name, str(label)),
        ).fetchone()
        current = None
        if service_id:
            current = conn.execute(
                "SELECT s.id, s.owner_api_user_id, s.service_name, s.label, s.recipe_path, "
                "s.deleted_at, i.status, i.current_procedure_id, i.failure "
                "FROM registry.services s LEFT JOIN registry.service_instances i "
                "ON i.service_id=s.id WHERE s.id=%s", (str(service_id),)
            ).fetchone()
            if current and current["deleted_at"] is not None:
                raise registry.RegistryError("service id is tombstoned")
            if current and (
                current["owner_api_user_id"] != owner_id
                or current["service_name"] != service_name
                or str(current["label"]) != str(label)
            ):
                raise registry.RegistryError("service id does not match this owner/tuple")
            if current and current["recipe_path"] != recipe_path:
                raise registry.RegistryError("recipe_path is immutable for a service")
        if existing and (not current or existing["id"] != current["id"]):
            raise registry.RegistryError(
                f"service {service_name!r} label {label!r} is already registered"
            )
        if current is None:
            tomb = conn.execute(
                "SELECT id FROM registry.services WHERE owner_api_user_id=%s "
                "AND service_name=%s AND label=%s AND deleted_at IS NOT NULL",
                (owner_id, service_name, str(label)),
            ).fetchone()
            if tomb:
                raise registry.RegistryError(
                    f"label {label!r} is historical and is never reused; use a new label"
                )

        sid = current["id"] if current else new_service_id
        status = current["status"] if current else None
        if current and status in ("building", "running"):
            same_proc = current["current_procedure_id"] == new_procedure_id
            if same_proc:
                # Identical replay: return the existing instance/task.
                row = conn.execute(
                    "SELECT task_id FROM registry.tasks WHERE procedure_id=%s "
                    "ORDER BY created_at DESC, task_id DESC LIMIT 1",
                    (new_procedure_id,),
                ).fetchone()
                return {"service_id": str(sid), "procedure_id": str(new_procedure_id),
                        "task_id": row["task_id"] if row else task_id,
                        "owner_api_user_id": str(owner_id), "username": username,
                        "label": str(label), "replayed": True}
            raise registry.RegistryError(
                "a deploy is already running for this owner/service/label"
            )

        if current is None:
            registry.insert_service(
                conn, service_id=sid, owner_id=owner_id, service_name=service_name,
                label=str(label), recipe_path=recipe_path,
            )
        registry.insert_task(
            conn, task_id=task_id, procedure_id=new_procedure_id, service_id=sid,
            kind="deploy", operation="register",
        )
        # Hostname/network identity is name-derived but the owner component is
        # the validated, immutable api_users.username.
        hostname = template_engine.service_hostname(service_name, username, str(label), domain)
        network_name = template_engine.user_network_name(service_name, username, str(label))
        if status is None:
            registry.insert_instance(
                conn, sid, procedure_id=new_procedure_id, status="building",
                hostname=hostname, network_name=network_name,
                compose_template_path="", compose_file_path="",
            )
        else:
            registry.update_instance(
                conn, sid, status="building", current_procedure_id=new_procedure_id,
                failure=None, previous_status=None, previous_failure=None,
                hostname=hostname, network_name=network_name,
            )
        return {"service_id": str(sid), "procedure_id": str(new_procedure_id),
                "task_id": task_id, "owner_api_user_id": str(owner_id),
                "username": username, "label": str(label), "replayed": False,
                "created_service": current is None,
                # The cancel/abort path reverts an existing card to the status
                # (and failure) it had before this task set it building (R6.1).
                "previous_status": status,
                "previous_failure": (current or {}).get("failure") if current else None}


def abort_new_registration(service_id: str, task_id: str, *, created: bool) -> None:
    """Capacity refusal before a worker starts: leave no half-created identity.

    A brand-new service/task/instance is removed; an adopted failed row keeps
    its service and history, and only the refused task row is dropped.
    """
    with db.connection() as conn:
        conn.execute("DELETE FROM registry.tasks WHERE task_id=%s", (task_id,))
        if created:
            conn.execute("DELETE FROM registry.service_endpoints WHERE service_id=%s", (service_id,))
            conn.execute("DELETE FROM registry.service_basic WHERE service_id=%s", (service_id,))
            conn.execute("DELETE FROM registry.service_instances WHERE service_id=%s", (service_id,))
            conn.execute("DELETE FROM registry.services WHERE id=%s", (service_id,))


def restore_projection(
    *, service_id: str, previous_status: str | None,
    previous_failure: dict | None = None, reason: str = "cancelled before it started",
) -> None:
    """Undo a reserved identity change without touching runtime resources.

    A waiting (queued/planned) deploy of an EXISTING instance changed only the
    registry row to `building`; cancelling it must revert that row and leave the
    running containers alone (R6.1).
    """
    restore = previous_status or "failed"
    failure = previous_failure if restore == "failed" else None
    if restore == "failed" and not failure:
        failure = {"error": reason}
    try:
        with db.connection() as conn:
            registry.update_instance(
                conn, service_id, status=restore, failure=failure,
                previous_status=None, previous_failure=None,
            )
    except Exception:  # noqa: BLE001 — the row write must not mask the cancel
        logging.getLogger(__name__).exception(
            "restore_projection failed for %s", service_id)


def compensate(
    *,
    service_id: str,
    operation: str = "",
    creation: bool = False,
    previous_status: str | None = None,
    previous_failure: dict | None = None,
    reason: str = "cancelled or interrupted",
    nginx_container: str = "subnet-acl-nginx",
) -> dict[str, Any]:
    """Runtime-only compensating cleanup after a cancel/timeout/restart (R6.2).

    Removes the containers and network the instance row names, and restores the
    registry projection (a card-creating task loses its identity; an existing
    instance returns to the status it had before the task). Generated files —
    source-project and rendered — are NEVER deleted here (the house rule); the
    one historical exception (a stale per-instance stream conf) is deliberately
    not applied on the abort path.
    """
    _log_ = logging.getLogger(__name__)
    entry = registry.get_service_by_id(service_id)
    if not entry:
        return {"service_id": service_id, "cleaned": False, "reason": "no instance"}
    net = entry.get("network_name") or ""
    compose_file = entry.get("compose_file_path") or ""
    status = entry.get("status")

    # 1. Runtime resources: containers and the instance network. Only when the
    # task was actually changing them (a create, or an instance still building/
    # deleting); a cancel of waiting work must never stop a running service.
    tearing_down = creation or status in ("building", "deleting")
    if tearing_down:
        try:
            if compose_file and Path(compose_file).exists():
                docker_ops.compose_down(
                    compose_file, project_name=net or None, remove_volumes=False,
                    profiles=list(entry.get("profiles") or []) or None,
                )
            elif net:
                docker_ops.compose_down_by_project(net, remove_volumes=False)
        except Exception:  # noqa: BLE001 — best effort; the row records the outcome
            _log_.exception("compensating compose down failed for %s", service_id)
    if net and tearing_down:
        try:
            docker_ops.network_disconnect(nginx_container, net)
        except Exception:  # noqa: BLE001
            pass
        try:
            docker_ops.orphan_network_cleanup(net, nginx_container)
        except Exception:  # noqa: BLE001
            pass

    # 2. Registry projection.
    try:
        if creation:
            with db.connection() as conn:
                conn.execute("DELETE FROM registry.service_endpoints WHERE service_id=%s",
                             (service_id,))
                conn.execute("DELETE FROM registry.service_basic WHERE service_id=%s",
                             (service_id,))
                conn.execute("DELETE FROM registry.service_instances WHERE service_id=%s",
                             (service_id,))
                conn.execute("DELETE FROM registry.services WHERE id=%s", (service_id,))
        else:
            restore = previous_status or "failed"
            failure = previous_failure if restore == "failed" else None
            if restore == "failed" and not failure:
                failure = {"error": reason}
            with db.connection() as conn:
                registry.update_instance(
                    conn, service_id, status=restore, failure=failure,
                    previous_status=None, previous_failure=None,
                )
    except Exception:  # noqa: BLE001 — never let the cleanup mask the task result
        _log_.exception("compensating registry restore failed for %s", service_id)
    return {"service_id": service_id, "cleaned": True, "operation": operation}


def register_user(
    *,
    service_id: str | None = None,
    procedure_id: str | None = None,
    owner_api_user_id: str | None = None,
    owner_username: str | None = None,
    recipe_path: str = "",
    user_name: str | None = None,
    service_name: str,
    label: str,
    compose_template: str,
    output_dir: str | Path,
    volumes: dict[str, str] | None = None,
    passwd: str = "",
    nginx_template: str | None = None,
    domain: str = "localhost",
    env_file: str | None = None,
    env_files: list[str] | None = None,
    profiles: list[str] | None = None,
    service_env_files: dict[str, str] | None = None,
    compose_sources: list[str] | None = None,
    nginx_container: str = "subnet-acl-nginx",
    nginx_output_dir: str | Path | None = None,
    nginx_conf_templates: list[str] | None = None,
    nginx_conf_templates_by_category: dict[str, str] | None = None,
    user_data_dir: str | Path | None = None,
    build_args: dict[str, str] | None = None,
    https: bool = False,
    fullchain: str | None = None,
    privkey: str | None = None,
    ssl_base_dir: str = "/provision/ssl",
    _task_id: str | None = None,
) -> dict[str, Any]:
    """Register a user and start their service containers.

    Steps
    -----
    1. Volume cross-check (returned in result, callers decide how to present).
    2. Duplicate-registration check (atomic with step 3).
    3. Add registry entry.
    4. Render compose file (optionally copies env_file).
    5. Render nginx conf + write htpasswd file.
    6. (If *https*) copy SSL certs to ``/provision/ssl/{domain}/``.
    7. ``docker compose build`` (if *build_args* provided) then ``docker compose up``.
    8. Connect provision-nginx to the user's isolated network + reload.

    Parameters
    ----------
    compose_template:
        Absolute path to a Jinja2 compose template (.yml.j2).
    output_dir:
        Directory where the rendered compose file is written (should be the
        source project root so ``build: .`` contexts resolve correctly).
    nginx_output_dir:
        Directory where nginx conf and htpasswd files are written.  Defaults
        to ``output_dir`` when not provided.
    user_data_dir:
        When provided and *volumes* is ``None`` or empty, host directories are
        automatically created under ``{user_data_dir}/{user_name}/{service_name}/{label}/{vol_key}/``
        and the resulting mapping is used as the volume dict.  When *volumes*
        is explicitly supplied it takes precedence over this parameter.
    passwd:
        Plain-text password.  Empty string → no htpasswd file written.
    nginx_container:
        Name of the provision-nginx Docker container.
    https:
        Enable HTTPS support.  When True, *fullchain* and *privkey* must be provided.
    fullchain:
        Path to the fullchain.pem certificate file.  Copied to
        ``{ssl_base_dir}/{domain}/fullchain.pem`` (or used directly if a bare filename).
    privkey:
        Path to the privkey.pem private key file.  Copied to
        ``{ssl_base_dir}/{domain}/privkey.pem`` (or used directly if a bare filename).
    ssl_base_dir:
        Base directory for SSL certificates.  Defaults to ``/provision/ssl``.
        Override via ``PROVISION_SSL_DIR`` env var or pass explicitly.

    Returns
    -------
    dict with keys:
        ``entry``           — the registry entry dict
        ``volume_warnings`` — ``{"missing": [...], "extra": [...]}``
        ``copied_env``      — absolute path to the copied .env file, or ``None``

    Raises
    ------
    ValueError
        If user/service/label is already registered.
    RuntimeError
        If ``docker compose up`` fails.
    """
    # --- Reserve the identity synchronously if the caller did not ------------
    if service_id is None:
        prepared = prepare_registration(
            procedure_id=procedure_id,
            task_id=_task_id or f"api-{uuid.uuid4().hex[:12]}",
            owner_api_user_id=owner_api_user_id, owner_username=owner_username,
            user_name=user_name, service_name=service_name, label=label,
            recipe_path=recipe_path, domain=domain,
        )
        service_id = prepared["service_id"]
        procedure_id = prepared["procedure_id"]
    entry_row = registry.get_service_by_id(service_id)
    if not entry_row:
        raise KeyError(f"Service not found: {service_id}")
    user_name = entry_row["user_name"]
    service_name = entry_row["service_name"]
    label = entry_row["label"]

    # The Basic credential: a supplied plaintext is encrypted; an instance-only
    # redeploy re-derives .htpasswd from the stored secret (R2.6); a
    # reset_required (bcrypt-only import) row is never guessed.
    existing_basic = db.fetch_one(
        "SELECT secret_enc, key_version, basic_version, reset_required "
        "FROM registry.service_basic WHERE service_id=%s", (service_id,)
    )
    plain_password = passwd or None
    if (plain_password is None and existing_basic and existing_basic["secret_enc"]
            and not existing_basic["reset_required"]):
        plain_password = keys.decrypt(existing_basic["secret_enc"].decode())

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    nginx_dir = Path(nginx_output_dir) if nginx_output_dir else output_dir
    nginx_dir.mkdir(parents=True, exist_ok=True)

    # --- Auto-generate volumes if not supplied ---
    if not volumes:
        if user_data_dir:
            volumes = _auto_volumes(
                compose_template, user_name, service_name, label,
                Path(user_data_dir),
            )
        else:
            volumes = {}
    else:
        # Explicit volumes (e.g. the deploy panel's pre-filled mapping) still
        # need the copy-if-empty bootstrap: repo-shipped FILE→FILE binds
        # (dify's docker-entrypoint.sh / squid templates / nginx templates /
        # sandbox conf) must land as FILES at the per-user target — without
        # the bootstrap the target is an empty DIRECTORY and container
        # entrypoints crash ("cp: omitting directory").
        _bootstrap_volumes_from_template(volumes, compose_template)

    # --- Complete the volume mapping (render-safety) ---
    # The template references EVERY declared bind key; a panel mapping that
    # misses keys (e.g. built from a compose preview that merged different
    # files) crashes the Jinja render with UndefinedError. Auto-fill missing
    # keys with the standard per-user path + copy-if-empty bootstrap so the
    # render and the mounts always have a value.
    expected_vols = template_engine.extract_template_volumes(compose_template)
    if user_data_dir and any(k not in volumes for k in expected_vols):
        auto = _auto_volumes(
            compose_template, user_name, service_name, label,
            Path(user_data_dir),
        )
        for k in expected_vols:
            if k not in volumes:
                volumes[k] = auto.get(k) or str(
                    Path(user_data_dir) / user_name / service_name / label / k
                )

    # --- Volume cross-check (informational; callers decide how to surface) ---
    missing_vols = [k for k in expected_vols if k not in volumes]
    extra_vols = [k for k in volumes if k not in expected_vols]

    # --- Hash password (derived from the stored/supplied plaintext) ---
    passwd_hash = auth.hash_password(user_name, plain_password) if plain_password else ""

    # --- HTTPS: validate + copy certs ---
    ssl_certificate_path = ""
    ssl_certificate_key_path = ""
    if https:
        if not fullchain:
            raise ValueError(
                "https=True requires a valid --fullchain path to the certificate file."
            )
        if not privkey:
            raise ValueError(
                "https=True requires a valid --privkey path to the private key file."
            )
        ssl_dir = Path(ssl_base_dir) / domain
        ssl_dir.mkdir(parents=True, exist_ok=True)

        # --- Resolve fullchain ---
        # Bare filename (no path separator) → look up directly in /provision/ssl/{domain}/
        # Full / relative path → copy to /provision/ssl/{domain}/fullchain.pem (normalized name)
        if Path(fullchain).is_absolute() or "/" in str(fullchain):
            _fc = Path(fullchain).resolve()
            if not _fc.is_file():
                raise ValueError(
                    f"https=True: fullchain file not found at {fullchain}"
                )
            ssl_certificate_path = str(ssl_dir / "fullchain.pem")
            _dest = Path(ssl_certificate_path).resolve()
            if _fc != _dest:
                shutil.copy2(str(_fc), ssl_certificate_path)
        else:
            ssl_certificate_path = str(ssl_dir / fullchain)
            if not Path(ssl_certificate_path).is_file():
                raise ValueError(
                    f"https=True: fullchain file not found at {ssl_certificate_path}"
                )

        # --- Resolve privkey (same logic) ---
        if Path(privkey).is_absolute() or "/" in str(privkey):
            _pk = Path(privkey).resolve()
            if not _pk.is_file():
                raise ValueError(
                    f"https=True: privkey file not found at {privkey}"
                )
            ssl_certificate_key_path = str(ssl_dir / "privkey.pem")
            _pdest = Path(ssl_certificate_key_path).resolve()
            if _pk != _pdest:
                shutil.copy2(str(_pk), ssl_certificate_key_path)
        else:
            ssl_certificate_key_path = str(ssl_dir / privkey)
            if not Path(ssl_certificate_key_path).is_file():
                raise ValueError(
                    f"https=True: privkey file not found at {ssl_certificate_key_path}"
                )

    # --- Output paths ---
    compose_out = str(output_dir / f"docker-compose.user-{user_name}.{label}.yml")
    nginx_out: str | None = None
    htpasswd_out: str | None = None
    # G55 — ONE rendered file per category, and the category (not the input file
    # name) drives the output suffix the internal nginx includes on.
    nginx_out_by_category: dict[str, tuple[str, str]] = {}

    if nginx_template:
        nginx_out = str(nginx_dir / f"{service_name}.user-{user_name}.{label}.nginx.conf")
        if passwd_hash:
            htpasswd_out = str(nginx_dir / f"{service_name}.user-{user_name}.{label}.htpasswd")
    if nginx_conf_templates_by_category:
        for _cat, _tpl in nginx_conf_templates_by_category.items():
            nginx_out_by_category[_cat] = (
                _tpl,
                str(nginx_dir / template_engine.rendered_conf_name(
                    service_name, user_name, label, _tpl, category=_cat
                )),
            )
    else:
        for _tpl in (nginx_conf_templates or []):
            _name = Path(_tpl).name
            if _name.endswith(".j2"):
                _name = _name[:-3]
            _cat = (
                "stream" if _name.endswith(".stream.conf")
                else "mail" if _name.endswith(".mail.conf")
                else "http"
            )
            if _cat in nginx_out_by_category:
                # One file per category: never overwrite silently.
                raise ValueError(
                    f"two nginx confs of category {_cat!r} — one file per category"
                )
            nginx_out_by_category[_cat] = (
                _tpl,
                str(nginx_dir / template_engine.rendered_conf_name(
                    service_name, user_name, label, _tpl, category=_cat
                )),
            )

    # --- Gap 8: auto re-convert old compose templates when subnet pools are
    # enabled. If the template lacks the {% if subnet %} ipam block, the
    # allocation below would reserve a subnet that never renders (Docker then
    # auto-assigns → allocation leak). Re-convert (back up as .bak) first.
    # Re-load env so tests that set SUBNET_POOLS/RESOURCE_PREFIX after import are honored.
    subnet_manager._load_env()
    template_engine._load_env()
    if subnet_manager.SUBNET_POOLS:
        ensure_subnet_ipam_block(compose_template)

    # --- Subnet allocation ---
    # Count containers from compose template, allocate subnet from pool.
    container_count = len(get_compose_service_names(compose_template))
    # The registry tracks this stack's allocations, but not networks created by
    # another stack on the same host (parallel integration run, registry reset).
    # Merge live Docker-network subnets inside the pools so we never collide
    # with an existing network (Docker would fail: "Pool overlaps").
    allocated_subnets = subnet_manager.get_allocated_subnets(registry.get_all_users())
    allocated_subnets += subnet_manager.get_host_allocated_subnets()
    allocated = subnet_manager.allocate_subnet(
        container_count,
        allocated_subnets,
    )
    # No pool => NULL, not "" (T4/R2.4): the partial UNIQUE index on subnet is
    # `WHERE subnet IS NOT NULL`, so an empty string would collide across every
    # service on a pool-less install.
    _subnet = allocated["subnet"] if allocated else None
    _gateway = allocated["gateway"] if allocated else None

    # --- Persist the rendered-file metadata on the instance projection -------
    nginx_conf_paths = [o for _t, o in nginx_out_by_category.values()] or ([nginx_out] if nginx_out else [])
    nginx_conf_paths_by_category = {
        cat: o for cat, (_t, o) in nginx_out_by_category.items()
    }
    instance_fields: dict[str, Any] = {
        "hostname": template_engine.service_hostname(service_name, user_name, label, domain),
        "network_name": template_engine.user_network_name(service_name, user_name, label),
        "compose_template_path": compose_template,
        "compose_file_path": compose_out,
        "nginx_conf_template_path": nginx_template or "",
        "nginx_conf_path": nginx_out or "",
        "nginx_conf_paths": nginx_conf_paths,
        "nginx_conf_paths_by_category": nginx_conf_paths_by_category,
        "htpasswd_path": htpasswd_out or None,
        "ssl_certificate_path": ssl_certificate_path,
        "ssl_certificate_key_path": ssl_certificate_key_path,
        "env_file_path": env_file or "",
        "env_files": [],
        "profiles": list(profiles or []),
        "volumes": volumes,
        "build_args": build_args or {},
        "tls": https,
        "subnet": _subnet,
        "gateway": _gateway,
    }
    entry: dict[str, Any] = dict(entry_row)
    entry.update(instance_fields)
    entry["container_names"] = []

    with db.connection() as conn:
        registry.update_instance(conn, service_id, **instance_fields)
        basic_version = (existing_basic["basic_version"] if existing_basic else 0) or 0
        if passwd:
            registry.set_basic(
                conn, service_id, basic_username=user_name,
                secret_enc=keys.encrypt(passwd, version="v1").encode(),
                key_version=1, basic_version=max(basic_version + 1, 1),
                reset_required=False,
            )
        elif existing_basic and existing_basic["secret_enc"]:
            # Instance-only redeploy: re-derive .htpasswd from the stored
            # secret; never write a plaintext second copy.
            registry.set_basic(
                conn, service_id, basic_username=user_name,
                secret_enc=existing_basic["secret_enc"],
                key_version=existing_basic["key_version"],
                basic_version=max(basic_version, 1),
                reset_required=bool(existing_basic["reset_required"]),
            )
        else:
            registry.set_basic(
                conn, service_id, basic_username=user_name,
                secret_enc=None, key_version=None,
                basic_version=max(basic_version, 1), reset_required=False,
            )

    # --- Materialize declared service env files from .example siblings ---
    # Projects like dify ship envs/*/x.env.example ONLY — the declared
    # env_file entries (required: false) are silently SKIPPED by compose when
    # missing, which drops the DB/Redis config (DB_HOST etc.) and breaks the
    # app (postgres connection refused on localhost:5432).
    _materialize_env_examples(Path(compose_template).parent)

    # --- Render compose file → start containers (rollback on ANY failure) ---
    # A failure here KEEPS the entry (GRILLING G10/G17, corrected 2026-09-26):
    # `_mark_failed` records the reason on the entry and leaves it, so the card
    # survives with `Retry`/`Delete`. This comment used to claim the opposite —
    # that any exception after the entry was added must REMOVE it — which
    # contradicts the code below and the retry gate that replaces a `failed`
    # entry (`if status != "failed": raise ValueError(...)`, above).
    try:
        copied_env = _register_render_and_start(
            nginx_conf_pairs=nginx_out_by_category,
            service_id=service_id,
            user_name=user_name, service_name=service_name, label=label,
            entry=entry, compose_template=compose_template,
            compose_out=compose_out, volumes=volumes,
            env_file=env_file, env_files=env_files,
            service_env_files=service_env_files,
            nginx_template=nginx_template, nginx_out=nginx_out,
            htpasswd_out=htpasswd_out, passwd_hash=passwd_hash,
            domain=domain, https=https,
            ssl_certificate_path=ssl_certificate_path,
            ssl_certificate_key_path=ssl_certificate_key_path,
            build_args=build_args, profiles=profiles,
            _subnet=_subnet, _gateway=_gateway,
        )
    except RuntimeError as exc:
        # Rollback: tear down any partially-created Docker resources
        # (containers, networks) so a retry doesn't hit "already exists" errors.
        #
        # The registry entry is NOT removed: failure is instance-scoped state
        # (GRILLING G10), so it is UPDATED to `failed` with a bounded reason and
        # left for the ordinary delete path to clean up (G17). Removing it here
        # is what made a failed deploy indistinguishable from "nothing was ever
        # deployed" and put the Docker residue beyond the registry's reach.
        import logging
        _log = logging.getLogger(__name__)
        try:
            if Path(compose_out).exists():
                docker_ops.compose_down(compose_out, project_name=entry["network_name"])
        except Exception as down_err:
            _log.warning("Failed to clean up Docker resources after failed deploy: %s", down_err)
        _mark_failed(
            service_id,
            getattr(exc, "stage", "deploy"),
            str(exc),
            reason=getattr(exc, "reason", None),
        )
        raise
    except Exception as exc:
        # Broader path: no Docker teardown (nothing is known to have started),
        # but the instance still records the failure rather than vanishing.
        _mark_failed(service_id, "deploy", str(exc))
        raise

    # --- Mark as running (containers started successfully) ---
    _after_successful_transition(service_id, "running")

    # --- Connect provision-nginx to user network + reload ---
    net = entry["network_name"]
    docker_ops.network_connect(nginx_container, net)
    stream_access.refresh_and_reload(nginx_container)

    # The result is persisted on the task row: return the redacted projection,
    # never a plaintext credential (R4.4/R5.9).
    public_entry = registry.get_service_by_id(service_id) or entry
    return {
        "entry": public_entry,
        "volume_warnings": {"missing": missing_vols, "extra": extra_vols},
        "copied_env": copied_env,
        "service_id": service_id,
        "procedure_id": procedure_id,
    }


def _purge_user_artifacts(
    user_name: str, service_name: str, label: str, entry: dict
) -> None:
    """Delete every artifact belonging to one (user, service, label) deploy:
    per-user env copies / rendered compose / markers in the recipe dir, and the
    user_data volume tree. Original recipe files are untouched (matched by the
    ``.{user}.{label}`` / ``.user-{user}.{label}`` suffixes) — as are the
    recipe-level shared artifacts, which carry no per-user suffix and are
    therefore not this instance's to remove: the ``.fix`` family
    (``docker-compose.fix.<job>.yml``, ``nginx.fix.<job>.conf``, ``.env.fix``)
    and the generated ``.base`` family
    (``docker-compose.base.<job>.yml``, ``nginx.base.<job>.conf``) — the
    ``<job>`` there is a COLLISION TOKEN, not an owner (GRILLING G33): those
    files belong to the recipe and outlive the job that wrote them."""
    import shutil as _sh

    # 1) per-user files in the recipe (compose) directory.
    compose_dir = None
    for k in ("compose_file_path", "compose_template_path"):
        p = entry.get(k)
        if p:
            d = Path(p).parent
            if d.is_dir():
                compose_dir = d
                break
    if compose_dir:
        m1 = f".{user_name}.{label}"       # .env.raw.specialsync.0, .env.1.specialsync.0, redis.env.specialsync.0, …
        m2 = f".user-{user_name}.{label}"  # docker-compose.user-specialsync.0.yml
        # ONLY names carrying THIS instance's per-user suffix are purged.
        #
        # The recipe-level shared artifacts are deliberately NOT in the delete
        # set. `docker-compose.base.<job>.yml`, `nginx.base.<job>.conf`,
        # `docker-compose.fix.<job>.yml`, `nginx.fix.<job>.conf` and `.env.fix`
        # carry NO per-user suffix: they belong to the recipe, not to a deploy.
        # A deployment's stored selection hands one of them to the -api as
        # `nginx_conf_file_path`, and `.env.fix` is recipe-level by the same
        # argument — a genuine `fix` job's output, shared by the whole recipe
        # (generation_jobs.py: `_recipe_level_env`, FIX_FILENAME). A shared file
        # has no per-user ownership, so NO per-user operation may delete it:
        # removing one user's instance must never break another deployment whose
        # stored selection points at those files. Deleting them here made a
        # single user's remove 502 every other deployment on the recipe
        # (`nginx_conf_file_path not found: …/nginx.fix.conf`, found live).
        # The `<job>` in those names is the collision token (GRILLING G33), not
        # an owner, so it does NOT make the file per-user either.
        for f in list(compose_dir.iterdir()):
            name = f.name
            if (m1 in name or m2 in name) and f.is_file():
                try:
                    f.unlink(missing_ok=True)
                    Path(str(f) + ".generated").unlink(missing_ok=True)
                except OSError:
                    pass

    # 2) user_data volume tree for this instance (bind targets share the
    #    {user_data}/{user}/{service}/{label} root).
    vols = [Path(v) for v in (entry.get("volumes") or {}).values() if v]
    tree = None
    if vols:
        try:
            import os as _os
            tree = Path(_os.path.commonpath([str(v) for v in vols]))
        except ValueError:
            tree = None
    while tree is not None and tree != tree.parent and tree.name != label:
        tree = tree.parent
    if tree is not None and tree.exists() and tree.name == label:
        _sh.rmtree(tree, ignore_errors=True)


def _teardown_instance(entry: dict[str, Any], nginx_container: str) -> None:
    """Stop the instance's containers and remove ONLY its stream conf(s).

    The no-auto-delete rule (R2.6/B27): generated files, user data and named
    volumes are preserved on both delete scopes. The one exception is the
    per-instance stream conf — a leftover `proxy_pass` names a container that
    no longer exists, which makes the next nginx restart fail for every service.
    """
    _log_ = logging.getLogger(__name__)
    compose_file = entry.get("compose_file_path") or ""
    net = entry.get("network_name") or ""
    project_name = entry.get("network_name")
    if net:
        docker_ops.network_disconnect(nginx_container, net)
    if compose_file and Path(compose_file).exists():
        docker_ops.compose_down(
            compose_file, project_name=project_name, remove_volumes=False,
            profiles=list(entry.get("profiles") or []) or None,
        )
    elif project_name:
        _log_.warning("Compose file %s missing — down by project %s", compose_file, project_name)
        docker_ops.compose_down_by_project(project_name, remove_volumes=False)
    # Stream confs only.
    by_cat = entry.get("nginx_conf_paths_by_category") or {}
    paths = [by_cat.get("stream")] if isinstance(by_cat, dict) and by_cat.get("stream") else []
    for fpath in (entry.get("nginx_conf_paths") or []):
        if isinstance(fpath, str) and fpath.endswith(".stream.conf"):
            paths.append(fpath)
    for fpath in {p for p in paths if p}:
        for candidate in (fpath, fpath + ".generated", fpath + stream_access.PAUSED_SUFFIX):
            try:
                Path(candidate).unlink(missing_ok=True)
            except OSError:
                pass
    if net:
        try:
            docker_ops.orphan_network_cleanup(net, nginx_container)
        except Exception:
            pass


def remove_user(
    *,
    service_id: str | None = None,
    user_name: str | None = None,
    service_name: str | None = None,
    label: str | None = None,
    procedure_id: str | None = None,
    nginx_container: str = "subnet-acl-nginx",
    recovery: bool = False,
) -> dict[str, str]:
    """Full service delete: `deleting` -> tombstone (R6.3).

    With an instance row the status moves `deleting -> gone`, remembering the
    exact prior status/failure so a failed teardown restores it. Without an
    instance row (paused fix, pre-deploy-only) one transaction removes
    endpoints/Basic and writes the tombstone; a failure before commit leaves
    `services` live. Generated files/volumes are preserved except the stream
    conf.
    """
    sid = registry.resolve_service_id(
        service_id, user_name=user_name, service_name=service_name, label=label
    )
    if not sid:
        raise KeyError("No registration found for this service.")
    entry = registry.get_service_by_id(sid)
    if not entry or entry.get("deleted_at") is not None:
        raise KeyError("No active service found for this id.")
    status = entry.get("status")
    if status in ("building", "deleting") and not recovery:
        raise ValueError(f"service is {status}; cancel it before delete")
    proc = uuid.UUID(str(procedure_id)) if procedure_id else uuid.uuid4()

    if status is None:
        # Instance-less: one atomic tombstone transaction.
        with db.connection() as conn:
            registry.set_current_procedure(conn, uuid.UUID(sid), proc)
            registry.tombstone(conn, uuid.UUID(sid))
        return {"service_id": sid, "user_name": entry["user_name"],
                "service_name": entry["service_name"], "label": entry["label"]}

    if recovery and status == "deleting":
        # A crash left the instance mid-delete. Complete the same idempotent
        # teardown/tombstone; `previous_*` are already the restore point.
        previous_failure = entry.get("previous_failure")
        status = entry.get("previous_status") or "unknown"
    else:
        previous_failure = entry.get("failure")
    with db.connection() as conn:
        registry.update_instance(
            conn, sid, status="deleting", previous_status=status,
            previous_failure=previous_failure, current_procedure_id=proc,
        )
    try:
        _teardown_instance(entry, nginx_container)
        with db.connection() as conn:
            registry.tombstone(conn, uuid.UUID(sid))
        stream_access.refresh_and_reload(nginx_container)
    except Exception:
        with db.connection() as conn:
            registry.update_instance(
                conn, sid, status=status or "unknown",
                failure=previous_failure, previous_status=None, previous_failure=None,
            )
        raise
    return {"service_id": sid, "user_name": entry["user_name"],
            "service_name": entry["service_name"], "label": entry["label"]}


def remove_instance_only(
    *,
    service_id: str | None = None,
    user_name: str | None = None,
    service_name: str | None = None,
    label: str | None = None,
    procedure_id: str | None = None,
    nginx_container: str = "subnet-acl-nginx",
) -> dict[str, str]:
    """Fix / instance-only delete: instance + endpoints, `services` and Basic stay.

    Used by the paused-fix handoff. It creates no `deleting` card state and
    never tombstones the service, so the paused session's redeploy reuses the
    same identity and re-derives `.htpasswd` from `service_basic.secret_enc`.
    """
    sid = registry.resolve_service_id(
        service_id, user_name=user_name, service_name=service_name, label=label
    )
    if not sid:
        raise KeyError("No registration found for this service.")
    entry = registry.get_service_by_id(sid)
    if not entry or entry.get("status") is None:
        raise KeyError("No deployed instance for this service.")
    if entry["status"] in ("building", "deleting"):
        raise ValueError(f"service is {entry['status']}; cannot remove its instance")
    _teardown_instance(entry, nginx_container)
    with db.connection() as conn:
        if procedure_id:
            registry.set_current_procedure(conn, uuid.UUID(sid), uuid.UUID(str(procedure_id)))
        registry.delete_instance(conn, uuid.UUID(sid))
    stream_access.refresh_and_reload(nginx_container)
    return {"service_id": sid, "user_name": entry["user_name"],
            "service_name": entry["service_name"], "label": entry["label"]}


def remove_user_marked(**kwargs) -> dict[str, str]:
    """Full delete with the persisted `deleting` state (R6.3)."""
    return remove_user(**kwargs)


def rebuild_user(
    *,
    service_id: str | None = None,
    user_name: str | None = None,
    service_name: str | None = None,
    label: str | None = None,
    procedure_id: str | None = None,
    no_cache: bool = False,
    build_args: dict[str, str] | None = None,
    nginx_container: str = "subnet-acl-nginx",
) -> dict[str, str]:
    """Rebuild and restart a user's service containers.

    Steps
    -----
    1. ``docker compose build`` (optionally with ``--no-cache`` and ``--build-arg``).
    2. ``docker compose up``.

    Raises
    ------
    KeyError
        If no registration is found.
    FileNotFoundError
        If the rendered compose file is missing.
    RuntimeError
        If build or up fails.
    """
    sid = registry.resolve_service_id(
        service_id, user_name=user_name, service_name=service_name, label=label
    )
    entry = registry.get_service_by_id(sid) if sid else None
    if not entry:
        raise KeyError("No registration found for this service.")
    sid = entry["service_id"]
    user_name, service_name, label = entry["user_name"], entry["service_name"], entry["label"]

    compose_file = entry.get("compose_file_path", "")
    if not compose_file or not Path(compose_file).exists():
        raise FileNotFoundError(f"Compose file not found: {compose_file}")

    # Copy-if-empty bootstrap (self-heal): FILE→FILE bind targets must exist
    # as FILES before `up` — empty dir targets (e.g. left by an older deploy
    # that skipped the bootstrap) crash container entrypoints.
    template_path = entry.get("compose_template_path") or ""
    volumes = dict(entry.get("volumes") or {})
    if template_path and volumes and Path(template_path).exists():
        _bootstrap_volumes_from_template(volumes, template_path)

    # Re-apply the exact recorded settings (design G8/G17 — otherwise a
    # rebuild silently drops the user's profile-gated database).
    env_file = entry.get("env_file_path") or None
    env_files = list(entry.get("env_files") or [])
    profiles = list(entry.get("profiles") or [])
    if not env_files and env_file:
        env_files = [env_file]
    if not env_files:
        # Always-pass invariant: ensure the per-user env file exists (empty
        # when none was selected) so compose never auto-reads the recipe .env.
        empty_env = Path(compose_file).parent / f".env.{user_name}.{label}"
        if not empty_env.exists():
            empty_env.write_text("")
        env_files = [str(empty_env)]
    project_name = entry.get("network_name")
    # Use explicit build_args if provided; otherwise fall back to registry-stored ones
    if build_args is None:
        build_args = entry.get("build_args") or None

    with db.connection() as conn:
        fields: dict[str, Any] = {"status": "building", "failure": None}
        if procedure_id:
            fields["current_procedure_id"] = uuid.UUID(str(procedure_id))
        registry.update_instance(conn, sid, **fields)
    try:
        docker_ops.compose_build(compose_file, no_cache=no_cache, env_file=env_files, project_name=project_name, build_args=build_args, profiles=profiles)
        docker_ops.compose_up(compose_file, env_file=env_files, project_name=project_name, profiles=profiles)
    except Exception as exc:
        _mark_failed(sid, "rebuild", str(exc))
        raise
    _after_successful_transition(sid, "running")

    # G: a rebuild recreates the containers, and Docker may hand the same
    # container name a different address.  Stream confs point at the name with a
    # STATIC proxy_pass, which nginx resolves once at config load — so without a
    # reload here the route keeps dialling the old address until something else
    # reloads nginx.  Reload after compose up has completed and the new
    # endpoints are registered.
    stream_access.refresh_and_reload(nginx_container)

    return {"user_name": user_name, "service_name": service_name, "label": label}


# ---------------------------------------------------------------------------
# P5: Password change
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Start / Stop service
# ---------------------------------------------------------------------------

def start_service(
    *,
    service_id: str | None = None,
    user_name: str | None = None,
    service_name: str | None = None,
    label: str | None = None,
    nginx_container: str = "subnet-acl-nginx",
) -> dict[str, str]:
    """Start a user's service containers (docker compose up -d).

    Raises
    ------
    KeyError if no registration is found.
    FileNotFoundError if the compose file is missing.
    RuntimeError if docker compose up fails.
    """
    sid = registry.resolve_service_id(
        service_id, user_name=user_name, service_name=service_name, label=label
    )
    entry = registry.get_service_by_id(sid) if sid else None
    if not entry:
        raise KeyError("No registration found for this service.")
    user_name, service_name, label = entry["user_name"], entry["service_name"], entry["label"]

    compose_file = entry.get("compose_file_path", "")
    if not compose_file or not Path(compose_file).exists():
        raise FileNotFoundError(f"Compose file not found: {compose_file}")

    env_file = entry.get("env_file_path") or None
    env_files = list(entry.get("env_files") or [])
    profiles = list(entry.get("profiles") or [])
    if not env_files and env_file:
        env_files = [env_file]
    if not env_files:
        empty_env = Path(compose_file).parent / f".env.{user_name}.{label}"
        if not empty_env.exists():
            empty_env.write_text("")
        env_files = [str(empty_env)]
    project_name = entry.get("network_name")
    docker_ops.compose_up(compose_file, env_file=env_files, project_name=project_name, profiles=profiles)

    # R13 — the stream conf was renamed to `.stream.conf.paused` by
    # `stop_service`, because its static upstream would otherwise be a load-time
    # liability while the containers are down.  Bring it back BEFORE the reload
    # that follows compose up, so the route returns with the containers.  The
    # active file wins when both names exist; the conflict is reported and the
    # paused copy is left untouched.
    restored = stream_access.restore_stream_confs(entry)
    if restored["conflicts"] or restored["failed"]:
        _log.warning(
            "stream restore for %s/%s/%s: conflicts=%s failed=%s",
            user_name, service_name, label,
            restored["conflicts"], restored["failed"],
        )

    # Same reason as rebuild_user: compose up may recreate containers, so the
    # static upstream names must be resolved again by a reload.
    stream_access.refresh_and_reload(nginx_container)

    return {
        "user_name": user_name, "service_name": service_name, "label": label,
        "stream_confs_restored": len(restored["restored"]),
        "stream_conf_conflicts": restored["conflicts"],
    }


def stop_service(
    *,
    service_id: str | None = None,
    user_name: str | None = None,
    service_name: str | None = None,
    label: str | None = None,
    nginx_container: str = "subnet-acl-nginx",
) -> dict[str, Any]:
    """Stop a user's service containers (docker compose stop).

    R13 — the instance's stream conf leaves the served set for as long as the
    instance is stopped: its static upstream names a container that is about to
    be down, and the internal nginx resolves that name at config load, so a
    later reload (or a restart) would fail for EVERY service.  Each recorded
    stream conf is renamed to `<name>.stream.conf.paused` BEFORE the containers
    stop, and nginx is reloaded while they are still healthy.  The registry
    entry and the file are kept — this is a pause, not a deletion; `start_service`
    restores it.

    Raises
    ------
    KeyError if no registration is found.
    FileNotFoundError if the compose file is missing.
    RuntimeError if docker compose stop fails.
    """
    sid = registry.resolve_service_id(
        service_id, user_name=user_name, service_name=service_name, label=label
    )
    entry = registry.get_service_by_id(sid) if sid else None
    if not entry:
        raise KeyError("No registration found for this service.")
    user_name, service_name, label = entry["user_name"], entry["service_name"], entry["label"]

    compose_file = entry.get("compose_file_path", "")
    if not compose_file or not Path(compose_file).exists():
        raise FileNotFoundError(f"Compose file not found: {compose_file}")

    env_file = entry.get("env_file_path") or None
    env_files = list(entry.get("env_files") or [])
    profiles = list(entry.get("profiles") or [])
    if not env_files and env_file:
        env_files = [env_file]

    # No stream conf → this rule does not apply at all (no rename, no reload).
    stream_paths = stream_access.stream_conf_paths(entry)
    paused: dict[str, object] = {"changed": [], "already": [], "failed": [], "conflicts": []}
    if stream_paths:
        # Rename while the containers are still up, then reload: nginx drops
        # the route from a config that no longer names the upstream.
        paused = stream_access.pause_stream_confs(entry)
        if paused["failed"] or paused["conflicts"]:
            # A failed rename is reported and the stop is NOT claimed as paused.
            # Reloading now would load the conf that is still there, so skip it.
            _log.error(
                "stream pause for %s/%s/%s: failed=%s conflicts=%s — the stop "
                "is not paused; the stream conf still names a container that is "
                "about to stop",
                user_name, service_name, label,
                paused["failed"], paused["conflicts"],
            )
        elif paused["changed"]:
            # Only a batch that actually renamed something changes what nginx
            # serves; a second stop (already paused) is a no-op.
            stream_access.refresh_and_reload(nginx_container)

    project_name = entry.get("network_name")
    docker_ops.compose_stop(compose_file, env_file=env_files or None, project_name=project_name, profiles=profiles)

    return {
        "user_name": user_name, "service_name": service_name, "label": label,
        "stream_confs_paused": len(paused["changed"]),
        "stream_confs_already_paused": len(paused["already"]),
        "stream_conf_conflicts": paused["conflicts"],
        "stream_conf_failures": paused["failed"],
    }


# ---------------------------------------------------------------------------
# P5: Password change
# ---------------------------------------------------------------------------

def change_password(
    *,
    service_id: str | None = None,
    user_name: str | None = None,
    service_name: str | None = None,
    label: str | None = None,
    passwd: str,
    nginx_container: str = "subnet-acl-nginx",
) -> dict[str, str]:
    """Change a service's Basic password (R6.5).

    The plaintext is encrypted into ``service_basic.secret_enc`` with the
    keyring and the derived ``.htpasswd`` is ``<basic_username>:<hash>``. Both
    halves come from the same stored ``basic_username``, so the gateway's
    ``X-Service-Basic`` and the instance's auth_basic always agree. The account
    password is separate.
    """
    sid = registry.resolve_service_id(
        service_id, user_name=user_name, service_name=service_name, label=label
    )
    entry = registry.get_service_by_id(sid) if sid else None
    if not entry:
        raise KeyError("No registration found for this service.")
    sid = entry["service_id"]
    user_name, service_name, label = entry["user_name"], entry["service_name"], entry["label"]
    if not passwd:
        raise ValueError("passwd must be non-empty (use remove_password to drop protection)")

    htpasswd_path = entry.get("htpasswd_path") or str(
        Path(entry.get("nginx_conf_path") or ".").with_name(
            f"{service_name}.user-{user_name}.{label}.htpasswd"
        )
    )
    # Capture the previous state so a FAILED reload restores it: the operation
    # must not report a password that nginx never loaded (and must not silently
    # half-apply one).
    old_htpasswd = Path(htpasswd_path).read_bytes() if Path(htpasswd_path).exists() else None
    with db.connection() as conn:
        _old = conn.execute(
            "SELECT basic_version, secret_enc, key_version, reset_required "
            "FROM registry.service_basic WHERE service_id=%s", (sid,)
        ).fetchone()
    old_basic = dict(_old) if _old else None

    passwd_hash = auth.hash_password(user_name, passwd)
    auth.write_htpasswd_file(htpasswd_path, user_name, passwd_hash)
    Path(htpasswd_path + ".generated").write_text("")

    with db.connection() as conn:
        row = conn.execute(
            "SELECT basic_version FROM registry.service_basic WHERE service_id=%s", (sid,)
        ).fetchone()
        version = int(row["basic_version"]) if row else 0
        registry.set_basic(
            conn, uuid.UUID(sid), basic_username=user_name,
            secret_enc=keys.encrypt(passwd, version="v1").encode(),
            key_version=1, basic_version=max(version + 1, 1), reset_required=False,
        )
        registry.update_instance(conn, sid, htpasswd_path=str(htpasswd_path))

    try:
        stream_access.refresh_and_reload(nginx_container)
    except Exception:
        # Roll the credential back with the conf: nginx is still serving the
        # previous state, so the record and the secret file must match it.
        with db.connection() as conn:
            if old_basic is not None:
                registry.set_basic(
                    conn, uuid.UUID(sid), basic_username=user_name,
                    secret_enc=old_basic.get("secret_enc"),
                    key_version=old_basic.get("key_version"),
                    basic_version=int(old_basic.get("basic_version") or 0),
                    reset_required=bool(old_basic.get("reset_required")),
                )
            registry.update_instance(conn, sid, htpasswd_path=str(htpasswd_path))
        if old_htpasswd is None:
            Path(htpasswd_path).unlink(missing_ok=True)
            Path(htpasswd_path + ".generated").unlink(missing_ok=True)
        else:
            Path(htpasswd_path).write_bytes(old_htpasswd)
        raise
    return {"service_id": sid, "user_name": user_name,
            "service_name": service_name, "label": label}


def remove_password(
    *,
    service_id: str | None = None,
    user_name: str | None = None,
    service_name: str | None = None,
    label: str | None = None,
    nginx_container: str = "subnet-acl-nginx",
) -> dict[str, str]:
    """Remove a service's password protection and reload nginx.

    Order matters. The conf is rendered and RELOADED first; only after the
    reload succeeds are the registry and the htpasswd file changed. If the
    render/reload fails, the previous conf is restored and the registry and
    secret file are untouched, so the instance is exactly as it was.

    This is the exit for a protected service whose plaintext is gone: bcrypt is
    one-way, so the original password cannot be recovered and the gate can never
    satisfy that ``auth_basic`` again. The service KEEPS its ACL gate — only the
    instance's own second layer is dropped.

    Raises
    ------
    KeyError
        If no registration is found.
    """
    sid = registry.resolve_service_id(
        service_id, user_name=user_name, service_name=service_name, label=label
    )
    entry = registry.get_service_by_id(sid) if sid else None
    if not entry:
        raise KeyError("No registration found for this service.")
    sid = entry["service_id"]
    user_name, service_name, label = entry["user_name"], entry["service_name"], entry["label"]

    htpasswd_path = entry.get("htpasswd_path") or ""
    nginx_tpl = entry.get("nginx_conf_template_path") or ""
    nginx_out = entry.get("nginx_conf_path") or ""

    old_conf: str | None = None
    if nginx_tpl and nginx_out:
        # Same derivation as reconciliation.regenerate_nginx_confs:
        # hostname = f"{service}-{user}-{label}.{domain}".
        domain = "localhost"
        hostname = entry.get("hostname", "")
        if hostname and "." in hostname:
            domain = hostname.split(".", 1)[1]
        out_path = Path(nginx_out)
        if out_path.exists():
            old_conf = out_path.read_text(encoding="utf-8")
        rendered = template_engine.render_nginx_conf(
            nginx_tpl, nginx_out,
            user_name, service_name, label,
            domain, "",  # no htpasswd -> the render strips auth_basic
            https=registry.entry_tls(entry),
            ssl_certificate_path=entry.get("ssl_certificate_path") or "",
            ssl_certificate_key_path=entry.get("ssl_certificate_key_path") or "",
            write=False,
        )
        out_path.write_text(rendered, encoding="utf-8")
        Path(str(out_path) + ".generated").write_text("")

    # Reload the new conf (and refresh the central policy) BEFORE any registry
    # or secret mutation. If this raises, restore the previous conf so the disk
    # matches the still-running nginx; the registry and htpasswd are untouched.
    try:
        stream_access.refresh_and_reload(nginx_container)
    except Exception:
        if nginx_out:
            out_path = Path(nginx_out)
            if old_conf is not None:
                try:
                    out_path.write_text(old_conf, encoding="utf-8")
                except OSError:
                    pass
            else:
                try:
                    out_path.unlink()
                    Path(str(out_path) + ".generated").unlink(missing_ok=True)
                except OSError:
                    pass
        raise

    # --- Registry: only after the served conf changed successfully ---
    with db.connection() as conn:
        row = conn.execute(
            "SELECT basic_version FROM registry.service_basic WHERE service_id=%s", (sid,)
        ).fetchone()
        version = int(row["basic_version"]) if row else 0
        registry.set_basic(
            conn, uuid.UUID(sid), basic_username=user_name, secret_enc=None,
            key_version=None, basic_version=max(version + 1, 1), reset_required=False,
        )
        registry.update_instance(conn, sid, htpasswd_path=None)

    # --- The htpasswd file itself (never leave a served secret behind) ---
    if htpasswd_path:
        for path in (htpasswd_path, htpasswd_path + ".generated"):
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass

    return {"user_name": user_name, "service_name": service_name, "label": label}

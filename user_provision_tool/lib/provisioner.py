"""lib/provisioner.py — core provisioning workflow shared by CLI and API.

All three operations (register, remove, rebuild) are implemented here so that
both ``cli/`` scripts and ``api.py`` delegate to the same logic without
duplicating it.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from . import auth, docker_ops, registry, subnet_manager, template_engine
from .compose_converter import ensure_subnet_ipam_block, get_compose_service_names

# Registry writes are serialised by lib.registry.transaction() — Tier 1
# (in-process RLock) + Tier 2 (cross-process flock on the registry sidecar
# lock file) — around every read-modify-write.


def _update_registry_status(user_name: str, service_name: str, label: str, status: str) -> None:
    """Update the status field of a registry entry (building → running).

    Used so the API can report accurate service state during long builds.
    Thread-safe and process-safe: runs under registry.transaction().
    """
    with registry.transaction():
        users = registry._load()
        matched = False
        for u in users:
            if (
                u.get("user_name") == user_name
                and u.get("service_name") == service_name
                and str(u.get("label", "")) == str(label)
            ):
                u["status"] = status
                matched = True
                break
        # Save ONLY on a real match: writing back an unchanged (possibly empty)
        # list is how an empty registry could be persisted over real data.
        if matched:
            registry._save(users)


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
    *,
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
    copied_env = template_engine.render_compose(
        compose_template, compose_out,
        user_name, service_name, label, volumes,
        env_file=_all_env[0] if _all_env else None,
        env_files_all=_all_env or None,
        env_files=service_env_files or None,
        subnet=_subnet,
        gateway=_gateway,
    )

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

    # Update registry with per-user copied env path + container names
    # so rebuild/remove/reconciliation always have the correct references.
    if copied_env:
        entry["env_file_path"] = copied_env
    with registry.transaction():
        users = registry._load()
        for u in users:
            if (
                u.get("user_name") == user_name
                and u.get("service_name") == service_name
                and str(u.get("label", "")) == str(label)
            ):
                if copied_env:
                    u["env_file_path"] = copied_env
                u["env_files"] = _env_files_used
                u["container_names"] = container_names
                break
        registry._save(users)

    # --- Render nginx conf + htpasswd ---
    if nginx_template and nginx_out:
        if htpasswd_out:
            auth.write_htpasswd_file(htpasswd_out, user_name, passwd_hash)
            # Mark htpasswd as generated
            Path(htpasswd_out + ".generated").write_text("")
        template_engine.render_nginx_conf(
            nginx_template, nginx_out,
            user_name, service_name, label,
            domain, htpasswd_out or "",
            https=https,
            ssl_certificate_path=ssl_certificate_path,
            ssl_certificate_key_path=ssl_certificate_key_path,
        )

    # --- Mark as building (container not yet started) ---
    _update_registry_status(user_name, service_name, label, "building")

    # --- Start containers ---
    # Profiles: explicit per-user --profile flags; default (none) activates
    # no profile plus the implicit "" services.
    if build_args:
        docker_ops.compose_build(compose_out, env_file=_env_files_used, project_name=entry["network_name"], build_args=build_args, profiles=profiles)
    docker_ops.compose_up(compose_out, env_file=_env_files_used, project_name=entry["network_name"], profiles=profiles)
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
        result[key] = str(vol_dir)
    return result


def register_user(
    *,
    user_name: str,
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
    user_data_dir: str | Path | None = None,
    build_args: dict[str, str] | None = None,
    https: bool = False,
    fullchain: str | None = None,
    privkey: str | None = None,
    ssl_base_dir: str = "/provision/ssl",
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

    # --- Hash password ---
    passwd_hash = auth.hash_password(user_name, passwd) if passwd else ""

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
    if nginx_template:
        nginx_out = str(nginx_dir / f"{service_name}.user-{user_name}.{label}.nginx.conf")
        if passwd_hash:
            htpasswd_out = str(nginx_dir / f"{service_name}.user-{user_name}.{label}.htpasswd")

    # --- Gap 8: auto re-convert old compose templates when subnet pools are
    # enabled. If the template lacks the {% if subnet %} ipam block, the
    # allocation below would reserve a subnet that never renders (Docker then
    # auto-assigns → allocation leak). Re-convert (back up as .bak) first.
    # Re-load env so tests that set SUBNET_POOLS after import are honored.
    subnet_manager._load_env()
    if subnet_manager.SUBNET_POOLS:
        ensure_subnet_ipam_block(compose_template)

    # --- Subnet allocation ---
    # Count containers from compose template, allocate subnet from pool.
    container_count = len(get_compose_service_names(compose_template))
    # The registry tracks this stack's allocations, but not networks created by
    # another stack on the same host (parallel integration run, registry reset).
    # Merge live Docker-network subnets inside the pools so we never collide
    # with an existing network (Docker would fail: "Pool overlaps").
    allocated_subnets = subnet_manager.get_allocated_subnets(registry._load())
    allocated_subnets += subnet_manager.get_host_allocated_subnets()
    allocated = subnet_manager.allocate_subnet(
        container_count,
        allocated_subnets,
    )
    _subnet = allocated["subnet"] if allocated else ""
    _gateway = allocated["gateway"] if allocated else ""

    # --- Registry entry (design §Implementation notes L287-288: profiles,
    # interpolation env_files order and service_env_files are recorded so
    # rebuild/up/down/reconciliation re-apply the exact settings) ---
    entry: dict[str, Any] = {
        "user_name": user_name,
        "passwd": passwd_hash,
        "service_name": service_name,
        "label": label,
        "network_name": template_engine.user_network_name(service_name, user_name, label),
        "compose_template_path": compose_template,
        "nginx_conf_template_path": nginx_template,
        "env_file_path": env_file,
        "env_files": [],  # filled after render (order preserved)
        "profiles": list(profiles or []),
        "service_env_files": [],
        "compose_sources": list(compose_sources or []),
        "compose_file_path": compose_out,
        "nginx_conf_path": nginx_out,
        "htpasswd_path": htpasswd_out,
        "volumes": volumes,
        "build_args": build_args or {},
        "https": https,
        "ssl_certificate_path": ssl_certificate_path,
        "ssl_certificate_key_path": ssl_certificate_key_path,
        "hostname": template_engine.service_hostname(service_name, user_name, label, domain),
        "passwd_plain": passwd,
        "subnet": _subnet,
        "gateway": _gateway,
    }

    # --- Record declared service env files that exist in the recipe
    # (copied per-user-per-recipe at render time; missing stay missing) ---
    for _key, _declared in template_engine.extract_template_env_file_sources(compose_template).items():
        if (Path(compose_template).parent / _declared).is_file():
            entry["service_env_files"].append(_declared)

    # Duplicate check + add are atomic to prevent concurrent registrations
    with registry.transaction():
        if registry.get_user_service(user_name, service_name, label):
            raise ValueError(
                f"User '{user_name}' with service '{service_name}' "
                f"and label '{label}' is already registered."
            )
        registry.add_user(entry)

    # --- Materialize declared service env files from .example siblings ---
    # Projects like dify ship envs/*/x.env.example ONLY — the declared
    # env_file entries (required: false) are silently SKIPPED by compose when
    # missing, which drops the DB/Redis config (DB_HOST etc.) and breaks the
    # app (postgres connection refused on localhost:5432).
    _materialize_env_examples(Path(compose_template).parent)

    # --- Render compose file → start containers (rollback on ANY failure) ---
    # Any exception after the registry entry is added must remove the entry —
    # a stale entry (e.g. from a Jinja render error caused by an incomplete
    # volume mapping) blocks every retry with "already registered".
    try:
        copied_env = _register_render_and_start(
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
    except RuntimeError:
        # Rollback: tear down any partially-created Docker resources
        # (containers, networks) so a retry doesn't hit "already exists" errors,
        # then remove the registry entry.
        import logging
        _log = logging.getLogger(__name__)
        try:
            if Path(compose_out).exists():
                docker_ops.compose_down(compose_out, project_name=entry["network_name"])
        except Exception as down_err:
            _log.warning("Failed to clean up Docker resources after failed deploy: %s", down_err)
        with registry.transaction():
            registry.remove_user_service(user_name, service_name, label)
        raise
    except Exception:
        with registry.transaction():
            registry.remove_user_service(user_name, service_name, label)
        raise

    # --- Mark as running (containers started successfully) ---
    _update_registry_status(user_name, service_name, label, "running")

    # --- Connect provision-nginx to user network + reload ---
    net = entry["network_name"]
    docker_ops.network_connect(nginx_container, net)
    docker_ops.nginx_reload(nginx_container)

    return {
        "entry": entry,
        "volume_warnings": {"missing": missing_vols, "extra": extra_vols},
        "copied_env": copied_env,
    }


def _purge_user_artifacts(
    user_name: str, service_name: str, label: str, entry: dict
) -> None:
    """Delete every artifact belonging to one (user, service, label) deploy:
    per-user env copies / rendered compose / markers in the recipe dir, and the
    user_data volume tree. Original recipe files are untouched (matched by the
    ``.{user}.{label}`` / ``.user-{user}.{label}`` suffixes)."""
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


def remove_user(
    *,
    user_name: str,
    service_name: str,
    label: str,
    nginx_container: str = "subnet-acl-nginx",
) -> dict[str, str]:
    """Stop containers and remove a user's service registration.

    Steps
    -----
    1. Disconnect provision-nginx from the user's network.
    2. ``docker compose down`` (falls back to project-name if compose file missing).
    3. Remove generated files (nginx conf, htpasswd, compose file).
    4. Reload provision-nginx.
    5. Remove registry entry.

    Raises
    ------
    KeyError
        If no registration is found.
    RuntimeError
        If ``docker compose down`` fails.
    """
    import logging
    _log = logging.getLogger(__name__)

    entry = registry.get_user_service(user_name, service_name, label)
    if not entry:
        raise KeyError(
            f"No registration found for {user_name}/{service_name}/{label}."
        )

    compose_file = entry.get("compose_file_path", "")
    net = entry.get("network_name", "")
    project_name = entry.get("network_name")

    # Resolve env_file to absolute path (registry stores it relative to project)
    env_file = entry.get("env_file_path") or None
    if env_file and not Path(env_file).is_absolute():
        # Reconstruct absolute path from the compose template directory
        compose_tpl = entry.get("compose_template_path", "")
        if compose_tpl:
            env_file = str(Path(compose_tpl).parent / env_file)

    # Disconnect nginx before compose_down so Docker can remove the network
    if net:
        docker_ops.network_disconnect(nginx_container, net)

    # Tear down containers: prefer compose file, fall back to project name.
    # NOTE: --env-file is intentionally NOT passed to compose_down — it is only
    # needed for variable substitution during 'up', and a missing env file should
    # never block teardown.
    compose_exists = compose_file and Path(compose_file).exists()
    if compose_exists:
        docker_ops.compose_down(compose_file, project_name=project_name)
    elif project_name:
        _log.warning(
            "Compose file %s not found for %s/%s/%s — attempting down by project name %s",
            compose_file, user_name, service_name, label, project_name,
        )
        docker_ops.compose_down_by_project(project_name)
    else:
        _log.warning(
            "No compose file or project name for %s/%s/%s — skipping container teardown",
            user_name, service_name, label,
        )

    # Remove generated files
    for key in ("compose_file_path", "nginx_conf_path", "htpasswd_path"):
        fpath = entry.get(key, "")
        if fpath:
            try:
                Path(fpath).unlink(missing_ok=True)
                Path(str(fpath) + ".generated").unlink(missing_ok=True)
            except OSError:
                pass

    # Full purge (decision #2): a deleted service leaves NOTHING behind —
    # per-user artifacts in the recipe dir + the user_data volume tree.
    _purge_user_artifacts(user_name, service_name, label, entry)

    # P1: Orphan network cleanup — if network still exists after compose_down, clean it up
    if net:
        try:
            docker_ops.orphan_network_cleanup(net, nginx_container)
        except Exception:
            pass

    docker_ops.nginx_reload(nginx_container)

    with registry.transaction():
        registry.remove_user_service(user_name, service_name, label)

    return {"user_name": user_name, "service_name": service_name, "label": label}


def remove_user_marked(**kwargs) -> dict[str, str]:
    """``remove_user`` with a persisted ``deleting`` registry status.

    The status is written BEFORE the teardown starts so the dashboard keeps
    showing "Deleting…" across page refreshes (mirrors the ``building`` status
    written on register/rebuild). ``remove_user`` drops the registry entry on
    success; on failure the status is reset so a card cannot stay stuck.
    """
    user_name = kwargs.get("user_name", "")
    service_name = kwargs.get("service_name", "")
    label = str(kwargs.get("label", ""))
    try:
        _update_registry_status(user_name, service_name, label, "deleting")
    except Exception:
        pass
    try:
        return remove_user(**kwargs)
    except Exception:
        try:
            _update_registry_status(user_name, service_name, label, "running")
        except Exception:
            pass
        raise


def rebuild_user(
    *,
    user_name: str,
    service_name: str,
    label: str,
    no_cache: bool = False,
    build_args: dict[str, str] | None = None,
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
    entry = registry.get_user_service(user_name, service_name, label)
    if not entry:
        raise KeyError(
            f"No registration found for {user_name}/{service_name}/{label}."
        )

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

    _update_registry_status(user_name, service_name, label, "building")
    try:
        docker_ops.compose_build(compose_file, no_cache=no_cache, env_file=env_files, project_name=project_name, build_args=build_args, profiles=profiles)
        docker_ops.compose_up(compose_file, env_file=env_files, project_name=project_name, profiles=profiles)
    except Exception:
        _update_registry_status(user_name, service_name, label, "error")
        raise
    _update_registry_status(user_name, service_name, label, "running")

    return {"user_name": user_name, "service_name": service_name, "label": label}


# ---------------------------------------------------------------------------
# P5: Password change
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Start / Stop service
# ---------------------------------------------------------------------------

def start_service(
    *,
    user_name: str,
    service_name: str,
    label: str,
) -> dict[str, str]:
    """Start a user's service containers (docker compose up -d).

    Raises
    ------
    KeyError if no registration is found.
    FileNotFoundError if the compose file is missing.
    RuntimeError if docker compose up fails.
    """
    entry = registry.get_user_service(user_name, service_name, label)
    if not entry:
        raise KeyError(f"No registration found for {user_name}/{service_name}/{label}.")

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

    return {"user_name": user_name, "service_name": service_name, "label": label}


def stop_service(
    *,
    user_name: str,
    service_name: str,
    label: str,
) -> dict[str, str]:
    """Stop a user's service containers (docker compose stop).

    Raises
    ------
    KeyError if no registration is found.
    FileNotFoundError if the compose file is missing.
    RuntimeError if docker compose stop fails.
    """
    entry = registry.get_user_service(user_name, service_name, label)
    if not entry:
        raise KeyError(f"No registration found for {user_name}/{service_name}/{label}.")

    compose_file = entry.get("compose_file_path", "")
    if not compose_file or not Path(compose_file).exists():
        raise FileNotFoundError(f"Compose file not found: {compose_file}")

    env_file = entry.get("env_file_path") or None
    env_files = list(entry.get("env_files") or [])
    profiles = list(entry.get("profiles") or [])
    if not env_files and env_file:
        env_files = [env_file]
    project_name = entry.get("network_name")
    docker_ops.compose_stop(compose_file, env_file=env_files or None, project_name=project_name, profiles=profiles)

    return {"user_name": user_name, "service_name": service_name, "label": label}


# ---------------------------------------------------------------------------
# P5: Password change
# ---------------------------------------------------------------------------

def change_password(
    *,
    user_name: str,
    service_name: str,
    label: str,
    passwd: str,
    nginx_container: str = "subnet-acl-nginx",
) -> dict[str, str]:
    """Change a user's htpasswd password and reload nginx.

    Steps
    -----
    1. Look up registration entry.
    2. Re-hash new password.
    3. Re-write .htpasswd file.
    4. Update registry entry with new hash.
    5. Reload nginx.

    Raises
    ------
    KeyError
        If no registration is found.
    FileNotFoundError
        If the htpasswd file path is missing or the file doesn't exist.
    """
    import logging
    _log = logging.getLogger(__name__)

    entry = registry.get_user_service(user_name, service_name, label)
    if not entry:
        raise KeyError(
            f"No registration found for {user_name}/{service_name}/{label}."
        )

    htpasswd_path = entry.get("htpasswd_path", "")
    if not htpasswd_path:
        raise FileNotFoundError(
            f"No htpasswd file path in registry for {user_name}/{service_name}/{label}."
        )
    if not Path(htpasswd_path).exists():
        raise FileNotFoundError(f"htpasswd file not found: {htpasswd_path}")

    passwd_hash = auth.hash_password(user_name, passwd)
    auth.write_htpasswd_file(htpasswd_path, user_name, passwd_hash)

    # Update registry
    with registry.transaction():
        users = registry._load()
        for u in users:
            if (
                u.get("user_name") == user_name
                and u.get("service_name") == service_name
                and str(u.get("label", "")) == str(label)
            ):
                u["passwd"] = passwd_hash
                break
        registry._save(users)

    docker_ops.nginx_reload(nginx_container)

    return {"user_name": user_name, "service_name": service_name, "label": label}

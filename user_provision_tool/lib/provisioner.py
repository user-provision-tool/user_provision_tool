"""lib/provisioner.py — core provisioning workflow shared by CLI and API.

All three operations (register, remove, rebuild) are implemented here so that
both ``cli/`` scripts and ``api.py`` delegate to the same logic without
duplicating it.
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Any

from . import auth, docker_ops, registry, template_engine
from .compose_converter import get_compose_service_names

# Registry writes must be atomic across threads (relevant when the API handles
# concurrent requests).
_registry_lock = threading.Lock()


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

    The returned dict can be passed directly to ``register_user`` as ``volumes``.
    """
    keys = template_engine.extract_template_volumes(compose_template)
    base = user_data_dir / user_name / service_name / label
    result: dict[str, str] = {}
    for key in keys:
        vol_dir = base / key
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
    nginx_container: str = "provision-nginx",
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

    # --- Volume cross-check (informational; callers decide how to surface) ---
    expected_vols = template_engine.extract_template_volumes(compose_template)
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
        _fc = Path(fullchain).resolve()
        if _fc.is_absolute() or "/" in str(fullchain):
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
        _pk = Path(privkey).resolve()
        if _pk.is_absolute() or "/" in str(privkey):
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

    # --- Registry entry ---
    entry: dict[str, Any] = {
        "user_name": user_name,
        "passwd": passwd_hash,
        "service_name": service_name,
        "label": label,
        "network_name": template_engine.user_network_name(service_name, user_name, label),
        "compose_template_path": compose_template,
        "nginx_conf_template_path": nginx_template,
        "env_file_path": env_file,
        "compose_file_path": compose_out,
        "nginx_conf_path": nginx_out,
        "htpasswd_path": htpasswd_out,
        "volumes": volumes,
        "build_args": build_args or {},
        "https": https,
        "ssl_certificate_path": ssl_certificate_path,
        "ssl_certificate_key_path": ssl_certificate_key_path,
    }

    # Duplicate check + add are atomic to prevent concurrent registrations
    with _registry_lock:
        if registry.get_user_service(user_name, service_name, label):
            raise ValueError(
                f"User '{user_name}' with service '{service_name}' "
                f"and label '{label}' is already registered."
            )
        registry.add_user(entry)

    # --- Render compose file ---
    copied_env = template_engine.render_compose(
        compose_template, compose_out,
        user_name, service_name, label, volumes,
        env_file=env_file,
    )

    # --- Record container names from the rendered compose ---
    prefix = template_engine.container_prefix(service_name, user_name, label)
    compose_svc_names = get_compose_service_names(compose_out)
    container_names = [f"{prefix}{svc}" for svc in compose_svc_names]
    entry["container_names"] = container_names

    # Update registry with per-user copied env path + container names
    # so rebuild/remove/reconciliation always have the correct references.
    if copied_env:
        entry["env_file_path"] = copied_env
    with _registry_lock:
        users = registry._load()
        for u in users:
            if (
                u.get("user_name") == user_name
                and u.get("service_name") == service_name
                and str(u.get("label", "")) == str(label)
            ):
                if copied_env:
                    u["env_file_path"] = copied_env
                u["container_names"] = container_names
                break
        registry._save(users)

    # --- Render nginx conf + htpasswd ---
    if nginx_template and nginx_out:
        if htpasswd_out:
            auth.write_htpasswd_file(htpasswd_out, user_name, passwd_hash)
        template_engine.render_nginx_conf(
            nginx_template, nginx_out,
            user_name, service_name, label,
            domain, htpasswd_out or "",
            https=https,
            ssl_certificate_path=ssl_certificate_path,
            ssl_certificate_key_path=ssl_certificate_key_path,
        )

    # --- Start containers ---
    try:
        if build_args:
            docker_ops.compose_build(compose_out, env_file=copied_env, project_name=entry["network_name"], build_args=build_args)
        docker_ops.compose_up(compose_out, env_file=copied_env, project_name=entry["network_name"])
    except RuntimeError:
        # Rollback: remove registry entry so the caller can retry
        with _registry_lock:
            registry.remove_user_service(user_name, service_name, label)
        raise

    # --- Connect provision-nginx to user network + reload ---
    net = entry["network_name"]
    docker_ops.network_connect(nginx_container, net)
    docker_ops.nginx_reload(nginx_container)

    return {
        "entry": entry,
        "volume_warnings": {"missing": missing_vols, "extra": extra_vols},
        "copied_env": copied_env,
    }


def remove_user(
    *,
    user_name: str,
    service_name: str,
    label: str,
    nginx_container: str = "provision-nginx",
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
            except OSError:
                pass

    # P1: Orphan network cleanup — if network still exists after compose_down, clean it up
    if net:
        try:
            docker_ops.orphan_network_cleanup(net, nginx_container)
        except Exception:
            pass

    docker_ops.nginx_reload(nginx_container)

    with _registry_lock:
        registry.remove_user_service(user_name, service_name, label)

    return {"user_name": user_name, "service_name": service_name, "label": label}


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

    env_file = entry.get("env_file_path") or None
    project_name = entry.get("network_name")
    # Use explicit build_args if provided; otherwise fall back to registry-stored ones
    if build_args is None:
        build_args = entry.get("build_args") or None
    docker_ops.compose_build(compose_file, no_cache=no_cache, env_file=env_file, project_name=project_name, build_args=build_args)
    docker_ops.compose_up(compose_file, env_file=env_file, project_name=project_name)

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
    project_name = entry.get("network_name")
    docker_ops.compose_up(compose_file, env_file=env_file, project_name=project_name)

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
    project_name = entry.get("network_name")
    docker_ops.compose_stop(compose_file, env_file=env_file, project_name=project_name)

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
    nginx_container: str = "provision-nginx",
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
    with _registry_lock:
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

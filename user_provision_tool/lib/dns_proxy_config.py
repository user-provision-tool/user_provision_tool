"""Per-instance DNS-proxy configuration and compose injection (G101).

This module owns the proxy's side of the conversion:

* ``inject_compose`` — post-render transform of one per-user compose: the
  ``dns-proxy`` sub-service, the app services' ``dns``/``depends_on``, and the
  tenant network's reserved proxy address + dynamic ``ip_range``.
* the proxy config file ``<user_data>/<user>/<service>/<label>/dns-proxy/
  dns-proxy.json`` and its atomic writes;
* the -api control plane: write the config and (dis)connect the proxy and the
  app containers to/from the cross network;
* the startup pass and the start-event membership watcher.

No ``/etc/resolv.conf`` is ever edited here (G101 retires that path).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import queue
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from . import db, docker_ops, registry

_log = logging.getLogger("provision-api.dns_proxy_config")

PROXY_SERVICE = "dns-proxy"
PROXY_DIR_NAME = "dns-proxy"
PROXY_CONFIG_NAME = "dns-proxy.json"
PROXY_CONFIG_DIR_IN_CONTAINER = "/etc/dns-proxy"
PROXY_CONFIG_PATH_IN_CONTAINER = f"{PROXY_CONFIG_DIR_IN_CONTAINER}/{PROXY_CONFIG_NAME}"
PROXY_HEALTH_NAME = "dns-proxy-health.invalid."
DEFAULT_FALLBACK = "127.0.0.11"
DEFAULT_TIMEOUT = 2.0
PROXY_PYTHON = "/app/.venv/bin/python"


def proxy_image() -> str:
    """The image the injected proxy runs: the -api image by default."""
    return (os.environ.get("DNS_PROXY_IMAGE")
            or os.environ.get("PROVISION_API_IMAGE")
            or "users_provision-subnet-acl-provision-api:latest")


def user_data_root() -> Path:
    raw = os.environ.get("USER_DATA_DIR")
    if raw:
        return Path(raw)
    return Path(os.environ.get("PROVISION_DIR", "/srv/provision_subnet_acl")) / "user_data"


def config_dir_for(user_name: str, service_name: str, label: str,
                   user_data_dir: str | Path | None = None) -> Path:
    root = Path(user_data_dir) if user_data_dir else user_data_root()
    return root / user_name / service_name / str(label) / PROXY_DIR_NAME


def _entry_config_dir(entry: dict) -> Path:
    return config_dir_for(
        str(entry.get("user_name")), str(entry.get("service_name")),
        str(entry.get("label")),
    )


def _prefix(entry: dict) -> str:
    # Lazy import: template_engine imports this module (injection) at call time.
    from . import template_engine
    return template_engine.container_prefix(
        str(entry["service_name"]), str(entry["user_name"]), str(entry["label"])
    )


def proxy_container_name(entry: dict) -> str:
    return f"{_prefix(entry)}{PROXY_SERVICE}"


def app_container_names(entry: dict) -> list[str]:
    names = [str(n) for n in (entry.get("container_names") or [])]
    proxy = proxy_container_name(entry)
    return [n for n in names if n != proxy]


# ─── Config file ─────────────────────────────────────────────────────────────


def config_path(config_dir: str | Path) -> Path:
    return Path(config_dir) / PROXY_CONFIG_NAME


def write_config(config_dir: str | Path, *, resolver_name: str | None,
                 network: str | None, proxy_ip: str | None = None,
                 enabled: bool | None = None) -> Path:
    """Atomically write the proxy config; a missing resolver means disabled."""
    path = config_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "enabled": bool(resolver_name) if enabled is None else bool(enabled),
        "resolver_name": resolver_name or None,
        "fallback": DEFAULT_FALLBACK,
        "timeout": DEFAULT_TIMEOUT,
        "network": network or None,
        "proxy_ip": proxy_ip or None,
    }
    tmp = path.with_name(f".{PROXY_CONFIG_NAME}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)
    Path(str(path) + ".generated").write_text("")
    return path


def read_config(config_dir: str | Path) -> dict[str, Any]:
    path = config_path(config_dir)
    try:
        raw = json.loads(path.read_text())
        raw["generation"] = path.stat().st_mtime_ns
        return raw
    except (OSError, ValueError):
        return {"enabled": False, "resolver_name": None, "generation": None}


# ─── Compose injection ───────────────────────────────────────────────────────

def _normalise_depends_on(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, list):
        return {str(k): {"condition": "service_started"} for k in value}
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, spec in value.items():
            if spec is None:
                out[str(key)] = {"condition": "service_started"}
            elif isinstance(spec, dict):
                out[str(key)] = dict(spec)
            else:
                out[str(key)] = {"condition": "service_started"}
        return out
    return {}


def inject_compose(
    rendered: str,
    *,
    network_name: str,
    container_prefix: str,
    subnet: str,
    gateway: str,
    proxy_ip: str,
    ip_range: str,
    config_dir: str,
    image: str | None = None,
) -> str:
    """Inject the proxy service, app DNS/depends_on and the IPAM range.

    ``rendered`` is the per-user compose text after Jinja resolution.  Services
    with ``network_mode`` are left alone (they are not on the tenant network);
    the injected proxy itself is never given a ``dns``/``depends_on``.
    """
    data = yaml.safe_load(rendered)
    if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
        return rendered

    networks = data.get("networks")
    if not isinstance(networks, dict):
        networks = {}
        data["networks"] = networks
    net_key = network_name if network_name in networks else None
    if net_key is None:
        for key, cfg in networks.items():
            if isinstance(cfg, dict) and cfg.get("name") == network_name:
                net_key = key
                break
    if net_key is None:
        net_key = network_name
        networks[net_key] = {"name": network_name}

    net_cfg = networks[net_key]
    if not isinstance(net_cfg, dict):
        net_cfg = {"name": network_name}
        networks[net_key] = net_cfg
    net_cfg["name"] = network_name
    net_cfg["ipam"] = {
        "config": [{"subnet": subnet, "gateway": gateway, "ip_range": ip_range}]
    }

    services: dict[str, Any] = data["services"]
    for key, svc in list(services.items()):
        if key == PROXY_SERVICE or not isinstance(svc, dict):
            continue
        if "network_mode" in svc:
            # Not on the tenant network: no dns/depends_on injection.
            continue
        svc["dns"] = [proxy_ip]
        depends = _normalise_depends_on(svc.get("depends_on"))
        depends[PROXY_SERVICE] = {"condition": "service_healthy"}
        svc["depends_on"] = depends

    services[PROXY_SERVICE] = {
        "image": image or proxy_image(),
        "container_name": f"{container_prefix}{PROXY_SERVICE}",
        "command": [PROXY_PYTHON, "-m", "lib.dns_proxy",
                    PROXY_CONFIG_PATH_IN_CONTAINER],
        "working_dir": "/app",
        "restart": "unless-stopped",
        "volumes": [f"{config_dir}:{PROXY_CONFIG_DIR_IN_CONTAINER}:ro"],
        "networks": {net_key: {"ipv4_address": proxy_ip}},
        "healthcheck": {
            "test": ["CMD-SHELL",
                     f"{PROXY_PYTHON} -m lib.dns_proxy --health "
                     f"{PROXY_CONFIG_PATH_IN_CONTAINER} --host 127.0.0.1"],
            "interval": "2s",
            "timeout": "2s",
            "retries": 10,
            "start_period": "3s",
        },
    }

    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, indent=2)


# ─── -api control plane ──────────────────────────────────────────────────────

def _connect_membership(entry: dict, network: str) -> dict[str, Any]:
    """Connect the proxy and its app containers to *network*; idempotent."""
    connected: list[str] = []
    errors: list[str] = []
    for name in [proxy_container_name(entry), *app_container_names(entry)]:
        try:
            docker_ops.network_connect(name, network)
            connected.append(name)
        except Exception as exc:  # noqa: BLE001 — one container must not stop the rest
            errors.append(f"{name}: {exc}")
            _log.warning("cross-network membership failed for %s: %s", name, exc)
    return {"connected": connected, "errors": errors}


def _disconnect_membership(entry: dict, network: str) -> list[str]:
    disconnected: list[str] = []
    for name in [proxy_container_name(entry), *app_container_names(entry)]:
        try:
            docker_ops.network_disconnect(name, network)
            disconnected.append(name)
        except Exception:  # noqa: BLE001 — best effort
            pass
    return disconnected


def apply_service(service_id: str, *, resolver_name: str,
                  network: str) -> dict[str, Any]:
    """Write the enabled proxy config and connect the membership.

    The resolver value is the cross resolver's container NAME (G101); an old
    caller's IP is also accepted as the stored value and resolves to itself.
    """
    entry = registry.get_service_by_id(str(service_id))
    if not entry:
        raise KeyError(f"no registration for {service_id}")
    cfg_dir = _entry_config_dir(entry)
    config = read_config(cfg_dir)
    write_config(cfg_dir, resolver_name=resolver_name, network=network,
                 proxy_ip=config.get("proxy_ip"))
    result = _connect_membership(entry, network)
    with db.connection() as conn:
        registry.update_instance(
            conn, entry["service_id"],
            extra_resolver=resolver_name, extra_network=network,
        )
    _log.info("dns proxy enabled for %s -> %s (membership %d)",
              entry["service_id"], resolver_name, len(result["connected"]))
    return {"service_id": str(entry["service_id"]), "resolver_name": resolver_name,
            "network": network, **result}


def clear_service(service_id: str) -> dict[str, Any]:
    entry = registry.get_service_by_id(str(service_id))
    if not entry:
        raise KeyError(f"no registration for {service_id}")
    cfg_dir = _entry_config_dir(entry)
    network = entry.get("extra_network")
    config = read_config(cfg_dir)
    write_config(cfg_dir, resolver_name=None, network=None,
                 proxy_ip=config.get("proxy_ip"), enabled=False)
    disconnected = _disconnect_membership(entry, str(network)) if network else []
    with db.connection() as conn:
        registry.update_instance(
            conn, entry["service_id"], extra_resolver=None, extra_network=None,
        )
    _log.info("dns proxy disabled for %s", entry["service_id"])
    return {"service_id": str(entry["service_id"]), "cleared": True,
            "disconnected": disconnected}


def get_state(service_id: str) -> dict[str, Any]:
    entry = registry.get_service_by_id(str(service_id))
    if not entry:
        raise KeyError(f"no registration for {service_id}")
    cfg_dir = _entry_config_dir(entry)
    config = read_config(cfg_dir)
    return {
        "service_id": str(entry["service_id"]),
        "extra_resolver": entry.get("extra_resolver"),
        "extra_network": entry.get("extra_network"),
        "proxy_container": proxy_container_name(entry),
        "proxy_address": config.get("proxy_ip"),
        "enabled": bool(config.get("enabled")),
        "resolver_name": config.get("resolver_name"),
        "config_generation": config.get("generation"),
    }


# ─── Startup pass and start-event membership watcher ─────────────────────────

def startup_pass() -> dict[str, Any]:
    """Re-write the config and re-ensure membership for every enabled service."""
    applied = 0
    errors: list[str] = []
    for entry in registry.get_all_users():
        resolver_name = entry.get("extra_resolver")
        network = entry.get("extra_network")
        if not resolver_name or not network:
            continue
        try:
            apply_service(str(entry["service_id"]), resolver_name=str(resolver_name),
                          network=str(network))
            applied += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{entry.get('service_id')}: {exc}")
    _log.info("dns-proxy startup pass: %d service(s), %d error(s)", applied, len(errors))
    return {"applied": applied, "errors": errors}


def _entries_with_membership() -> dict[str, dict]:
    by_container: dict[str, dict] = {}
    for entry in registry.get_all_users():
        if not entry.get("extra_resolver") or not entry.get("extra_network"):
            continue
        for name in [proxy_container_name(entry), *app_container_names(entry)]:
            by_container[name] = entry
    return by_container


def _match_container(name: str, by_container: dict[str, dict]) -> str | None:
    if not name:
        return None
    if name in by_container:
        return name
    if "_" in name:
        stripped = name.split("_", 1)[1]
        if stripped in by_container:
            return stripped
    return None


def _event_container(event: dict) -> str:
    actor = event.get("Actor") or {}
    attrs = actor.get("Attributes") or {}
    return str(attrs.get("name") or "")


def watch_for_starts(*, proc_factory=None, _once: bool = False) -> None:
    """Re-ensure cross-network membership when one of the service's containers starts.

    This is the *only* remaining job of the retired resolver-pair watcher: no
    ``/etc/resolv.conf`` is written.  Docker regenerates the membership on
    recreate; the proxy's own start event is handled like any app container's.
    """
    cmd = [
        "docker", "events", "--filter", "type=container",
        "--filter", "event=start", "--format", "{{json .}}",
    ]
    factory = proc_factory or (
        lambda: subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    )
    while True:
        proc = None
        try:
            proc = factory()
            q: "queue.Queue[str | None]" = queue.Queue()

            def _reader(stdout) -> None:
                try:
                    for line in stdout:
                        q.put(line)
                finally:
                    q.put(None)

            threading.Thread(target=_reader, args=(proc.stdout,), daemon=True).start()
            while True:
                line = q.get()
                if line is None:
                    break
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                name = _event_container(event)
                entries = _entries_with_membership()
                match = _match_container(name, entries)
                if not match:
                    continue
                entry = entries[match]
                try:
                    _connect_membership(entry, str(entry["extra_network"]))
                except Exception:  # noqa: BLE001
                    _log.exception("membership re-ensure failed for %s", match)
            if _once:
                return
        except Exception:  # noqa: BLE001 — the watcher must never kill the -api
            _log.exception("docker events watcher failed; retrying")
            if _once:
                return
        finally:
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
        import time as _time
        _time.sleep(1.0)


def start_membership_monitor() -> threading.Thread:
    thread = threading.Thread(target=watch_for_starts, daemon=True,
                              name="dns-proxy-membership")
    thread.start()
    return thread


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

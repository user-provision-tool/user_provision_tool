"""Nginx network recovery and reconciliation.

Runs inside provision-api.  All state comes from ``user_registry.yml`` —
no separate state file needed.  The registry already records ``network_name``
for every registered user/service/label, which is the single source of truth
for reconnecting ``provision-nginx`` to user networks on startup.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import docker_ops, registry

_log = logging.getLogger(__name__)


def _generated_dir() -> Path:
    """Return GENERATED_DIR from env or default."""
    return Path(
        os.environ.get("GENERATED_DIR", "/srv/provision/generated")
    )


# ---------------------------------------------------------------------------
# Startup recovery  (reads user_registry.yml only)
# ---------------------------------------------------------------------------


def recover_on_startup(
    nginx_container: str = "provision-nginx",
) -> dict[str, Any]:
    """Reconnect nginx to every user network listed in the registry.

    Called automatically on provision-api boot.  Idempotent — networks
    already connected are silently skipped by ``docker network connect``.

    Returns
    -------
    dict with ``networks_reconnected``, ``networks_total``, ``nginx_reloaded``.
    """
    _log.info("nginx recovery: reconnecting to all user networks from registry")

    all_users = registry.get_all_users()
    networks = sorted({e["network_name"] for e in all_users if e.get("network_name")})

    reconnected = 0
    for net in networks:
        try:
            docker_ops.network_connect(nginx_container, net)
            reconnected += 1
        except Exception:
            _log.warning("Failed to connect %s to network %s", nginx_container, net)

    try:
        docker_ops.nginx_reload(nginx_container)
        nginx_reloaded = True
    except Exception:
        _log.warning("Failed to reload %s", nginx_container)
        nginx_reloaded = False

    _log.info(
        "nginx recovery complete: %d/%d networks reconnected, nginx %s",
        reconnected, len(networks),
        "reloaded" if nginx_reloaded else "NOT reloaded",
    )

    return {
        "networks_reconnected": reconnected,
        "networks_total": len(networks),
        "nginx_reloaded": nginx_reloaded,
    }


# ---------------------------------------------------------------------------
# Reconciliation  (live — no state file)
# ---------------------------------------------------------------------------


def run_reconciliation(
    nginx_container: str = "provision-nginx",
) -> dict[str, Any]:
    """Run a live reconciliation pass.

    Parses all ``*.nginx.conf`` files, checks whether each upstream container
    is running, reconnects nginx to every network in the registry, and reloads
    nginx.  Returns a report — nothing is persisted to disk.

    Returns
    -------
    dict with ``last_run``, ``total_upstreams``, ``reachable``,
    ``unreachable``, ``unreachable_details``, ``networks_reconnected``,
    ``nginx_reloaded``, ``upstreams``.
    """
    generated_dir = _generated_dir()
    conf_files = sorted(generated_dir.glob("*.nginx.conf"))

    upstreams: list[dict[str, Any]] = []
    reachable = 0
    unreachable = 0
    unreachable_details: list[dict[str, Any]] = []

    # --- Parse each nginx conf and verify upstream targets ---
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

            upstream: dict[str, Any] = {
                "conf_file": cf.name,
                "server_name": server_name,
                "proxy_pass": proxy_pass,
            }

            if proxy_pass:
                url_match = re.match(r"https?://([^:]+)(?::(\d+))?", proxy_pass)
                if url_match:
                    target_container = url_match.group(1)
                    upstream["target_container"] = target_container

                    running = docker_ops.container_running(target_container)
                    exists = docker_ops.container_exists(target_container)
                    if running:
                        upstream["reachable"] = True
                        reachable += 1
                    else:
                        upstream["reachable"] = False
                        unreachable += 1
                        reason = "container not running" if exists else "container not found"
                        upstream["reason"] = reason
                        unreachable_details.append({
                            "upstream": proxy_pass,
                            "target_container": target_container,
                            "reason": reason,
                        })
            else:
                upstream["reachable"] = None

            upstreams.append(upstream)
        except Exception as e:
            unreachable_details.append({
                "conf_file": cf.name,
                "reason": str(e),
            })
            unreachable += 1

    # --- Reconnect nginx to all networks from registry ---
    all_users = registry.get_all_users()
    networks = sorted({e["network_name"] for e in all_users if e.get("network_name")})

    networks_reconnected = 0
    for net in networks:
        try:
            docker_ops.network_connect(nginx_container, net)
            networks_reconnected += 1
        except Exception:
            pass

    # --- Check container health from stored container_names ---
    containers_healthy = 0
    containers_total = 0
    for entry in all_users:
        names = entry.get("container_names") or _derive_container_names(entry)
        for cname in names:
            containers_total += 1
            if docker_ops.container_running(cname):
                containers_healthy += 1

    # --- Reload nginx ---
    try:
        docker_ops.nginx_reload(nginx_container)
        nginx_reloaded = True
    except Exception:
        nginx_reloaded = False

    return {
        "last_run": datetime.now(timezone.utc).isoformat(),
        "total_upstreams": len(upstreams),
        "reachable": reachable,
        "unreachable": unreachable,
        "unreachable_details": unreachable_details,
        "networks_reconnected": networks_reconnected,
        "total_networks_in_registry": len(networks),
        "nginx_reloaded": nginx_reloaded,
        "upstreams": upstreams,
        "containers_healthy": containers_healthy,
        "containers_total": containers_total,
    }


def _derive_container_names(entry: dict) -> list[str]:
    """Backward-compat: derive container names from compose file if not stored."""
    compose_file = entry.get("compose_file_path", "")
    if compose_file and Path(compose_file).exists():
        try:
            import yaml
            from . import template_engine
            with open(compose_file) as f:
                data = yaml.safe_load(f) or {}
            prefix = template_engine.container_prefix(
                entry.get("service_name", ""),
                entry.get("user_name", ""),
                str(entry.get("label", "0")),
            )
            svc_keys = list(data.get("services", {}).keys())
            return [f"{prefix}{k}" for k in svc_keys]
        except Exception:
            pass
    return []


def get_nginx_state() -> dict[str, Any]:
    """Return a live snapshot of nginx state — derived from registry + Docker.

    No state file is read; everything is queried live from the registry
    and the Docker daemon.  Container health is checked for every stored
    ``container_names`` entry.
    """
    all_users = registry.get_all_users()
    networks = sorted({e["network_name"] for e in all_users if e.get("network_name")})

    # Check which networks nginx is actually connected to
    nginx_connected: list[str] = []
    for net in networks:
        if docker_ops.network_connected_to_container(net, "provision-nginx"):
            nginx_connected.append(net)

    # Per-service container health (from stored container_names in registry)
    from . import template_engine

    services: list[dict[str, Any]] = []
    for entry in all_users:
        container_names = entry.get("container_names") or _derive_container_names(entry)
        containers: list[dict[str, Any]] = []
        for cname in container_names:
            running = docker_ops.container_running(cname)
            exists = docker_ops.container_exists(cname)
            status = "running" if running else ("stopped" if exists else "missing")
            containers.append({"name": cname, "status": status})
        services.append({
            "user_name": entry.get("user_name"),
            "service_name": entry.get("service_name"),
            "label": entry.get("label"),
            "network_name": entry.get("network_name"),
            "container_names": container_names,
            "containers": containers,
        })

    # Count upstream confs
    generated_dir = _generated_dir()
    conf_files = sorted(generated_dir.glob("*.nginx.conf"))

    return {
        "total_users": len(all_users),
        "total_networks": len(networks),
        "nginx_connected_networks": len(nginx_connected),
        "networks": networks,
        "connected": nginx_connected,
        "disconnected": [n for n in networks if n not in nginx_connected],
        "total_nginx_confs": len(conf_files),
        "services": services,
    }

"""Nginx state reconciliation — verifies upstreams and recovers on startup.

Runs inside provision-api (same filesystem as PROVISION_DIR).
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import docker_ops, registry

_log = logging.getLogger(__name__)

# Default location — same as provision-gateway used
DEFAULT_STATE_FILE = Path(
    os.environ.get("PROVISION_DIR", "/srv/provision")
) / "provision_nginx_state.json"


def _generated_dir() -> Path:
    """Return GENERATED_DIR from env or default."""
    return Path(
        os.environ.get("GENERATED_DIR", "/srv/provision/generated")
    )


# ---------------------------------------------------------------------------
# State file helpers
# ---------------------------------------------------------------------------


def _read_state(state_file: Path) -> dict[str, Any]:
    """Read the cached nginx state file. Returns empty dict if not found."""
    if state_file.exists():
        try:
            return json.loads(state_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"version": 1, "upstreams": [], "networks": {}}


def _write_state(report: dict[str, Any], state_file: Path) -> None:
    """Persist reconciliation report to state file."""
    state = {
        "version": 1,
        "last_updated": report["last_run"],
        "networks": report.get("networks", {}),
        "upstreams": report.get("upstreams", []),
    }
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state, indent=2))
    except Exception:
        _log.exception("Failed to write nginx state file %s", state_file)


# ---------------------------------------------------------------------------
# Startup recovery
# ---------------------------------------------------------------------------


def recover_on_startup(
    state_file: Path | None = None,
    nginx_container: str = "provision-nginx",
) -> dict[str, Any]:
    """Run on provision-api startup: reconnect nginx to all user networks.

    Reads the user registry (not the state file) to find all known networks,
    reconnects provision-nginx to each one idempotently, and reloads nginx.
    Also updates the state file with current upstream metadata.

    Parameters
    ----------
    state_file:
        Path to provision_nginx_state.json.  Defaults to
        ``$PROVISION_DIR/provision_nginx_state.json``.
    nginx_container:
        Name of the nginx container.

    Returns
    -------
    dict with keys: ``networks_reconnected``, ``networks_total``, ``nginx_reloaded``.
    """
    if state_file is None:
        state_file = DEFAULT_STATE_FILE

    _log.info("Starting nginx state recovery on provision-api startup")

    # Reconnect to all networks listed in the registry
    all_users = registry.get_all_users()
    networks = set()
    for entry in all_users:
        net = entry.get("network_name", "")
        if net:
            networks.add(net)

    reconnected = 0
    for net in sorted(networks):
        try:
            docker_ops.network_connect(nginx_container, net)
            reconnected += 1
        except Exception:
            _log.warning("Failed to reconnect %s to network %s", nginx_container, net)

    # Reload nginx
    try:
        docker_ops.nginx_reload(nginx_container)
        nginx_reloaded = True
    except Exception:
        _log.warning("Failed to reload %s", nginx_container)
        nginx_reloaded = False

    # Run a full reconciliation to update the state file
    try:
        report = run_reconciliation(state_file=state_file, nginx_container=nginx_container)
    except Exception:
        _log.exception("Reconciliation during startup recovery failed")
        report = {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "total_upstreams": 0,
            "reachable": 0,
            "unreachable": 0,
            "unreachable_details": [],
            "networks_reconnected": reconnected,
            "nginx_reloaded": nginx_reloaded,
            "upstreams": [],
        }
        _write_state(report, state_file)

    _log.info(
        "Startup recovery complete: %d/%d networks reconnected, nginx %s",
        reconnected, len(networks),
        "reloaded" if nginx_reloaded else "NOT reloaded",
    )

    return {
        "networks_reconnected": reconnected,
        "networks_total": len(networks),
        "nginx_reloaded": nginx_reloaded,
    }


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def run_reconciliation(
    state_file: Path | None = None,
    nginx_container: str = "provision-nginx",
) -> dict[str, Any]:
    """Run a full reconciliation pass.

    Reads all *.nginx.conf files from GENERATED_DIR, checks whether each
    upstream container is running, reconnects nginx to all user networks,
    reloads nginx, and writes the result to the state file.

    Parameters
    ----------
    state_file:
        Path to provision_nginx_state.json.  Defaults to
        ``$PROVISION_DIR/provision_nginx_state.json``.
    nginx_container:
        Name of the nginx container to reload.

    Returns
    -------
    dict report with keys:
        ``last_run``, ``total_upstreams``, ``reachable``, ``unreachable``,
        ``unreachable_details``, ``networks_reconnected``, ``nginx_reloaded``,
        ``upstreams``.
    """
    if state_file is None:
        state_file = DEFAULT_STATE_FILE

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
    networks = set()
    for entry in all_users:
        net = entry.get("network_name", "")
        if net:
            networks.add(net)

    networks_reconnected = 0
    for net in sorted(networks):
        try:
            docker_ops.network_connect(nginx_container, net)
            networks_reconnected += 1
        except Exception:
            pass

    # --- Reload nginx ---
    try:
        docker_ops.nginx_reload(nginx_container)
        nginx_reloaded = True
    except Exception:
        nginx_reloaded = False

    # --- Build and persist report ---
    report = {
        "last_run": datetime.now(timezone.utc).isoformat(),
        "total_upstreams": len(upstreams),
        "reachable": reachable,
        "unreachable": unreachable,
        "unreachable_details": unreachable_details,
        "networks_reconnected": networks_reconnected,
        "nginx_reloaded": nginx_reloaded,
        "upstreams": upstreams,
    }

    _write_state(report, state_file)
    return report


def get_reconciliation_state(state_file: Path | None = None) -> dict[str, Any]:
    """Return the current cached state from provision_nginx_state.json."""
    if state_file is None:
        state_file = DEFAULT_STATE_FILE
    return _read_state(state_file)

"""Generic extra-resolver / extra-network manager (GRILLING G83, 2026-11-03).

The ``-api`` is the only component with the Docker socket, so it is the one that
can edit a running container's ``/etc/resolv.conf`` and attach it to an extra
network. The values are supplied by the gateway and are **opaque** here: this
module never learns that the IP is the cross resolver or that the network is the
cross network; it stores them as the per-service ``extra_resolver`` and
``extra_network`` and re-applies them when they are needed again.

The update is addressed by **source service** and fans out to **every container
of that service**. It is asynchronous (a ``registry.tasks`` row the gateway can
poll) because a fan-out performs one Docker operation per container.

G91 — the pair is stored even when the service has **no running container**:
a stopped (or never-started) container gets it from the restart monitor when
it starts. A failure while updating a running container raises **before** the
store, so the previous pair stays in place.

Docker regenerates ``/etc/resolv.conf`` on every container start, so the stored
pair is re-applied:

* when a container of a field-set service restarts/redeploys (the restart
  monitor), and
* once on this process's startup, over the running containers of every service
  that has the fields set (a one-time pass — there is no periodic reconcile).

Rewriting ``/etc/resolv.conf`` has one platform constraint: the file is a
Docker bind mount, so it cannot be replaced by rename (``mv`` returns
``Resource busy``, measured 2026-11-03). The write is therefore
``cat > /etc/resolv.conf`` through a shell; it is validated by reading the file
back, and the previous file is saved as the baseline the removal restores.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from . import db, docker_ops, registry

_log = logging.getLogger("provision-api.resolv_conf")

#: The two nameserver lines the gateway's update installs: the extra resolver
#: first, the Docker embedded DNS second. No `search`/`options` line is kept:
#: the pair IS the whole file while the service has an extra resolver.
TWO_LINE_TEMPLATE = "nameserver {resolver_ip}\nnameserver 127.0.0.11\n"

#: The functional Docker default — only used when no captured baseline exists
#: (for example a container recreated while the -api was down).
DOCKER_DEFAULT = "nameserver 127.0.0.11\noptions ndots:0\n"

_lock = threading.Lock()
_service_locks: dict[str, threading.Lock] = {}


def _service_lock(service_id: str) -> threading.Lock:
    with _lock:
        return _service_locks.setdefault(str(service_id), threading.Lock())


def _baseline_dir() -> Path:
    path = Path(os.environ.get("GENERATED_DIR", "./generated")) / "resolv_baselines"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _baseline_path(container: str) -> Path:
    return _baseline_dir() / f"{container}.conf"


def _containers(entry: dict[str, Any]) -> list[str]:
    return [str(c) for c in (entry.get("container_names") or []) if c]


def _running(container: str) -> bool:
    return docker_ops.container_running(container)


def _save_baseline(container: str, content: str) -> None:
    try:
        _baseline_path(container).write_text(content, encoding="utf-8")
    except OSError:
        _log.warning("could not save the resolv.conf baseline for %s", container)


def _restore_content(container: str, resolver_ip: str | None) -> str:
    path = _baseline_path(container)
    try:
        baseline = path.read_text(encoding="utf-8")
    except OSError:
        return DOCKER_DEFAULT
    # A baseline that already carries the extra resolver is not a Docker
    # default (an earlier failed restore); do not make it the new default.
    if resolver_ip and f"nameserver {resolver_ip}" in baseline:
        return DOCKER_DEFAULT
    return baseline


def _apply_one(container: str, resolver_ip: str, network: str) -> None:
    """Write the two lines and ensure the membership for ONE container."""
    if not _running(container):
        raise RuntimeError(f"container {container} is not running — cannot apply the update")
    current = docker_ops.container_read_file(container, "/etc/resolv.conf")
    if f"nameserver {resolver_ip}" not in current:
        _save_baseline(container, current)
    expected = TWO_LINE_TEMPLATE.format(resolver_ip=resolver_ip)
    docker_ops.container_write_file(container, "/etc/resolv.conf", expected)
    written = docker_ops.container_read_file(container, "/etc/resolv.conf")
    if written != expected:
        raise RuntimeError(
            f"resolv.conf verification failed in {container}: expected {expected!r}, "
            f"got {written!r}"
        )
    if not docker_ops.network_connected_to_container(network, container):
        docker_ops.network_connect(container, network)
    if not docker_ops.network_connected_to_container(network, container):
        raise RuntimeError(
            f"container {container} is not a member of {network} after connect"
        )


def _clear_one(container: str, resolver_ip: str | None, network: str) -> None:
    if not _running(container):
        return
    content = _restore_content(container, resolver_ip)
    docker_ops.container_write_file(container, "/etc/resolv.conf", content)
    if resolver_ip and f"nameserver {resolver_ip}" in docker_ops.container_read_file(
        container, "/etc/resolv.conf"
    ):
        raise RuntimeError(f"resolv.conf still carries the extra resolver in {container}")
    if network:
        docker_ops.network_disconnect(container, network)


def apply_service(service_id: str, resolver_ip: str, network: str) -> dict[str, Any]:
    """Apply the pair to every container of *service_id*, then store it (G91).

    The pair is persisted even when the service has **no running container**:
    a stopped container (or a compose service whose profile never started) gets
    the pair from the restart monitor on its next start. The stored row is the
    durable form; the running files are the applied form.

    The store happens only after every RUNNING container was updated: a failed
    apply raises before the write, so the previous pair stays in place and the
    next pass retries with the same values.
    """
    resolver_ip = (resolver_ip or "").strip()
    network = (network or "").strip()
    if not resolver_ip or not network:
        raise ValueError("resolver_ip and network are required")
    lock = _service_lock(service_id)
    with lock:
        entry = registry.get_service_by_id(service_id)
        if entry is None:
            raise KeyError(f"service {service_id} does not exist")
        if entry.get("deleted_at") is not None:
            raise KeyError(f"service {service_id} is deleted")
        containers = _containers(entry)
        # A compose service that no profile started has a recorded name but no
        # container (measured 2026-11-03: graphiti's falkordb profiles). It is
        # stored-pending, not a failure: the monitor applies the pair when a
        # container of the service first starts.
        running = [c for c in containers if _running(c)]
        skipped = [c for c in containers if c not in running]
        for container in running:
            _apply_one(container, resolver_ip, network)
        with db.connection() as conn:
            registry.update_instance(
                conn, service_id,
                extra_resolver=resolver_ip, extra_network=network,
            )
        _log.info(
            "extra resolver stored for %s (%d applied, %d pending): %s / %s",
            service_id, len(running), len(skipped), resolver_ip, network,
        )
        return {"service_id": str(service_id), "containers": running,
                "skipped": skipped, "applied": len(running),
                "stored": True, "pending": len(skipped),
                "extra_resolver": resolver_ip, "extra_network": network}


def clear_service(service_id: str) -> dict[str, Any]:
    """Restore the default and disconnect every container; clear both fields."""
    lock = _service_lock(service_id)
    with lock:
        entry = registry.get_service_by_id(service_id)
        if entry is None:
            raise KeyError(f"service {service_id} does not exist")
        resolver_ip = entry.get("extra_resolver")
        network = entry.get("extra_network") or ""
        containers = _containers(entry)
        for container in containers:
            _clear_one(container, resolver_ip, network)
        with db.connection() as conn:
            registry.update_instance(
                conn, service_id, extra_resolver=None, extra_network=None,
            )
        _log.info("extra resolver cleared for %s (%d container(s))",
                  service_id, len(containers))
        return {"service_id": str(service_id), "containers": containers,
                "cleared": True}


def reapply_entry(entry: dict[str, Any], *, container: str | None = None) -> dict[str, Any]:
    """Re-apply the STORED pair to a service (or to one of its containers)."""
    service_id = str(entry.get("service_id") or "")
    resolver_ip = entry.get("extra_resolver")
    network = entry.get("extra_network")
    if not service_id or not resolver_ip or not network:
        return {"service_id": service_id, "applied": False, "reason": "fields not set"}
    targets = [container] if container else _containers(entry)
    applied: list[str] = []
    errors: list[str] = []
    for name in targets:
        # A stopped container is not a failure: its file is regenerated on the
        # next start, and the restart monitor applies the pair then.
        if not _running(str(name)):
            continue
        try:
            _apply_one(str(name), str(resolver_ip), str(network))
            applied.append(str(name))
        except Exception as exc:  # noqa: BLE001 — one container must not stop the rest
            errors.append(f"{name}: {exc}")
            _log.warning("re-apply of the stored extra resolver failed for %s: %s",
                         name, exc)
    return {"service_id": service_id, "applied": bool(applied),
            "containers": applied, "errors": errors}


def startup_pass() -> dict[str, Any]:
    """One re-apply pass over every service with the fields set (not periodic)."""
    applied = 0
    errors: list[str] = []
    for entry in registry.get_all_users():
        if not entry.get("extra_resolver") or not entry.get("extra_network"):
            continue
        result = reapply_entry(entry)
        if result.get("applied"):
            applied += 1
        errors.extend(result.get("errors") or [])
    _log.info("extra-resolver startup pass: %d service(s) re-applied, %d error(s)",
              applied, len(errors))
    return {"applied": applied, "errors": errors}


# ---------------------------------------------------------------------------
# Restart monitor
# ---------------------------------------------------------------------------

def _entries_with_fields() -> tuple[dict[str, dict], set[str]]:
    """``(container name → entry, compose projects)`` for field-set services."""
    by_container: dict[str, dict] = {}
    projects: set[str] = set()
    for entry in registry.get_all_users():
        if not entry.get("extra_resolver") or not entry.get("extra_network"):
            continue
        for name in _containers(entry):
            by_container[name] = entry
        project = entry.get("network_name")
        if project:
            projects.add(str(project))
    return by_container, projects


def _match_container(name: str, by_container: dict[str, dict]) -> str | None:
    if not name:
        return None
    if name in by_container:
        return name
    # Compose's recreate names the container `<hash>_<final name>` for a while.
    if "_" in name:
        stripped = name.split("_", 1)[1]
        if stripped in by_container:
            return stripped
    return None


def _event_container(event: dict) -> str:
    actor = event.get("Actor") or {}
    attrs = actor.get("Attributes") or {}
    return str(attrs.get("name") or "")


def watch_for_restarts(*, proc_factory=None, _once: bool = False) -> None:
    """Block: re-apply the stored resolver/membership when a container starts.

    Docker regenerates ``/etc/resolv.conf`` on every start and a recreated
    container loses its runtime membership. The trigger is Docker's own event
    stream, not a timer: the window is fail closed, and a timer would only
    stretch it. Only containers belonging to a service whose fields are set are
    touched.
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

            threading.Thread(target=_reader, args=(proc.stdout,),
                             name="resolv-event-reader", daemon=True).start()
            while True:
                line = q.get()
                if line is None:
                    break
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if event.get("Type") != "container" or event.get("Action") != "start":
                    continue
                by_container, _projects = _entries_with_fields()
                name = _match_container(_event_container(event), by_container)
                if name is None:
                    continue
                entry = by_container[name]
                # Give the container's entrypoint a moment; Docker has already
                # regenerated the file by the time `start` is emitted.
                for _ in range(20):
                    if _running(name):
                        break
                    time.sleep(0.5)
                reapply_entry(entry, container=name)
                if _once:
                    return
            print("[provision-api] docker resolv-event stream ended; retrying in 5s",
                  flush=True)
        except Exception:
            _log.exception("docker resolv-event stream failed; retrying")
        finally:
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        if _once:
            return
        time.sleep(5)


def start_restart_monitor() -> threading.Thread:
    thread = threading.Thread(
        target=watch_for_restarts, name="extra-resolver-restart-watch", daemon=True,
    )
    thread.start()
    return thread

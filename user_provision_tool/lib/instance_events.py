"""R12 — reload nginx when an INSTANCE container starts without the -api.

Every stream conf points at its compose service by NAME with a STATIC
``proxy_pass``; nginx resolves that name once, at config load.  The -api already
reloads after each ``compose_up`` IT performs (registration, rebuild, start —
R16).  This module closes the other half: a container started or recreated by
anything else — a host reboot, a manual ``docker restart`` / ``docker compose
up``, another tool — is observed on Docker's own event stream, and the -api
reloads once the new endpoint is current.

The watcher is deliberately narrow:

* it watches container ``start`` / ``create`` events only (a stop cannot add an
  endpoint, and every stream conf it could affect is paused by ``stop_service``,
  R13);
* it keeps only events whose container belongs to a REGISTERED instance (by the
  recorded container name, or by the compose project the registry records);
* it coalesces one ``compose up`` burst into ONE reload (a quiet window after
  the last matching event);
* it waits until every affected container is running and attached to a network
  before reloading — a reload before the endpoint exists would fail;
* it writes the central policy and reloads through
  ``stream_access.refresh_and_reload``, so a resolver/write failure follows the
  same fail-closed rule as every other reload path.

It is not a licence to watch every Docker event, and it does not replace the
in-band reloads: those stay.
"""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
import time

from . import docker_ops, registry, stream_access

_log = logging.getLogger(__name__)

# Docker's `create` precedes `start`; `start` also covers a plain restart of an
# existing container, and a recreate (rm + create + start) emits both.
MATCHING_ACTIONS = ("create", "start")

# A compose up emits one create/start pair per container, usually within a
# couple of seconds.  The quiet window collapses the burst; it also gives the
# endpoint a moment to become current before the readiness poll starts.
DEFAULT_SETTLE_SECONDS = 3.0
DEFAULT_READINESS_TIMEOUT = 20.0
DEFAULT_READINESS_INTERVAL = 0.5
DEFAULT_RELOAD_ATTEMPTS = 3


def parse_event_line(line: str) -> dict | None:
    """Parse one ``docker events --format '{{json .}}'`` line.

    A malformed line is ignored: the stream also carries unrelated event types,
    and one bad line must not tear the watcher down.
    """
    if not line or not line.strip():
        return None
    try:
        data = json.loads(line)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def event_container_id(event: dict) -> str:
    """The event's container ID — the only identifier stable across a rename.

    Docker Compose recreates a container under a temporary name
    (`<hash>_<final name>`) and renames it to the final name afterwards.  The
    `create` event carries the TEMPORARY name, so a name-based readiness check
    inspects a container that no longer exists and skips the reload (measured
    2026-10-27 on an out-of-band `compose up --force-recreate`).  The ID is the
    same for create, start and the renamed container.
    """
    actor = event.get("Actor") or {}
    return str(actor.get("ID") or "")


def event_container_name(event: dict) -> str:
    actor = event.get("Actor") or {}
    attrs = actor.get("Attributes") or {}
    return str(attrs.get("name") or "")


def event_compose_project(event: dict) -> str:
    actor = event.get("Actor") or {}
    attrs = actor.get("Attributes") or {}
    return str(attrs.get("com.docker.compose.project") or "")


def instance_targets() -> tuple[set[str], set[str]]:
    """``(container names, compose projects)`` of every registered instance.

    Read fresh on each event: an instance deployed after the watcher started is
    an instance too.  The recorded container names are authoritative; the
    compose project is the fallback for a container whose records predate
    ``container_names`` (or that was renamed by Compose).
    """
    names: set[str] = set()
    projects: set[str] = set()
    for entry in registry.get_all_users():
        for name in entry.get("container_names") or []:
            if name:
                names.add(str(name))
        project = entry.get("network_name")
        if project:
            projects.add(str(project))
    return names, projects


def event_matches(event: dict, names: set[str], projects: set[str]) -> bool:
    """True only for an instance container's start/create."""
    if event.get("Type") != "container":
        return False
    if event.get("Action") not in MATCHING_ACTIONS:
        return False
    name = event_container_name(event)
    if name and name in names:
        return True
    # Compose's recreate temporarily names the container `<hash>_<final name>`;
    # strip that prefix before the membership test.
    if name and "_" in name:
        stripped = name.split("_", 1)[1]
        if stripped in names:
            return True
    project = event_compose_project(event)
    return bool(project and project in projects)


def _container_ready(container: str, inspect=docker_ops.container_inspect) -> bool:
    """Running AND attached to at least one network.

    Running alone is not enough: nginx resolves the name through Docker's DNS,
    which only has an answer once the container is attached.  A `create` that is
    never started therefore never becomes ready, and the caller skips the reload
    rather than loading a config whose upstream cannot resolve.
    """
    info = inspect(container)
    if not info:
        return False
    if not (info.get("State") or {}).get("Running"):
        return False
    networks = (info.get("NetworkSettings") or {}).get("Networks") or {}
    return bool(networks)


def wait_until_ready(
    names: set[str] | list[str],
    *,
    timeout: float = DEFAULT_READINESS_TIMEOUT,
    interval: float = DEFAULT_READINESS_INTERVAL,
    inspect=docker_ops.container_inspect,
    sleep=time.sleep,
) -> tuple[set[str], set[str]]:
    """Poll until every name is ready; return ``(ready, not_ready)``."""
    pending = {str(n) for n in names if n}
    deadline = time.monotonic() + max(0.0, timeout)
    while pending:
        for name in list(pending):
            if _container_ready(name, inspect=inspect):
                pending.discard(name)
        if not pending:
            break
        if time.monotonic() >= deadline:
            break
        sleep(interval)
    return ({str(n) for n in names if n} - pending, pending)


def reload_for_batch(
    names: set[str],
    nginx_container: str,
    *,
    attempts: int = DEFAULT_RELOAD_ATTEMPTS,
    sleep=time.sleep,
) -> bool:
    """Refresh the central policy and reload, retrying a write/resolve failure.

    ``stream_access.refresh_and_reload`` fails closed: a resolver failure writes
    ``deny all;`` and a write failure removes the stale allow.  When it raises
    (the file could not be written or removed) the reload is NOT sent — a stale
    allow must never be loaded — and the attempt is logged and retried.
    """
    last: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            stream_access.refresh_and_reload(nginx_container)
            return True
        except Exception as exc:  # noqa: BLE001 — retry, then report
            last = exc
            _log.warning(
                "instance-event reload attempt %d/%d failed for %s: %s",
                attempt, attempts, sorted(names), exc,
            )
            if attempt < attempts:
                sleep(1.0)
    _log.error(
        "instance-event reload gave up for %s after %d attempt(s): %s",
        sorted(names), attempts, last,
    )
    return False


def handle_event_batch(
    names: set[str],
    nginx_container: str,
    *,
    readiness_timeout: float = DEFAULT_READINESS_TIMEOUT,
    inspect=docker_ops.container_inspect,
    sleep=time.sleep,
    reload_fn=reload_for_batch,
) -> dict[str, object]:
    """One settled batch: wait for readiness, then reload once (or skip)."""
    ready, pending = wait_until_ready(
        names, timeout=readiness_timeout, inspect=inspect, sleep=sleep,
    )
    if pending:
        # A container that never started has no endpoint to resolve.  Reloading
        # would load a config that names it and make `nginx -t` fail for every
        # service, so the batch is reported and skipped.
        _log.warning(
            "instance events: %s did not become ready within %.0fs — no reload",
            sorted(pending), readiness_timeout,
        )
        return {"reloaded": False, "ready": sorted(ready), "not_ready": sorted(pending)}
    reloaded = reload_fn(names, nginx_container, sleep=sleep)
    return {"reloaded": bool(reloaded), "ready": sorted(ready), "not_ready": []}


def _drain(
    q: "queue.Queue[str | None]",
    batch: set[str],
    names: set[str],
    projects: set[str],
    settle_seconds: float,
    sleep=time.sleep,
) -> bool:
    """Collect further matching events until a quiet window; return stream ended."""
    deadline = time.monotonic() + settle_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            line = q.get(timeout=remaining)
        except queue.Empty:
            return False
        if line is None:
            return True
        event = parse_event_line(line)
        if event is None or not event_matches(event, names, projects):
            continue
        cid = event_container_id(event)
        if cid:
            batch.add(cid)
        # A compose up's events are usually adjacent; extending the quiet
        # window after each matching one collapses the whole burst.
        deadline = time.monotonic() + settle_seconds


def _reader(stdout, q: "queue.Queue[str | None]") -> None:
    try:
        for line in stdout:
            q.put(line)
    except Exception:  # noqa: BLE001 — the stream dies with the process
        pass
    finally:
        q.put(None)


def watch_instance_events(
    nginx_container: str = "subnet-acl-nginx",
    *,
    proc_factory=None,
    _once: bool = False,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    readiness_timeout: float = DEFAULT_READINESS_TIMEOUT,
    inspect=docker_ops.container_inspect,
    sleep=time.sleep,
    reload_fn=reload_for_batch,
) -> None:
    """Block: reload nginx when an instance container starts behind our back.

    ``proc_factory`` and ``_once`` exist so a test can drive one settled batch
    without a Docker daemon.  The `--format '{{json .}}'` shape is used because
    the default text form has no stable fields; the JSON actor carries the
    container name and its compose project, which is exactly the filter input.
    """
    cmd = [
        "docker", "events",
        "--filter", "type=container",
        "--filter", "event=create",
        "--filter", "event=start",
        "--format", "{{json .}}",
    ]
    factory = proc_factory or (
        lambda: subprocess.Popen(  # noqa: S603 — a fixed argv, no shell
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    )
    while True:
        proc = None
        try:
            proc = factory()
            q: "queue.Queue[str | None]" = queue.Queue()
            threading.Thread(
                target=_reader, args=(proc.stdout, q),
                name="instance-event-reader", daemon=True,
            ).start()
            while True:
                line = q.get()
                if line is None:
                    break
                event = parse_event_line(line)
                if event is None:
                    continue
                names, projects = instance_targets()
                if not event_matches(event, names, projects):
                    continue
                # The ID, not the name: a compose recreate's `create` event
                # carries a temporary name that does not exist by readiness time.
                batch = {event_container_id(event)}
                batch.discard("")
                ended = _drain(
                    q, batch, names, projects, settle_seconds, sleep=sleep,
                )
                if batch:
                    handle_event_batch(
                        batch, nginx_container,
                        readiness_timeout=readiness_timeout,
                        inspect=inspect, sleep=sleep, reload_fn=reload_fn,
                    )
                if _once:
                    return
                if ended:
                    break
            # The stream ENDED — the daemon went away, the socket was revoked,
            # or the argv was rejected.  Say so: a watcher that silently never
            # watches is worse than none.
            print(
                "[provision-api] docker instance-event stream ended; retrying in 5s",
                flush=True,
            )
        except Exception:
            _log.exception("docker instance-event stream failed; retrying")
        finally:
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        if _once:
            return
        sleep(5)


def start_watcher(nginx_container: str = "subnet-acl-nginx") -> threading.Thread:
    """Start the watcher as a daemon thread (the -api lifespan's entry point)."""
    thread = threading.Thread(
        target=watch_instance_events,
        kwargs={"nginx_container": nginx_container},
        name="instance-container-watch",
        daemon=True,
    )
    thread.start()
    return thread

"""The platform's ONE stream access policy file.

`nginx.provision.conf` includes this file once in the `stream {}` context and
falls back to `deny all;` when it is absent.  The per-instance `*.stream.conf`
blocks carry NO policy: a server-level `allow`/`deny` would override the
inherited stream-context list entirely.

The file is written from the network NAME in ``SHARED_NETWORK``.  nginx's
`allow` accepts an address/prefix, never a network name, so this module is the
translation: `docker network inspect <name>` → exactly one IPv4 subnet → one
`allow <cidr>; deny all;`.  It is written atomically before every nginx reload,
so the policy a reload consumes is the policy for the network as it exists at
that moment.

Failure is fail-closed: if the network cannot be resolved, the file is replaced
with `deny all;` (or removed, which the static fallback also denies).  A stale
`allow` is never left behind.
"""

from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path

from . import docker_ops

_log = logging.getLogger(__name__)

POLICY_FILENAME = "stream-access.conf"
# The temp name is derived per target as `.<name>.tmp`; the leading dot and
# `.tmp` suffix keep it out of both nginx globs, so a reload never reads a
# half-written file.
LOCK_FILENAME = ".stream-access.lock"

FAIL_CLOSED = "deny all;\n"

# R13 — a stopped instance's stream conf leaves the served set.  The suffix is
# outside the internal nginx's `*.stream.conf` include and outside the deploy
# panel's candidate predicate, so a paused file can neither be loaded nor
# re-selected by accident.  It is a PAUSE, not a deletion: the registry entry
# and the file stay, and `start_service` renames it back.
PAUSED_SUFFIX = ".paused"


def stream_conf_paths(entry: dict) -> list[Path]:
    """The stream conf paths recorded on a registry entry.

    The per-category map is the authority (G55); the flat list is the fallback
    for entries written before it.  A path already carrying `.paused` is not a
    stream conf path for this purpose — callers derive the paused name from the
    active one.
    """
    by_cat = entry.get("nginx_conf_paths_by_category") or {}
    raw: list[str] = []
    if isinstance(by_cat, dict) and by_cat.get("stream"):
        raw = [str(by_cat["stream"])]
    else:
        raw = [str(p) for p in (entry.get("nginx_conf_paths") or []) if p]
    out: list[Path] = []
    for p in raw:
        path = Path(p)
        if path.name.endswith(".stream.conf") and path not in out:
            out.append(path)
    return out


def paused_path(active: str | Path) -> Path:
    return Path(str(active) + PAUSED_SUFFIX)


def pause_stream_confs(entry: dict) -> dict[str, object]:
    """Rename every active stream conf to `<name>.stream.conf.paused`.

    Returns ``{"changed": [paths], "already": [paths], "failed": [paths],
    "conflicts": [paths]}``.  A path whose paused name already exists is not
    overwritten (the active file keeps its route and the conflict is reported);
    a path whose active name is already absent is ``already`` — a second stop
    is a no-op.
    """
    changed: list[str] = []
    already: list[str] = []
    failed: list[str] = []
    conflicts: list[str] = []
    for active in stream_conf_paths(entry):
        paused = paused_path(active)
        if not active.exists():
            already.append(str(active))
            continue
        if paused.exists():
            conflicts.append(str(active))
            continue
        try:
            os.replace(active, paused)
        except OSError as exc:
            _log.warning("stream pause: cannot rename %s → %s: %s", active, paused, exc)
            failed.append(str(active))
            continue
        changed.append(str(paused))
    return {
        "changed": changed,
        "already": already,
        "failed": failed,
        "conflicts": conflicts,
    }


def restore_stream_confs(entry: dict) -> dict[str, object]:
    """Restore `<name>.stream.conf.paused` → `<name>.stream.conf`.

    The ACTIVE file wins when both names exist: the paused file is neither
    overwritten nor deleted, and the conflict is reported.  A missing paused
    file is not an error (the instance may have had no stream conf).
    """
    restored: list[str] = []
    conflicts: list[str] = []
    failed: list[str] = []
    missing: list[str] = []
    for active in stream_conf_paths(entry):
        paused = paused_path(active)
        if not paused.exists():
            missing.append(str(paused))
            continue
        if active.exists():
            conflicts.append(str(active))
            continue
        try:
            os.replace(paused, active)
        except OSError as exc:
            _log.warning("stream restore: cannot rename %s → %s: %s", paused, active, exc)
            failed.append(str(active))
            continue
        restored.append(str(active))
    return {
        "restored": restored,
        "conflicts": conflicts,
        "failed": failed,
        "missing": missing,
    }


def generated_dir() -> Path | None:
    """The directory the internal nginx includes, from the -api's own env."""
    raw = os.environ.get("GENERATED_DIR", "").strip()
    return Path(raw) if raw else None


def policy_text(cidr: str) -> str:
    return f"allow {cidr};\ndeny all;\n"


def _write_atomic(path: Path, text: str) -> None:
    # Per-target temp: a migration must not race the policy writer on one name.
    # The leading dot and `.tmp` suffix keep it out of both nginx globs.
    tmp = path.parent / f".{path.name}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    # Make the rename durable too; not all filesystems honour directory fsync,
    # so a failure here is not fatal.
    try:
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def refresh(base_dir: str | Path | None = None) -> tuple[bool, str]:
    """Resolve `SHARED_NETWORK` and write the policy file.

    Returns ``(changed, text)``.  With no generated directory (unit tests that
    do not set ``GENERATED_DIR``) it is a no-op and returns ``(False, "")`` —
    the caller still reloads, but no policy file is touched.
    """
    base = Path(base_dir) if base_dir is not None else generated_dir()
    if base is None:
        return False, ""

    base.mkdir(parents=True, exist_ok=True)
    path = base / POLICY_FILENAME

    with open(base / LOCK_FILENAME, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            try:
                cidr = docker_ops.shared_network_subnet()
                text = policy_text(cidr)
            except Exception as exc:  # noqa: BLE001 — every cause denies
                _log.warning(
                    "stream access: SHARED_NETWORK cannot be resolved (%s) — "
                    "denying all stream clients", exc,
                )
                text = FAIL_CLOSED

            try:
                old = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                old = None
            if old == text:
                return False, text

            try:
                _write_atomic(path, text)
            except OSError:
                # Fail closed: without the include, nginx.provision.conf's own
                # `deny all;` applies.  If even removal fails, propagate so the
                # caller does not reload a stale allow.
                _log.exception("stream access: cannot write %s — removing the allow file", path)
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    raise
            return True, text
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def refresh_and_reload(
    nginx_container: str,
    *,
    base_dir: str | Path | None = None,
    only_if_changed: bool = False,
) -> dict[str, object]:
    """Refresh the policy, then reload nginx (unless nothing changed and asked)."""
    changed, text = refresh(base_dir)
    if changed or not only_if_changed:
        docker_ops.nginx_reload(nginx_container)
    return {"policy_changed": changed, "policy": text}


def strip_stream_policy(text: str) -> str:
    """Remove per-server `allow`/`deny` from a stream conf.

    This is the migration/defence-in-depth half of the central policy: an old
    template or an old rendered conf must not keep a per-server rule that would
    override the stream-context list.
    """
    from .nginx_converter import remove_stream_policy

    return remove_stream_policy(text)


def migrate_rendered_stream_confs(base_dir: str | Path | None = None) -> list[str]:
    """Strip per-server policy from existing rendered `*.stream.conf` files.

    Returns the paths that changed.  Called once from startup recovery, before
    the reload, so confs rendered by the previous design cannot override the
    central policy.
    """
    base = Path(base_dir) if base_dir is not None else generated_dir()
    if base is None or not base.is_dir():
        return []
    changed: list[str] = []
    for path in sorted(base.glob("*.stream.conf")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        stripped = strip_stream_policy(text)
        if stripped == text:
            continue
        try:
            _write_atomic(path, stripped)
        except OSError:
            _log.exception("stream access: cannot migrate %s", path)
            continue
        changed.append(str(path))
    return changed

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
from datetime import datetime, timezone
from pathlib import Path

from . import docker_ops

_log = logging.getLogger(__name__)

POLICY_FILENAME = "stream-access.conf"
# The temp name is derived per target as `.<name>.tmp`; the leading dot and
# `.tmp` suffix keep it out of both nginx globs, so a reload never reads a
# half-written file.
LOCK_FILENAME = ".stream-access.lock"

FAIL_CLOSED = "deny all;\n"

# R13/S8 — a stopped instance's stream conf leaves the served set.  The suffix
# is outside the internal nginx's `*.stream.conf` include and outside the deploy
# panel's candidate predicate, so a paused file can neither be loaded nor
# re-selected by accident.  It is a PAUSE, not a deletion: the registry entry
# and the file stay, and `start_service` renames it back.
#
# S8 (GRILLING G95) — the hold is COLLISION-FREE.  The normal destination is
# `<name>.stream.conf.paused`; when that name already exists (an interrupted
# restore), the hold takes the next `<name>.stream.conf.paused.<n>` and raises
# an alert carried by the task.  Restore takes the HIGHEST paused index only
# when the active conf is absent; the active file wins when it exists.  No
# `unlink` is part of the hold, and the held copies are retained as context
# (`.paused.*` cleanup is a future feature, not part of this change).
PAUSED_SUFFIX = ".paused"
# The alert text a hold records when it had to use an index.  It is the signal
# that a previous restore was lost, so it travels to the task result and (via
# the gateway queue stats) to the Services-page notification.
HOLD_ALERT_MESSAGE = "previous paused conf was not restored"


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


def paused_index_path(active: str | Path, index: int) -> Path:
    """The indexed held name: `<active>.paused.<index>` (index >= 1)."""
    return Path(f"{active}{PAUSED_SUFFIX}.{index}")


def paused_copies(active: str | Path) -> list[tuple[int, Path]]:
    """Every held copy of *active*, lowest index first.

    Index 0 is the base `<name>.stream.conf.paused`; an explicit numeric suffix
    sorts above it.  Only names whose suffix is a positive integer count — a
    foreign file that merely shares the prefix is left alone.
    """
    active = Path(active)
    base = paused_path(active)
    found: list[tuple[int, Path]] = []
    if base.exists():
        found.append((0, base))
    prefix = f"{active.name}{PAUSED_SUFFIX}."
    try:
        siblings = list(active.parent.iterdir())
    except OSError:
        siblings = []
    for sibling in siblings:
        if not sibling.name.startswith(prefix):
            continue
        suffix = sibling.name[len(prefix):]
        if suffix.isdigit() and int(suffix) > 0:
            found.append((int(suffix), sibling))
    found.sort(key=lambda pair: pair[0])
    return found


def _held_copy_facts(path: Path) -> dict[str, object]:
    """`mtime` (ISO-8601, UTC, seconds) and `size` bytes for a held copy.

    The path is the fact the alert needs; the stat details are evidence for the
    Services page.  A stat race or permission error omits them rather than
    failing the hold.
    """
    try:
        st = path.stat()
    except OSError:
        return {}
    return {
        "mtime": datetime.fromtimestamp(
            st.st_mtime, tz=timezone.utc,
        ).isoformat(timespec="seconds"),
        "size": st.st_size,
    }


def next_paused_path(active: str | Path) -> tuple[int, Path]:
    """The collision-free hold destination for *active* and its index.

    The base `.paused` name is the normal hold (index 0).  An existing base
    means a previous restore was lost, so the hold takes the next free
    `.paused.<n>` (index >= 1) and the caller raises the hold alert.
    """
    active = Path(active)
    base = paused_path(active)
    if not base.exists():
        return 0, base
    index = 1
    while paused_index_path(active, index).exists():
        index += 1
    return index, paused_index_path(active, index)


def pause_stream_confs(entry: dict) -> dict[str, object]:
    """Rename every active stream conf out of the served glob.

    The normal destination is `<name>.stream.conf.paused` (index 0).  When a
    paused copy already exists the hold uses the next `.paused.<n>` and records
    an alert; the active conf is ALWAYS renamed (the collision is resolved, not
    reported as a conflict), so no reload can see a conf whose container is
    about to disappear.  A rename failure is ``failed`` and the caller aborts
    before the destructive step.

    Returns ``{"changed": [dest paths], "already": [active paths],
    "failed": [active paths], "conflicts": [], "details": [{active, paused,
    index}], "alerts": [{index, previous_paused_present, message, active,
    paused, held_as, existing}]}``.  ``conflicts`` is kept (always empty) for
    callers written against the fixed-suffix hold.

    Only an indexed hold (index >= 1) produces an alert: it names the held
    copy (or copies) that forced the index, each with its ``path``, ``mtime``
    (ISO-8601, UTC, seconds) and ``size`` bytes, plus the active path and the
    indexed destination (``paused`` and its alias ``held_as``).
    """
    changed: list[str] = []
    already: list[str] = []
    failed: list[str] = []
    conflicts: list[str] = []
    details: list[dict[str, object]] = []
    alerts: list[dict[str, object]] = []
    for active in stream_conf_paths(entry):
        if not active.exists():
            already.append(str(active))
            continue
        index, paused = next_paused_path(active)
        if index > 0:
            existing = [
                {"path": str(path), **_held_copy_facts(path)}
                for _existing_index, path in paused_copies(active)
            ]
            alerts.append({
                "index": index,
                "previous_paused_present": True,
                "message": HOLD_ALERT_MESSAGE,
                "active": str(active),
                "paused": str(paused),
                "held_as": str(paused),
                "existing": existing,
            })
            _log.warning(
                "stream-conf hold used index %d for %s: %s",
                index, active, HOLD_ALERT_MESSAGE,
            )
        try:
            os.replace(active, paused)
        except OSError as exc:
            _log.warning("stream pause: cannot rename %s → %s: %s", active, paused, exc)
            failed.append(str(active))
            continue
        changed.append(str(paused))
        details.append({"active": str(active), "paused": str(paused), "index": index})
    return {
        "changed": changed,
        "already": already,
        "failed": failed,
        "conflicts": conflicts,
        "details": details,
        "alerts": alerts,
    }


def restore_stream_confs(entry: dict) -> dict[str, object]:
    """Restore the HIGHEST held copy → the active name.

    The ACTIVE file wins when it exists: every held copy stays and the active
    path is reported as a conflict.  Otherwise the highest paused index (the
    base `.paused` is index 0, an explicit `.paused.<n>` sorts above it) is
    renamed back; the older held copies stay as context.  A missing paused copy
    is not an error (the instance may have had no stream conf).
    """
    restored: list[str] = []
    conflicts: list[str] = []
    failed: list[str] = []
    missing: list[str] = []
    details: list[dict[str, object]] = []
    for active in stream_conf_paths(entry):
        if active.exists():
            conflicts.append(str(active))
            continue
        copies = paused_copies(active)
        if not copies:
            missing.append(str(paused_path(active)))
            continue
        index, source = copies[-1]
        try:
            os.replace(source, active)
        except OSError as exc:
            _log.warning("stream restore: cannot rename %s → %s: %s", source, active, exc)
            failed.append(str(active))
            continue
        restored.append(str(active))
        details.append({"active": str(active), "paused": str(source), "index": index})
    return {
        "restored": restored,
        "conflicts": conflicts,
        "failed": failed,
        "missing": missing,
        "details": details,
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

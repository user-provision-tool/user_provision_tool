"""CRUD operations on user_registry.yml."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import contextlib
import fcntl
import os
import threading
import time

import yaml

REGISTRY_FILE = Path(
    os.environ.get("REGISTRY_FILE", str(Path(__file__).parent.parent / "user_registry.yml"))
)


_state: dict[str, Any] = {"mtime": None, "data": [], "path": None}


class RegistryLockTimeout(RuntimeError):
    """Another process held the registry write lock past the timeout."""


# Tier 2 (cross-process) lock: a SIDECAR file, never the registry itself —
# `_save` publishes via os.replace, which swaps the inode; a lock taken on the
# data file would stop excluding anyone the moment a write lands.
LOCK_TIMEOUT = 5.0
_lock_fd: dict[str, int] = {}          # path -> open fd (per process)
_local = threading.local()             # per-thread reentrancy depth


def _lock_path() -> Path:
    return REGISTRY_FILE.with_name(REGISTRY_FILE.name + ".lock")


def _acquire_flock(path: Path, timeout: float) -> int:
    """Exclusive flock with a bounded wait. Raises RegistryLockTimeout."""
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise RegistryLockTimeout(
                    f"registry write lock busy for >{timeout}s ({path})"
                )
            time.sleep(0.02)


@contextlib.contextmanager
def transaction(timeout: float = LOCK_TIMEOUT):
    """Serialise a registry READ-MODIFY-WRITE.

    Tier 1: the in-process RLock (threads in this process).
    Tier 2: an exclusive flock on the sidecar lock file (other processes: the
    CLI, scripts, another -api). Re-entrant per thread — a nested `flock` on a
    second fd would deadlock against itself (flock is per open file
    description), so a nested call reuses the outer lock.
    Readers never take this: `_save` publishes atomically, so a reader always
    sees a complete file and is never blocked by a writer.
    """
    with _lock:
        depth = getattr(_local, "depth", 0)
        if depth:
            _local.depth = depth + 1
            try:
                yield
            finally:
                _local.depth = depth
            return
        path = _lock_path()
        fd = _acquire_flock(path, timeout)
        _local.depth = 1
        try:
            # Read FRESH state under the lock: the cache is only refreshed by
            # the 1s monitor / a path change, so a writer appending to a stale
            # snapshot would silently drop another process's entry (the
            # lost-update race the flock exists to prevent).
            _reload()
            yield
        finally:
            _local.depth = 0
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
_lock = threading.RLock()
_monitor_started = False


def _reload() -> None:
    if not REGISTRY_FILE.exists():
        _state["data"] = []
        _state["mtime"] = None
        return
    try:
        with REGISTRY_FILE.open("r") as f:
            data = yaml.safe_load(f)
    except Exception:
        return
    if data is None:
        # Empty/partially-written file (a non-atomic writer truncates first).
        # Keep the last good state instead of caching [] — caching [] here is
        # what let the NEXT save persist an empty registry (data loss).
        return
    _state["data"] = data if isinstance(data, list) else []
    _state["mtime"] = REGISTRY_FILE.stat().st_mtime_ns
    _state["path"] = str(REGISTRY_FILE)


def _monitor_loop() -> None:
    """Watch the registry file; reload ONLY when it changes. Reads never
    stat/re-parse (per-poll re-parsing saturated the -api CPU)."""
    while True:
        time.sleep(1.0)
        if not REGISTRY_FILE.exists():
            continue
        try:
            m = REGISTRY_FILE.stat().st_mtime_ns
        except OSError:
            continue
        if m != _state["mtime"]:
            with _lock:
                try:
                    m2 = REGISTRY_FILE.stat().st_mtime_ns
                except OSError:
                    continue
                if m2 != _state["mtime"]:
                    _reload()


def _load() -> list[dict[str, Any]]:
    global _monitor_started
    with _lock:
        # Cache is keyed by the FILE, not just its mtime: a changed
        # REGISTRY_FILE (tests monkeypatch it; ops may repoint it) must never
        # serve the previous file's data.
        if _state["path"] != str(REGISTRY_FILE):
            _state["path"] = str(REGISTRY_FILE)
            _reload()
        elif _state["mtime"] is None and REGISTRY_FILE.exists():
            _reload()
    if not _monitor_started:
        _monitor_started = True
        threading.Thread(target=_monitor_loop, daemon=True, name="registry-watch").start()
    return _state["data"]


def _save(users: list[dict[str, Any]]) -> None:
    """Atomic write (temp + os.replace).

    A truncate-then-write let a concurrent reader/monitor observe an empty or
    partial file; that state was cached as [] and the next save persisted it,
    wiping the whole registry."""
    # Recovery net: keep the previous good copy next to the file so a bad write
    # (or any future bug) is always recoverable.
    try:
        if REGISTRY_FILE.exists() and REGISTRY_FILE.stat().st_size > 3:
            import shutil as _sh
            _sh.copy2(REGISTRY_FILE, REGISTRY_FILE.with_name(REGISTRY_FILE.name + ".bak"))
    except OSError:
        pass
    # Unique temp name per process: a shared "<name>.tmp" let two writers
    # consume each other's temp file (FileNotFoundError on os.replace) — the
    # flock serialises cooperating writers, but a stray writer must not be able
    # to break a concurrent save.
    tmp = REGISTRY_FILE.with_name(f"{REGISTRY_FILE.name}.tmp.{os.getpid()}")
    with tmp.open("w") as f:
        yaml.dump(users, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, REGISTRY_FILE)
    # Publish the new state immediately: readers must never see a stale list
    # (remove-then-save used to read the pre-write cache for up to 1s).
    _state["data"] = users
    try:
        _state["mtime"] = REGISTRY_FILE.stat().st_mtime_ns
    except OSError:
        pass


def get_all_users() -> list[dict[str, Any]]:
    return _load()


def get_user(user_name: str) -> list[dict[str, Any]]:
    """Return all registry entries for the given user_name."""
    return [u for u in _load() if u.get("user_name") == user_name]


def get_user_service(user_name: str, service_name: str, label: str) -> dict[str, Any] | None:
    """Return the registry entry for a specific user+service+label combination."""
    for u in _load():
        if (
            u.get("user_name") == user_name
            and u.get("service_name") == service_name
            and str(u.get("label", "")) == str(label)
        ):
            return u
    return None


def add_user(entry: dict[str, Any]) -> None:
    with transaction():
        users = list(_load())
        users.append(entry)
        _save(users)


def remove_user_service(user_name: str, service_name: str, label: str) -> bool:
    """Remove the entry matching user_name+service_name+label. Returns True if removed."""
    with transaction():
        users = list(_load())
        before = len(users)
        users = [
            u for u in users
            if not (
                u.get("user_name") == user_name
                and u.get("service_name") == service_name
                and str(u.get("label", "")) == str(label)
            )
        ]
        if len(users) == before:
            return False
        _save(users)
        return True

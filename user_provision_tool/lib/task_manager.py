"""``registry.tasks`` task store (T5).

The -api runs ONE process, so the future/cancel event stays in memory; the task
row is the persistence and the API surface. A restart cannot recover a runner,
so a non-terminal row left by a previous process is marked ``unknown`` and the
startup recovery sweep terminalizes it ``failed`` with an interrupted reason.

Contract highlights:
  * ids are ``api-<random>``; ``procedure_id`` groups one operator procedure;
  * ``kind`` is deploy|rebuild|remove; ``operation`` preserves the old type
    (register/rebuild/remove/fix_remove);
  * TTL/cap soft-deletes only finished operator-free rows: ``deleted_at`` is
    set, the log file is deleted and ``log_ref`` cleared, so every link still
    resolves to a tombstone;
  * the log endpoint returns 410 for a tombstone and 404 for a live row whose
    log is gone — never another task's global log.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import db, registry

_log = logging.getLogger("provision-api.tasks")

KIND_BY_TYPE = {"register": ("deploy", "register"),
                "rebuild": ("rebuild", "rebuild"),
                "remove": ("remove", "remove"),
                "fix_remove": ("remove", "fix_remove")}


class TaskCapacityError(RuntimeError):
    reason = "at_capacity"

    def __init__(self, running: int, capacity: int):
        self.running = running
        self.capacity = capacity
        super().__init__(
            f"at capacity: {running} task(s) running (limit {capacity}); "
            "cancel a task that has been hanging and retry"
        )


def _now():
    return datetime.now(timezone.utc)


def new_task_id() -> str:
    return f"api-{uuid.uuid4().hex[:12]}"


class TaskManager:
    def __init__(self, max_workers: int = 4, ttl_seconds: float = 604800,
                 max_tasks: int = 1000, log_dir: str = ""):
        self._max_workers = max_workers
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._lock = threading.Lock()
        self._cancel_events: dict[str, threading.Event] = {}
        self._futures: dict[str, Future] = {}
        self._ttl = ttl_seconds
        self._max_tasks = max_tasks
        self._log_dir = Path(log_dir) if log_dir else Path(".")
        self._log_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _row(self, task_id: str) -> dict | None:
        return db.fetch_one("SELECT * FROM registry.tasks WHERE task_id=%s", (task_id,))

    def _update(self, task_id: str, **fields: Any) -> None:
        allowed = {"status", "phase", "progress", "error", "log_ref", "result",
                   "deleted_at", "updated_at"}
        sets, vals = [], []
        import json

        from psycopg.types.json import Jsonb
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key}=%s")
            if key == "result":
                # UUID/datetime objects in a task result are data, not an
                # error: serialize them rather than failing a FINISHED task.
                vals.append(Jsonb(value, dumps=lambda v: json.dumps(v, default=str)))
            else:
                vals.append(value)
        if not sets:
            return
        sets.append("updated_at=%s")
        vals.append(_now())
        vals.append(task_id)
        db.execute(f"UPDATE registry.tasks SET {','.join(sets)} WHERE task_id=%s", tuple(vals))

    def recover_on_startup(self) -> int:
        """A non-terminal row from a previous process has no runner: `unknown`."""
        rows = db.fetch_all(
            "SELECT task_id FROM registry.tasks WHERE status IN ('queued','running')"
        )
        for row in rows:
            self._update(row["task_id"], status="unknown",
                         error="provision-api restarted — task state lost")
        return len(rows)

    def terminalize_unknown(self, reason: str = "interrupted by restart") -> int:
        rows = db.fetch_all("SELECT task_id FROM registry.tasks WHERE status='unknown'")
        for row in rows:
            self._update(row["task_id"], status="failed", error=reason)
        return len(rows)

    def prune(self) -> None:
        """TTL and cap: soft-delete finished rows; never touch live ones."""
        now = time.time()
        rows = db.fetch_all(
            "SELECT task_id, log_ref, updated_at FROM registry.tasks "
            "WHERE status IN ('completed','failed','cancelled') AND deleted_at IS NULL "
            "ORDER BY created_at"
        )
        excess = max(0, len(rows) - self._max_tasks)
        for idx, row in enumerate(rows):
            age = now - row["updated_at"].timestamp() if row["updated_at"] else 0
            if idx < excess or age > self._ttl:
                self._soft_delete(row["task_id"], row.get("log_ref"))

    def _soft_delete(self, task_id: str, log_ref: str | None) -> None:
        if log_ref:
            try:
                Path(log_ref).unlink(missing_ok=True)
            except OSError:
                pass
        self._update(task_id, deleted_at=_now(), log_ref=None)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @staticmethod
    def new_task_id() -> str:
        return new_task_id()

    def submit(self, task_type: str, fn: Callable[..., Any], *args: Any,
               task_id: str | None = None, procedure_id: str | None = None,
               service_id: str | None = None, persist: bool = True,
               **kwargs: Any) -> str:
        task_id = task_id or new_task_id()
        kind, operation = KIND_BY_TYPE.get(task_type, ("deploy", task_type))
        log_file = str(self._log_dir / f"task-{task_id}.log")

        # Capacity is checked BEFORE the row is written: a refused submit must
        # leave no task behind (T5/T7 — the refusal is not an accepted task).
        with self._lock:
            in_flight = db.fetch_one(
                "SELECT count(*) AS n FROM registry.tasks "
                "WHERE status IN ('queued','running') AND deleted_at IS NULL"
            )["n"]
            if in_flight >= self._max_workers:
                raise TaskCapacityError(in_flight, self._max_workers)
            if persist:
                proc = uuid.UUID(str(procedure_id)) if procedure_id else uuid.uuid4()
                with db.connection() as conn:
                    registry.insert_task(
                        conn, task_id=task_id, procedure_id=proc,
                        service_id=uuid.UUID(str(service_id)) if service_id else None,
                        kind=kind, operation=operation,
                    )
                self._update(task_id, log_ref=log_file)
            else:
                self._update(task_id, log_ref=log_file)
            event = threading.Event()
            self._cancel_events[task_id] = event

        try:
            import inspect as _inspect
            if "_task_id" in _inspect.signature(fn).parameters:
                kwargs["_task_id"] = task_id
            if "service_id" in _inspect.signature(fn).parameters and service_id:
                kwargs.setdefault("service_id", service_id)
            if "procedure_id" in _inspect.signature(fn).parameters and procedure_id:
                kwargs.setdefault("procedure_id", procedure_id)
        except (TypeError, ValueError):
            pass

        future = self._executor.submit(self._run_task, task_id, task_type, fn, *args, **kwargs)
        self._futures[task_id] = future
        self.prune()
        return task_id

    def get(self, task_id: str) -> dict[str, Any] | None:
        row = self._row(task_id)
        if not row:
            return None
        return self._to_dict(row)

    def get_log_file(self, task_id: str) -> str | None:
        row = self._row(task_id)
        return row.get("log_ref") if row else None

    def cancel(self, task_id: str) -> bool:
        row = self._row(task_id)
        if not row:
            return False
        if row["status"] in ("completed", "failed", "cancelled"):
            return False
        if row["operation"] == "fix_remove":
            return False
        if row["status"] == "unknown":
            return False
        # CAS: one winner.
        executed = db.execute(
            "UPDATE registry.tasks SET status='cancelled', updated_at=%s "
            "WHERE task_id=%s AND status IN ('queued','running')",
            (_now(), task_id),
        )
        if executed:
            ev = self._cancel_events.get(task_id)
            if ev:
                ev.set()
        return bool(executed)

    def finish(self, task_id: str, status: str, error: str = "") -> bool:
        if status not in ("completed", "failed"):
            return False
        executed = db.execute(
            "UPDATE registry.tasks SET status=%s, error=%s, updated_at=%s "
            "WHERE task_id=%s AND status IN ('queued','running','unknown')",
            (status, str(error) if status == "failed" else None, _now(), task_id),
        )
        return bool(executed)

    def list_all(self) -> list[dict[str, Any]]:
        rows = db.fetch_all(
            "SELECT * FROM registry.tasks WHERE deleted_at IS NULL "
            "ORDER BY created_at DESC"
        )
        return [self._to_dict(r) for r in rows]

    def _to_dict(self, row: dict) -> dict[str, Any]:
        return {
            "task_id": row["task_id"],
            "procedure_id": str(row["procedure_id"]),
            "service_id": str(row["service_id"]) if row["service_id"] else None,
            "executor": row["executor"],
            "kind": row["kind"],
            "operation": row["operation"],
            "type": row["operation"],
            "status": row["status"],
            "phase": row["phase"],
            "progress": row["progress"],
            "error": row["error"],
            "log_ref": row["log_ref"],
            "result": row["result"],
            "target": row.get("target_snapshot") or (
                {"service_id": str(row["service_id"])} if row["service_id"] else {}
            ),
            "deleted_at": row["deleted_at"].isoformat() if row["deleted_at"] else None,
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------
    def _run_task(self, task_id: str, task_type: str, fn: Callable,
                  *args: Any, **kwargs: Any) -> None:
        from . import docker_ops
        row = self._row(task_id)
        if not row or row["status"] == "cancelled":
            return
        log_file = row.get("log_ref")
        docker_ops.set_task_log_file(log_file)
        self._update(task_id, status="running")
        event = self._cancel_events.get(task_id)
        if event is not None:
            docker_ops.set_cancel_event(event)
        try:
            result = fn(*args, **kwargs)
            if self._row(task_id)["status"] != "cancelled":
                self._update(task_id, status="completed",
                             result=_redact(result))
        except Exception as exc:  # noqa: BLE001
            self._update(task_id, status="failed", error=str(exc))
        finally:
            docker_ops.clear_cancel_event()
            docker_ops.clear_task_log_file()
            self._cancel_events.pop(task_id, None)


def _redact(value: Any) -> Any:
    secret = {"passwd", "passwd_plain", "privkey", "password", "api_key", "token",
              "resume_values"}
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items() if k not in secret}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


task_manager = TaskManager(
    max_workers=int(os.environ.get("TASK_MAX_CONCURRENCY", "4")),
    ttl_seconds=int(os.environ.get("TASK_TTL_SECONDS", "604800")),
    max_tasks=int(os.environ.get("TASK_MAX_COUNT", "1000")),
    log_dir=os.environ.get(
        "TASK_LOG_DIR",
        str(Path(os.environ.get("GENERATED_DIR", "./generated")) / "task_logs"),
    ),
)

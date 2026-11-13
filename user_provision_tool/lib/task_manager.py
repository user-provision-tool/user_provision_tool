"""``registry.tasks`` — the bounded durable task queue (GRILLING G92).

The table IS the queue. One serialized planner turns ``queued`` rows into a
small ``planned`` buffer (``plan_seq``), and a fixed pool of workers claims
``planned -> running`` rows. The API surface only admits, reads and cancels.

Contract highlights (task doc §6):
  * admission refuses with ``queue_full`` (20 queued) or ``queue_unavailable``
    (stale planner heartbeat) and writes no row;
  * states are ``queued -> planned -> running -> completed|failed`` with
    ``cancelled`` from the first three; ``planned`` is a frozen plan;
  * the planner runs on plan exhaustion, anchored at the earliest queued task,
    over a 300 s window extended when too few, per-user deficit round-robin;
  * workers claim the lowest ``plan_seq`` atomically; the next plan forms as
    soon as the last planned row is claimed or cancelled — the plan is never
    refilled;
  * per-kind run timeouts start at ``running``; per-kind cancellation timeouts
    start at the cancel request; there is no queue-wait or end-to-end bound;
  * a restart fails only ``running`` rows ("interrupted by restart") and runs
    the runtime-only compensating cleanup; ``queued``/``planned`` survive.

A restart's queued/planned rows are re-executable because the submit path
persists a runner descriptor (operation, module/qualname, kwargs) in
``registry.tasks.request``; secret kwargs are encrypted with the service-Basic
keyring. Generated files are never auto-deleted anywhere in this module.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import db, keys, queue_contract as qc, registry

_log = logging.getLogger("provision-api.tasks")

KIND_BY_TYPE = {"register": ("deploy", "register"),
                "rebuild": ("rebuild", "rebuild"),
                "remove": ("remove", "remove"),
                "fix_remove": ("remove", "fix_remove"),
                # G83 — the resolv.conf/membership update is a first-class task:
                # addressed by source service, fanned out to every container, and
                # polled by the gateway.
                "resolv_conf_set": ("update", "resolv_conf_set"),
                "resolv_conf_clear": ("update", "resolv_conf_clear")}

# Advisory-lock keys (registry-owned). The planner and the admission/claim
# critical sections are separate so a slow plan selection never blocks a
# submit, while two workers can never both pass the running-count guard.
_PLAN_LOCK = 7_412_005
_ADMIT_CLAIM_LOCK = 7_412_006

_SECRET_KEYS = {"passwd", "passwd_plain", "privkey", "password", "api_key",
                "token", "resume_values"}

_MEDIAN_CACHE_TTL = 5.0

# operation -> callable. Used to re-execute a queued/planned row after a
# restart (the row's descriptor names the same operation).
_RUNNERS: dict[str, Callable[..., Any]] = {}


def _runners() -> dict[str, Callable[..., Any]]:
    if not _RUNNERS:
        from . import provisioner, resolv_conf
        _RUNNERS.update({
            "register": provisioner.register_user,
            "rebuild": provisioner.rebuild_user,
            "remove": provisioner.remove_user,
            "fix_remove": provisioner.remove_instance_only,
            "resolv_conf_set": resolv_conf.apply_service,
            "resolv_conf_clear": resolv_conf.clear_service,
        })
    return _RUNNERS


class TaskCapacityError(RuntimeError):
    """Legacy name kept for import compatibility; the queue uses QueueFullError."""

    reason = "at_capacity"

    def __init__(self, running: int = 0, capacity: int = 0):
        self.running = running
        self.capacity = capacity
        super().__init__("at capacity")


class QueueFullError(TaskCapacityError):
    """The bounded queue is full (20 queued): no row is written."""

    reason = qc.ERR_QUEUE_FULL

    def __init__(self, queued: int, cap: int):
        self.queued = queued
        self.capacity = cap
        super(TaskCapacityError, self).__init__(
            f"queue full: {queued} task(s) queued (limit {cap}); "
            "wait for work to start, or cancel a queued task"
        )


class QueueUnavailableError(TaskCapacityError):
    """The planner heartbeat is stale: refuse rather than accept dead work."""

    reason = qc.ERR_QUEUE_UNAVAILABLE

    def __init__(self, age_s: float | None, stale_after_s: int):
        self.age_s = age_s
        self.stale_after_s = stale_after_s
        super(TaskCapacityError, self).__init__(
            "queue unavailable: the planner heartbeat is "
            + ("missing" if age_s is None else f"{age_s:.0f}s old")
            + f" (stale after {stale_after_s}s)"
        )


def _now():
    return datetime.now(timezone.utc)


def new_task_id() -> str:
    return f"api-{uuid.uuid4().hex[:12]}"


def _json_safe(value: Any) -> Any:
    """A JSON-safe copy: Paths and datetimes become strings (replay accepts str)."""
    return json.loads(json.dumps(value, default=str))


def _encrypt_value(value: Any) -> Any:
    if isinstance(value, str):
        return {"__encrypted__": keys.encrypt(value)}
    return value


def _decrypt_value(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"__encrypted__"}:
        return keys.decrypt(value["__encrypted__"])
    return value


def _runner_descriptor(operation: str, fn: Callable, args: tuple, kwargs: dict) -> dict:
    safe = _json_safe(kwargs)
    for key in list(safe):
        if key in _SECRET_KEYS:
            safe[key] = _encrypt_value(safe[key])
    return {
        "operation": operation,
        "module": getattr(fn, "__module__", ""),
        "qualname": getattr(fn, "__qualname__", ""),
        "args": _json_safe(list(args)),
        "kwargs": safe,
    }


def _decode_kwargs(descriptor: dict) -> tuple[list, dict]:
    args = descriptor.get("args") or []
    kwargs = {k: _decrypt_value(v) for k, v in (descriptor.get("kwargs") or {}).items()}
    return list(args), kwargs


class TaskManager:
    def __init__(self, max_workers: int = 4, ttl_seconds: float = 604800,
                 max_tasks: int = 1000, log_dir: str = "",
                 planner_interval_s: float | None = None,
                 worker_poll_s: float = 1.0):
        self._max_workers = max_workers
        self._lock = threading.Lock()
        self._cancel_events: dict[str, threading.Event] = {}
        # In-process callables (fast path; the DB descriptor is the durable one).
        self._callables: dict[str, tuple[Callable, tuple, dict]] = {}
        self._ttl = ttl_seconds
        self._max_tasks = max_tasks
        self._log_dir = Path(log_dir) if log_dir else Path(".")
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._planner_interval_s = planner_interval_s
        self._worker_poll_s = max(0.005, worker_poll_s)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._loops: list[threading.Thread] = []
        self._started = False
        self._config_cache: tuple[float, dict] | None = None
        self._median_cache: tuple[float, dict, dict] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start the planner, the workers and the watchdog (idempotent)."""
        if self._started:
            return
        self._started = True
        self._stop.clear()
        threads = [
            threading.Thread(target=self._planner_loop, name="task-planner", daemon=True),
            threading.Thread(target=self._watchdog_loop, name="task-watchdog", daemon=True),
        ]
        for idx in range(self._max_workers):
            threads.append(threading.Thread(
                target=self._worker_loop, name=f"task-worker-{idx}", daemon=True))
        for thread in threads:
            thread.start()
            self._loops.append(thread)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for thread in self._loops:
            thread.join(timeout=2.0)
        self._loops.clear()
        self._started = False

    # ------------------------------------------------------------------
    # Configuration / admission
    # ------------------------------------------------------------------
    def config(self, force: bool = False) -> dict:
        now = time.monotonic()
        if not force and self._config_cache and now - self._config_cache[0] < 5.0:
            return self._config_cache[1]
        try:
            row = db.fetch_one("SELECT * FROM registry.queue_config WHERE id=1")
        except Exception:  # noqa: BLE001 — a missing table is not a reason to deadlock
            row = None
        cfg = qc.config_from_row(row)
        self._config_cache = (now, cfg)
        return cfg

    def _heartbeat(self, conn=None) -> tuple[datetime | None, bool]:
        reader = conn or db
        if conn is not None:
            row = conn.execute(
                "SELECT last_dispatch_at FROM registry.queue_state WHERE id=1"
            ).fetchone()
        else:
            row = reader.fetch_one(
                "SELECT last_dispatch_at FROM registry.queue_state WHERE id=1")
        cfg = self.config()
        last = (row or {}).get("last_dispatch_at") if row else None
        if last is None:
            return None, False
        age = (_now() - last).total_seconds()
        return last, age <= int(cfg["heartbeat_stale_s"])

    def _admission_check(self, conn, cfg: dict) -> None:
        """The exact boundary: 20 queued admitted, the 21st refused, no row."""
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ADMIT_CLAIM_LOCK,))
        queued = conn.execute(
            "SELECT count(*) AS n FROM registry.tasks "
            "WHERE status='queued' AND deleted_at IS NULL"
        ).fetchone()["n"]
        if queued >= int(cfg["queue_cap"]):
            raise QueueFullError(queued, int(cfg["queue_cap"]))
        last = conn.execute(
            "SELECT last_dispatch_at FROM registry.queue_state WHERE id=1"
        ).fetchone()
        last = (last or {}).get("last_dispatch_at")
        stale = int(cfg["heartbeat_stale_s"])
        if last is None:
            raise QueueUnavailableError(None, stale)
        age = (_now() - last).total_seconds()
        if age > stale:
            raise QueueUnavailableError(age, stale)

    def admit(self) -> None:
        """Check the admission boundary without writing (register pre-check)."""
        with db.connection() as conn:
            self._admission_check(conn, self.config())

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _row(self, task_id: str) -> dict | None:
        return db.fetch_one("SELECT * FROM registry.tasks WHERE task_id=%s", (task_id,))

    def _update(self, task_id: str, **fields: Any) -> None:
        allowed = {"status", "phase", "progress", "error", "log_ref", "result",
                   "deleted_at", "updated_at", "request", "plan_seq",
                   "started_at", "finished_at", "cancel_requested_at"}
        sets, vals = [], []

        from psycopg.types.json import Jsonb
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key}=%s")
            if key == "result":
                # UUID/datetime objects in a task result are data, not an
                # error: serialize them rather than failing a FINISHED task.
                vals.append(Jsonb(value, dumps=lambda v: json.dumps(v, default=str)))
            elif key == "request":
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
        """A restart fails ONLY ``running`` rows; the queue survives (R2.2).

        The runtime resources those rows created are cleaned up afterwards by
        the compensating cleanup. ``queued``/``planned`` rows are the durable
        backlog and are left for the planner and workers.
        """
        rows = db.fetch_all(
            "SELECT task_id FROM registry.tasks WHERE status='running' AND deleted_at IS NULL"
        )
        for row in rows:
            self._update(row["task_id"], status=qc.FAILED,
                         error="interrupted by restart", finished_at=_now())
            try:
                self.compensating_cleanup(row["task_id"])
            except Exception:  # noqa: BLE001 — cleanup is best-effort, never fatal
                _log.exception("restart cleanup failed for %s", row["task_id"])
        return len(rows)

    def terminalize_unknown(self, reason: str = "interrupted by restart") -> int:
        rows = db.fetch_all("SELECT task_id FROM registry.tasks WHERE status='unknown'")
        for row in rows:
            self._update(row["task_id"], status=qc.FAILED, error=reason, finished_at=_now())
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
               created_service: bool = False,
               previous_status: str | None = None,
               previous_failure: dict | None = None,
               **kwargs: Any) -> str:
        """Admit the task into the queue (``queued``); workers execute it later.

        Admission is checked BEFORE the row is written, under the same
        transaction-scoped lock as the insert, so a refused submit leaves no
        task behind and two concurrent submitters cannot both take slot 20.
        """
        task_id = task_id or new_task_id()
        kind, operation = KIND_BY_TYPE.get(task_type, ("deploy", task_type))
        log_file = str(self._log_dir / f"task-{task_id}.log")
        # Callables that take the identity get it from the submit call (the
        # register handler passes the reserved service/procedure ids here, not
        # inside its payload kwargs). Done before the descriptor so a restarted
        # replay carries the same values.
        try:
            import inspect as _inspect
            params = _inspect.signature(fn).parameters
            if "service_id" in params and service_id:
                kwargs.setdefault("service_id", str(service_id))
            if "procedure_id" in params and procedure_id:
                kwargs.setdefault("procedure_id", str(procedure_id))
            if "_task_id" in params:
                kwargs.setdefault("_task_id", task_id)
        except (TypeError, ValueError):
            pass
        descriptor = _runner_descriptor(operation, fn, args, kwargs)
        # What the compensating cleanup must restore (R6.1/R6.2). Stored with
        # the row so a restart's cleanup still knows it.
        descriptor["created"] = bool(created_service)
        descriptor["previous_status"] = previous_status
        descriptor["previous_failure"] = _json_safe(previous_failure) if previous_failure else None

        cfg = self.config()
        with db.connection() as conn:
            self._admission_check(conn, cfg)
            proc = uuid.UUID(str(procedure_id)) if procedure_id else uuid.uuid4()
            if persist:
                registry.insert_task(
                    conn, task_id=task_id, procedure_id=proc,
                    service_id=uuid.UUID(str(service_id)) if service_id else None,
                    kind=kind, operation=operation, request=descriptor,
                )
                conn.execute(
                    "UPDATE registry.tasks SET log_ref=%s WHERE task_id=%s",
                    (log_file, task_id),
                )
            else:
                # The register path reserved the row before the 202 (R6.1).
                conn.execute(
                    "UPDATE registry.tasks SET request=%s, log_ref=%s, "
                    "status='queued', updated_at=now() WHERE task_id=%s",
                    (_jsonb(descriptor), log_file, task_id),
                )
            self._callables[task_id] = (fn, tuple(args), dict(kwargs))
            self._cancel_events[task_id] = threading.Event()
        self._wake.set()
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

    def mark_running_now(self, task_id: str) -> None:
        """Take a synchronously-executed row out of the planner's reach.

        The blocking (``sync=true``) API paths still run inline; without this
        the planner could plan their row and a worker could execute it a second
        time. The CAS keeps a concurrently-cancelled row cancelled.
        """
        db.execute(
            "UPDATE registry.tasks SET status='running', started_at=%s, updated_at=%s "
            "WHERE task_id=%s AND status='queued'",
            (_now(), _now(), task_id),
        )

    def cancel(self, task_id: str) -> bool:
        """Request cancellation; the caller polls for the settle bound (R6.3).

        ``queued``/``planned`` rows end immediately (the plan is simply
        smaller); ``running`` rows get ``cancel_requested_at`` and their worker
        is signalled, then the worker's abort path settles the row.
        """
        row = self._row(task_id)
        if not row:
            return False
        if row["status"] in qc.TERMINAL_STATES or row["status"] == qc.UNKNOWN:
            return False
        if row["operation"] == "fix_remove":
            return False
        if row["status"] in (qc.QUEUED, qc.PLANNED):
            executed = db.execute(
                "UPDATE registry.tasks SET status=%s, error=%s, finished_at=%s, "
                "updated_at=%s WHERE task_id=%s AND status IN ('queued','planned')",
                (qc.CANCELLED, "cancelled by operator", _now(), _now(), task_id),
            )
            if executed:
                # The card effects follow the cancel (R6.1): a creation's card
                # disappears. Nothing ran, so the only cleanup needed is the
                # reserved identity — never a runtime teardown of the service
                # the task had not started touching.
                descriptor = row.get("request") or {}
                try:
                    if isinstance(descriptor, dict) and descriptor.get("created"):
                        self.compensating_cleanup(task_id)
                    elif row.get("operation") == "register" and row.get("service_id"):
                        # An existing instance was reserved as `building` before
                        # the 202; revert it and leave its containers alone.
                        from . import provisioner
                        provisioner.restore_projection(
                            service_id=str(row["service_id"]),
                            previous_status=descriptor.get("previous_status"),
                            previous_failure=descriptor.get("previous_failure"),
                        )
                except Exception:  # noqa: BLE001
                    _log.exception("cleanup after queued/planned cancel failed")
                self._wake.set()
            return bool(executed)
        # running: record the request time (the cancellation clock) and signal.
        executed = db.execute(
            "UPDATE registry.tasks SET cancel_requested_at=now(), updated_at=now() "
            "WHERE task_id=%s AND status='running' AND cancel_requested_at IS NULL",
            (task_id,),
        )
        if not executed:
            # Already requested: still a valid control call, not a new one.
            return self._row(task_id) is not None
        ev = self._cancel_events.get(task_id)
        if ev:
            ev.set()
        self._wake.set()
        return True

    def cancel_and_wait(self, task_id: str) -> dict[str, Any]:
        """Cancel and poll to terminal within the per-kind cancellation timeout."""
        row = self._row(task_id)
        if not row:
            return {"task_id": task_id, "status": None, "confirmed": False}
        timeout = int(self.config().get(
            f"cancel_timeout_{row.get('kind') or 'deploy'}",
            qc.CANCEL_TIMEOUTS.get(row.get("kind") or "deploy", 300)))
        requested = self.cancel(task_id)
        if not requested:
            current = self._row(task_id)
            status = current["status"] if current else None
            return {"task_id": task_id, "status": status,
                    "confirmed": status in qc.TERMINAL_STATES}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self._row(task_id)
            if current and current["status"] in qc.TERMINAL_STATES:
                return {"task_id": task_id, "status": current["status"],
                        "confirmed": True, "error": current.get("error")}
            time.sleep(0.25)
        current = self._row(task_id)
        return {
            "task_id": task_id,
            "status": current["status"] if current else None,
            "confirmed": False,
            "error": qc.ERR_CANCELLATION_UNCONFIRMED,
            "timeout_s": timeout,
        }

    def finish(self, task_id: str, status: str, error: str = "") -> bool:
        if status not in (qc.COMPLETED, qc.FAILED):
            return False
        executed = db.execute(
            "UPDATE registry.tasks SET status=%s, error=%s, finished_at=%s, "
            "updated_at=%s WHERE task_id=%s "
            "AND status IN ('queued','planned','running','unknown')",
            (status, str(error) if status == qc.FAILED else None, _now(), _now(), task_id),
        )
        self._wake.set()
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
            "plan_seq": row.get("plan_seq"),
            "started_at": row["started_at"].isoformat() if row.get("started_at") else None,
            "finished_at": row["finished_at"].isoformat() if row.get("finished_at") else None,
            "cancel_requested_at": (row["cancel_requested_at"].isoformat()
                                    if row.get("cancel_requested_at") else None),
            "target": row.get("target_snapshot") or (
                {"service_id": str(row["service_id"])} if row["service_id"] else {}
            ),
            "deleted_at": row["deleted_at"].isoformat() if row["deleted_at"] else None,
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }

    # ------------------------------------------------------------------
    # Planner (one serialized routine; T2/R3)
    # ------------------------------------------------------------------
    def _planner_loop(self) -> None:
        interval = self._planner_interval_s
        if interval is None:
            interval = float(self.config().get("planner_interval_s", 2))
        while not self._stop.is_set():
            try:
                self.planner_tick()
            except Exception:  # noqa: BLE001 — the queue must outlive one bad tick
                _log.exception("planner tick failed")
            self._wake.wait(max(0.05, interval))
            self._wake.clear()

    def _touch_heartbeat(self, conn, planner_id: str) -> None:
        conn.execute(
            "INSERT INTO registry.queue_state (id, last_dispatch_at, planner_id, updated_at) "
            "VALUES (1, now(), %s, now()) "
            "ON CONFLICT (id) DO UPDATE SET last_dispatch_at=now(), "
            "planner_id=EXCLUDED.planner_id, updated_at=now()",
            (planner_id,),
        )

    def planner_tick(self) -> None:
        """Beat, then (under the plan lock) form a plan if the plan is empty."""
        cfg = self.config()
        planner_id = f"{os.getpid()}"
        with db.connection() as conn:
            locked = conn.execute(
                "SELECT pg_try_advisory_xact_lock(%s) AS ok", (_PLAN_LOCK,)
            ).fetchone()["ok"]
            if not locked:
                # Another process is planning; its heartbeat covers this tick.
                return
            self._touch_heartbeat(conn, planner_id)
            planned = conn.execute(
                "SELECT count(*) AS n FROM registry.tasks "
                "WHERE status='planned' AND deleted_at IS NULL"
            ).fetchone()["n"]
            if planned:
                return  # the plan is not refilled (D2)
            self._write_plan(conn, cfg)

    def _write_plan(self, conn, cfg: dict) -> int:
        rows = conn.execute(
            "SELECT t.task_id, t.created_at, "
            "       COALESCE(u.username, '') AS user_key "
            "FROM registry.tasks t "
            "LEFT JOIN registry.services s ON s.id = t.service_id "
            "LEFT JOIN registry.api_users u ON u.id = s.owner_api_user_id "
            "WHERE t.status='queued' AND t.deleted_at IS NULL "
            # The register path reserves its row before the request descriptor
            # is written; a row without one is not yet executable and must not
            # enter a plan (its submit() follows in the same request).
            "AND t.request IS NOT NULL "
            "ORDER BY t.created_at, t.task_id"
        ).fetchall()
        if not rows:
            return 0
        plan_size = int(cfg["plan_size"])
        span = int(cfg["span_s"])
        anchor = rows[0]["created_at"]
        candidates = rows
        extended = 0
        while True:
            horizon = anchor + _seconds(span * (extended + 1))
            candidates = [r for r in rows if r["created_at"] < horizon]
            if len(candidates) >= plan_size or len(candidates) >= len(rows):
                break
            extended += 1
        selected = _fair_select(candidates, plan_size)
        for seq, row in enumerate(selected, start=1):
            conn.execute(
                "UPDATE registry.tasks SET status='planned', plan_seq=%s, updated_at=now() "
                "WHERE task_id=%s AND status='queued'",
                (seq, row["task_id"]),
            )
        if selected:
            _log.info("planned %d task(s) (plan_seq 1..%d)", len(selected), len(selected))
        return len(selected)

    # ------------------------------------------------------------------
    # Workers (consumer-only; T3/R4)
    # ------------------------------------------------------------------
    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                row = self._claim_next()
            except Exception:  # noqa: BLE001
                _log.exception("claim failed")
                row = None
            if row is None:
                self._wake.wait(self._worker_poll_s)
                self._wake.clear()
                continue
            # A claim empties (or shrinks) the plan: the next plan may form now.
            self._wake.set()
            try:
                self._execute(row)
            except Exception:  # noqa: BLE001 — a worker must not die
                _log.exception("worker execution failed for %s", row["task_id"])

    def _claim_next(self) -> dict | None:
        cfg = self.config()
        with db.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ADMIT_CLAIM_LOCK,))
            running = conn.execute(
                "SELECT count(*) AS n FROM registry.tasks "
                "WHERE status='running' AND deleted_at IS NULL"
            ).fetchone()["n"]
            if running >= min(int(cfg["workers"]), self._max_workers):
                return None
            row = conn.execute(
                "SELECT * FROM registry.tasks "
                "WHERE status='planned' AND deleted_at IS NULL "
                "ORDER BY plan_seq NULLS LAST, created_at, task_id "
                "LIMIT 1 FOR UPDATE SKIP LOCKED"
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE registry.tasks SET status='running', started_at=now(), "
                "updated_at=now() WHERE task_id=%s",
                (row["task_id"],),
            )
            row["status"] = qc.RUNNING
            return row

    def _execute(self, row: dict) -> None:
        from . import docker_ops

        task_id = row["task_id"]
        # A cancel that landed between the claim and here never runs the work.
        current = self._row(task_id)
        if not current or current["status"] != qc.RUNNING:
            return
        if current.get("cancel_requested_at"):
            self._settle(task_id, qc.CANCELLED, "cancelled by operator")
            return

        log_file = current.get("log_ref")
        if log_file:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        docker_ops.set_task_log_file(log_file)
        event = self._cancel_events.get(task_id)
        if event is None:
            event = threading.Event()
            self._cancel_events[task_id] = event
        docker_ops.set_cancel_event(event)
        try:
            fn, args, kwargs = self._resolve_runner(task_id, current)
            import inspect as _inspect
            if "_task_id" in _inspect.signature(fn).parameters:
                kwargs.setdefault("_task_id", task_id)
            result = fn(*args, **kwargs)
            latest = self._row(task_id)
            if latest and latest["status"] == qc.CANCELLED:
                pass  # cancel already won the row
            elif latest and latest.get("cancel_requested_at"):
                status, reason = self._abort_outcome(task_id)
                self._settle(task_id, status, reason)
            else:
                self._settle(task_id, qc.COMPLETED, None,
                             result=_redact(result))
        except docker_ops.CommandCancelled:
            status, reason = self._abort_outcome(task_id)
            self._settle(task_id, status, reason)
        except Exception as exc:  # noqa: BLE001
            latest = self._row(task_id)
            if latest and (latest.get("cancel_requested_at")
                           or latest["status"] == qc.CANCELLED):
                status, reason = self._abort_outcome(task_id)
                self._settle(task_id, status,
                             f"{reason}: {exc}" if exc else reason)
            else:
                self._settle(task_id, qc.FAILED, str(exc))
        finally:
            docker_ops.clear_cancel_event()
            docker_ops.clear_task_log_file()
            self._cancel_events.pop(task_id, None)
            self._callables.pop(task_id, None)
            self._wake.set()

    def _resolve_runner(self, task_id: str, row: dict) -> tuple[Callable, list, dict]:
        """The in-process callable first; the persisted descriptor after restart."""
        with self._lock:
            local = self._callables.get(task_id)
        if local:
            return local
        descriptor = row.get("request") or {}
        if not isinstance(descriptor, dict):
            raise RuntimeError(f"task {task_id} has no re-executable request")
        operation = descriptor.get("operation") or row.get("operation")
        args, kwargs = _decode_kwargs(descriptor)
        fn = None
        module_name = descriptor.get("module") or ""
        qualname = descriptor.get("qualname") or ""
        if module_name and qualname and "<lambda>" not in qualname:
            try:
                module = __import__(module_name, fromlist=["*"])
                obj: Any = module
                for part in qualname.split("."):
                    obj = getattr(obj, part)
                fn = obj
            except Exception:  # noqa: BLE001 — fall back to the operation map
                fn = None
        if fn is None:
            fn = _runners().get(str(operation))
        if fn is None:
            raise RuntimeError(
                f"task {task_id}: no runner for operation {operation!r}")
        return fn, args, kwargs

    def _abort_outcome(self, task_id: str) -> tuple[str, str]:
        """Distinguish an operator cancel from a run-timeout abort (R5.1/R6.2).

        The watchdog records a run timeout by writing its reason to the row and
        setting ``cancel_requested_at``; that abort is a FAILURE, while an
        operator cancel is CANCELLED.
        """
        row = self._row(task_id) or {}
        reason = (row.get("error") or "").strip()
        if reason.startswith("run timeout"):
            return qc.FAILED, reason
        return qc.CANCELLED, "cancelled by operator"

    def _settle(self, task_id: str, status: str, error: str | None,
                result: Any = None) -> None:
        """Write the terminal row; a sticky cancel can never be overwritten."""
        fields: dict[str, Any] = {"status": status, "finished_at": _now()}
        if error is not None:
            fields["error"] = error
        if result is not None:
            fields["result"] = result
        if status == qc.COMPLETED:
            fields["error"] = None
        self._update(task_id, **fields)
        if status in (qc.FAILED, qc.CANCELLED):
            try:
                self.compensating_cleanup(task_id)
            except Exception:  # noqa: BLE001
                _log.exception("compensating cleanup failed for %s", task_id)

    # ------------------------------------------------------------------
    # Watchdog + compensating cleanup (T4/R5, R6.2)
    # ------------------------------------------------------------------
    def _watchdog_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._watchdog_tick()
            except Exception:  # noqa: BLE001
                _log.exception("watchdog tick failed")
            self._stop.wait(1.0)

    def _watchdog_tick(self) -> None:
        cfg = self.config()
        rows = db.fetch_all(
            "SELECT * FROM registry.tasks WHERE status='running' AND deleted_at IS NULL"
        )
        now = _now()
        for row in rows:
            kind = row.get("kind") or "deploy"
            started = row.get("started_at")
            requested = row.get("cancel_requested_at")
            if started is None and requested is None:
                continue
            if requested is None:
                timeout = int(cfg.get(f"run_timeout_{kind}",
                                      qc.RUN_TIMEOUTS.get(kind, 1800)))
                if started is not None and (now - started).total_seconds() > timeout:
                    db.execute(
                        "UPDATE registry.tasks SET cancel_requested_at=now(), "
                        "error=%s, updated_at=now() WHERE task_id=%s "
                        "AND status='running' AND cancel_requested_at IS NULL",
                        (f"run timeout after {timeout}s", row["task_id"]),
                    )
                    ev = self._cancel_events.get(row["task_id"])
                    if ev:
                        ev.set()
                continue
            timeout = int(cfg.get(f"cancel_timeout_{kind}",
                                  qc.CANCEL_TIMEOUTS.get(kind, 300)))
            if (now - requested).total_seconds() <= timeout:
                continue
            # The worker did not settle within the cancellation bound: the
            # supervisor takes the row over (it stays visible as pinned until
            # this write), runs the runtime cleanup and fails it loudly.
            current = self._row(row["task_id"])
            if not current or current["status"] != qc.RUNNING:
                continue
            self._settle(
                row["task_id"], qc.FAILED,
                f"cancellation did not settle within {timeout}s; "
                "interrupted, runtime cleanup attempted",
            )

    def compensating_cleanup(self, task_id: str) -> None:
        """Remove the RUNTIME resources a failed/cancelled task created.

        Containers and networks named by the instance row are removed; a
        card-creating task removes its reserved identity so the card
        disappears. Generated files (source-project and rendered) are never
        touched — the house rule.
        """
        from . import provisioner
        row = self._row(task_id)
        if not row:
            return
        operation = row.get("operation") or ""
        service_id = str(row["service_id"]) if row.get("service_id") else None
        if not service_id:
            return
        # Only container-work operations have runtime resources to reconcile.
        # A failed resolv.conf update touched a file inside running containers;
        # tearing the service down for it would be worse than the failure.
        if operation not in ("register", "rebuild", "remove", "fix_remove"):
            return
        descriptor = row.get("request") or {}
        creation = bool(descriptor.get("created")) if isinstance(descriptor, dict) else False
        provisioner.compensate(
            service_id=service_id, operation=operation, creation=creation,
            previous_status=descriptor.get("previous_status")
            if isinstance(descriptor, dict) else None,
            previous_failure=descriptor.get("previous_failure")
            if isinstance(descriptor, dict) else None,
        )

    # ------------------------------------------------------------------
    # Telemetry / stats (T6/R7)
    # ------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        cfg = self.config()

        counts = {state: 0 for state in qc.STATES}
        for row in db.fetch_all(
            "SELECT status, count(*) AS n FROM registry.tasks "
            "WHERE deleted_at IS NULL GROUP BY status"
        ):
            counts[row["status"]] = row["n"]

        last, alive = self._heartbeat()
        stale_after = int(cfg["heartbeat_stale_s"])
        age = None if last is None else max(0.0, (_now() - last).total_seconds())

        medians, samples, global_median = self._kind_medians()

        active = db.fetch_all(
            "SELECT task_id, kind, status, plan_seq, created_at, started_at, finished_at "
            "FROM registry.tasks WHERE status IN ('queued','planned','running') "
            "AND deleted_at IS NULL ORDER BY created_at, task_id"
        )
        queued_by_order = [r for r in active if r["status"] == qc.QUEUED]
        positions = {r["task_id"]: idx for idx, r in enumerate(queued_by_order)}
        workers = max(1, min(int(cfg["workers"]), self._max_workers))
        tasks: dict[str, Any] = {}
        now = _now()
        for row in active:
            kind = row.get("kind") or "deploy"
            estimate = medians.get(kind, global_median) or 0.0
            position = positions.get(row["task_id"])
            if row["status"] == qc.QUEUED:
                start = (position or 0) / workers * estimate
                finish = start + estimate
            elif row["status"] == qc.PLANNED:
                start = 0.0
                finish = estimate
            else:
                elapsed = 0.0
                if row.get("started_at"):
                    elapsed = max(0.0, (now - row["started_at"]).total_seconds())
                start = 0.0
                finish = max(0.0, estimate - elapsed)
            tasks[row["task_id"]] = {
                "status": row["status"],
                "kind": kind,
                "plan_seq": row.get("plan_seq"),
                "position": position,
                "eta_start_s": round(start, 1),
                "eta_finish_s": round(finish, 1),
                "started_at": row["started_at"].isoformat() if row.get("started_at") else None,
                "finished_at": row["finished_at"].isoformat() if row.get("finished_at") else None,
            }

        pinned = 0
        for row in db.fetch_all(
            "SELECT kind, cancel_requested_at FROM registry.tasks "
            "WHERE status='running' AND cancel_requested_at IS NOT NULL "
            "AND deleted_at IS NULL"
        ):
            kind = row.get("kind") or "deploy"
            limit = int(cfg.get(f"cancel_timeout_{kind}",
                                qc.CANCEL_TIMEOUTS.get(kind, 300)))
            if (now - row["cancel_requested_at"]).total_seconds() > limit:
                pinned += 1

        return {
            "queue": "registry",
            "counts": counts,
            "workers": int(cfg["workers"]),
            "pinned_workers": pinned,
            "cap": int(cfg["queue_cap"]),
            "span_s": int(cfg["span_s"]),
            "plan_size": int(cfg["plan_size"]),
            "heartbeat": {
                "last_dispatch_at": last.isoformat() if last else None,
                "age_s": None if age is None else round(age, 1),
                "alive": alive,
                "stale_after_s": stale_after,
            },
            "planner_interval_s": int(cfg["planner_interval_s"]),
            "tasks": tasks,
            "kinds": {
                kind: {"median_s": round(value, 1), "samples": samples.get(kind, 0)}
                for kind, value in medians.items()
            },
            "config": cfg,
        }

    def _kind_medians(self) -> tuple[dict[str, float], dict[str, int], float | None]:
        """Median run time per kind over the last 50 finished rows (5 s cache)."""
        now = time.monotonic()
        if self._median_cache and now - self._median_cache[0] < _MEDIAN_CACHE_TTL:
            return self._median_cache[1], self._median_cache[2], self._median_cache[3]
        rows = db.fetch_all(
            "SELECT kind, EXTRACT(EPOCH FROM (finished_at - started_at)) AS secs, "
            "       row_number() OVER (PARTITION BY kind ORDER BY finished_at DESC) AS rn "
            "FROM registry.tasks "
            "WHERE status IN ('completed','failed') AND started_at IS NOT NULL "
            "AND finished_at IS NOT NULL AND deleted_at IS NULL"
        )
        by_kind: dict[str, list[float]] = {}
        global_vals: list[float] = []
        for row in rows:
            secs = float(row["secs"]) if row["secs"] is not None else None
            if secs is None:
                continue
            global_vals.append(secs)
            if int(row["rn"]) <= 50:
                by_kind.setdefault(row["kind"], []).append(secs)
        medians = {kind: _median(vals) for kind, vals in by_kind.items() if vals}
        samples = {kind: len(vals) for kind, vals in by_kind.items()}
        global_median = _median(global_vals[-50:]) if global_vals else None
        self._median_cache = (now, medians, samples, global_median)
        return medians, samples, global_median


def _seconds(value: int):
    from datetime import timedelta
    return timedelta(seconds=value)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _fair_select(rows: list[dict], plan_size: int) -> list[dict]:
    """Per-user deficit round-robin with per-user FIFO (R4.1).

    One slot per user per round; each user's tasks are taken in enqueue order.
    A user's second task never jumps another user's first. The round order is
    by each user's next task time, so the globally earliest task leads.
    """
    pending: dict[str, list[dict]] = {}
    for row in sorted(rows, key=lambda r: (r["created_at"], r["task_id"])):
        pending.setdefault(row.get("user_key") or "", []).append(row)
    selected: list[dict] = []
    while len(selected) < plan_size and pending:
        order = sorted(
            (k for k, v in pending.items() if v),
            key=lambda k: (pending[k][0]["created_at"], k),
        )
        if not order:
            break
        for user in order:
            if len(selected) >= plan_size:
                break
            queue = pending[user]
            if not queue:
                continue
            selected.append(queue.pop(0))
        pending = {k: v for k, v in pending.items() if v}
    return selected


def _jsonb(value: Any):
    from psycopg.types.json import Jsonb
    return Jsonb(value, dumps=lambda v: json.dumps(v, default=str))


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

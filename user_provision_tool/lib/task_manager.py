"""lib/task_manager.py — lightweight async task pool for provision-api.

Provides a thread-pool-backed task queue so long-running Docker operations
(register, rebuild, remove) don't block the API.  Each task gets a UUID,
runs on a worker thread, and reports status through in-memory storage
with disk persistence for surviving restarts.

Usage::

    from lib.task_manager import task_manager

    task_id = task_manager.submit("register", provisioner.register_user, **kwargs)
    # → returns immediately with task_id

    status = task_manager.get(task_id)
    # → {"task_id": "...", "type": "register", "status": "running", ...}

    task_manager.cancel(task_id)
    # → marks as cancelled; the thread will stop at the next docker_ops call
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable


class Task:
    """A single task tracked by the TaskManager."""

    __slots__ = (
        "task_id", "type", "status", "created_at", "updated_at",
        "result", "error", "_future", "_cancel_event", "log_file",
    )

    def __init__(self, task_id: str, task_type: str, future: Future = None, log_file: str = ""):
        self.task_id = task_id
        self.type = task_type          # "register" | "rebuild" | "remove"
        self.status = "pending"        # pending → running → completed | failed | cancelled
        self.created_at = time.time()
        self.updated_at = self.created_at
        self.result: Any = None
        self.error: str | None = None
        self._future = future
        self._cancel_event = threading.Event()
        self.log_file = log_file       # path to per-task log file

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "type": self.type,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "result": self.result,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        """Reconstruct a Task from a persisted dict (no future/cancel_event)."""
        task = cls(
            task_id=d["task_id"],
            task_type=d.get("type", "unknown"),
            log_file=d.get("log_file", ""),
        )
        task.status = d.get("status", "unknown")
        task.created_at = d.get("created_at", 0)
        task.updated_at = d.get("updated_at", 0)
        task.result = d.get("result")
        task.error = d.get("error")
        return task

    def to_persist_dict(self) -> dict[str, Any]:
        """Dict for disk persistence (includes log_file path)."""
        d = self.to_dict()
        d["log_file"] = self.log_file
        return d


class TaskManager:
    """In-memory task pool backed by a ThreadPoolExecutor.

    Each task gets a dedicated log file under *log_dir*.  Tasks older than
    *ttl_seconds* are cleaned up, and if the total number exceeds *max_tasks*,
    the oldest are evicted first.  Task log files are deleted alongside their
    task entries.

    Task metadata is persisted to ``task_registry.json`` in *log_dir* so
    that task history survives provision-api restarts (up to TTL).
    """

    def __init__(
        self,
        max_workers: int = 4,
        ttl_seconds: float = 604800,   # 1 week
        max_tasks: int = 1000,
        log_dir: str = "",
    ):
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._max_tasks = max_tasks
        self._log_dir = Path(log_dir) if log_dir else Path(".")
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._registry_file = self._log_dir / "task_registry.json"

        # Restore tasks from disk
        self._restore_from_disk()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _restore_from_disk(self) -> None:
        """Load previously persisted tasks from the registry JSON file."""
        if not self._registry_file.exists():
            return
        try:
            with open(self._registry_file, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return

        now = time.time()
        restored = 0
        for task_dict in data.get("tasks", []):
            task = Task.from_dict(task_dict)
            # Skip tasks that have already exceeded TTL
            if task.status in ("completed", "failed", "cancelled", "unknown"):
                if (now - task.updated_at) > self._ttl:
                    self._delete_task_log(task.log_file)
                    continue
            # Mark any non-terminal tasks as "unknown" since they didn't survive restart
            if task.status in ("pending", "running"):
                task.status = "unknown"
                task.error = "provision-api restarted — task state lost"
                task.updated_at = now
            self._tasks[task.task_id] = task
            restored += 1
        if restored > 0:
            print(f"[task_manager] Restored {restored} tasks from {self._registry_file}")

    def _persist_to_disk(self) -> None:
        """Save current task metadata to the registry JSON file."""
        try:
            tasks_data = [t.to_persist_dict() for t in self._tasks.values()]
            with open(self._registry_file, "w") as f:
                json.dump({"tasks": tasks_data, "updated_at": time.time()}, f, default=str)
        except OSError:
            pass  # Don't crash if we can't persist

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit(
        self,
        task_type: str,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> str:
        """Submit *fn* for background execution.  Returns a task UUID immediately."""
        task_id = uuid.uuid4().hex[:12]
        log_file = str(self._log_dir / f"task-{task_id}.log")

        future = Future()
        task = Task(task_id, task_type, future, log_file=log_file)
        with self._lock:
            self._tasks[task_id] = task
            self._cleanup_excess()
            self._persist_to_disk()

        real_future = self._executor.submit(
            self._run_task, task_id, task_type, fn, *args, **kwargs
        )
        task._future = real_future
        return task_id

    def get(self, task_id: str) -> dict[str, Any] | None:
        """Return the task status dict, or None if not found / cleaned up."""
        self._cleanup_stale()
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None:
            return None
        return task.to_dict()

    def get_log_file(self, task_id: str) -> str | None:
        """Return the per-task log file path, or None if task not found."""
        with self._lock:
            task = self._tasks.get(task_id)
        return task.log_file if task else None

    def cancel(self, task_id: str) -> bool:
        """Request cancellation of a pending or running task."""
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None:
            return False
        if task.status in ("completed", "failed", "cancelled"):
            return False
        task._cancel_event.set()
        task.status = "cancelled"
        task.updated_at = time.time()
        self._persist_to_disk()
        return True

    def list_all(self) -> list[dict[str, Any]]:
        """Return status dicts for all tasks in the pool, newest first."""
        self._cleanup_stale()
        with self._lock:
            tasks = sorted(
                self._tasks.values(),
                key=lambda t: t.created_at,
                reverse=True,
            )
        return [t.to_dict() for t in tasks]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_task(self, task_id: str, task_type: str, fn: Callable, *args: Any, **kwargs: Any) -> None:
        """Wrapper executed on a worker thread."""
        # Set up thread-local task log file so docker_ops writes to it
        from . import docker_ops

        with self._lock:
            task = self._tasks.get(task_id)
        if task is None:
            return
        docker_ops.set_task_log_file(task.log_file)

        task.status = "running"
        task.updated_at = time.time()
        self._persist_to_disk()

        cancel_event = kwargs.pop("_cancel_event", None)
        if cancel_event is not None:
            task._cancel_event = cancel_event

        try:
            result = fn(*args, **kwargs)
            task.result = result
            task.status = "completed"
        except Exception as e:
            task.error = str(e)
            task.status = "failed"
        finally:
            task.updated_at = time.time()
            docker_ops.clear_task_log_file()
            self._persist_to_disk()

    def _cleanup_stale(self) -> None:
        """Remove finished tasks older than _ttl and delete their log files."""
        now = time.time()
        with self._lock:
            stale = [
                (tid, t) for tid, t in self._tasks.items()
                if t.status in ("completed", "failed", "cancelled", "unknown")
                and (now - t.updated_at) > self._ttl
            ]
            if not stale:
                return
            for tid, t in stale:
                self._delete_task_log(t.log_file)
                del self._tasks[tid]
            self._persist_to_disk()

    def _cleanup_excess(self) -> None:
        """Remove oldest tasks if total exceeds _max_tasks and delete their logs."""
        if len(self._tasks) <= self._max_tasks:
            return
        # Sort oldest-first, remove the overflow
        sorted_tasks = sorted(
            self._tasks.items(),
            key=lambda item: item[1].created_at,
        )
        to_remove = len(self._tasks) - self._max_tasks
        for tid, t in sorted_tasks[:to_remove]:
            self._delete_task_log(t.log_file)
            del self._tasks[tid]
        self._persist_to_disk()

    @staticmethod
    def _delete_task_log(log_file: str) -> None:
        if log_file and Path(log_file).exists():
            try:
                Path(log_file).unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Singleton instance
# ---------------------------------------------------------------------------

task_manager = TaskManager(
    ttl_seconds=int(os.environ.get("TASK_TTL_SECONDS", "604800")),
    max_tasks=int(os.environ.get("TASK_MAX_COUNT", "1000")),
    log_dir=os.environ.get(
        "TASK_LOG_DIR",
        str(Path(os.environ.get("GENERATED_DIR", "./generated")) / "task_logs"),
    ),
)

"""Unit tests for the async task pool (lib/task_manager.py)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


class TestTaskManager:
    """Verify the async task pool: submit → status → complete → cancel."""

    def test_submit_and_complete(self):
        from lib.task_manager import TaskManager
        tm = TaskManager(max_workers=2)

        def slow_add(a, b):
            import time
            time.sleep(0.05)
            return a + b

        task_id = tm.submit("test", slow_add, 1, 2)
        # Namespaced since GRILLING G28: `api-` + 12 hex chars. The prefix is what
        # makes an id self-describing in a list that also holds the gateway's `gt-` ids.
        assert task_id.startswith("api-") and len(task_id) == 16

        # Poll until completed
        import time
        for _ in range(50):
            task = tm.get(task_id)
            if task["status"] in ("completed", "failed"):
                break
            time.sleep(0.01)

        assert task["status"] == "completed"
        assert task["result"] == 3
        assert task["error"] is None
        assert task["type"] == "test"

    def test_submit_and_fail(self):
        from lib.task_manager import TaskManager
        tm = TaskManager(max_workers=2)

        def raiser():
            raise ValueError("boom")

        task_id = tm.submit("test", raiser)
        import time
        for _ in range(50):
            task = tm.get(task_id)
            if task["status"] in ("completed", "failed"):
                break
            time.sleep(0.01)

        assert task["status"] == "failed"
        assert "boom" in (task["error"] or "")
        assert task["result"] is None

    def test_cancel_pending_task(self):
        """A queued row is cancellable before a worker picks it up."""
        import uuid
        from lib import db, registry
        from lib.task_manager import TaskManager

        tm = TaskManager(max_workers=1)
        task_id = "api-pending01"
        with db.connection() as conn:
            registry.insert_task(
                conn, task_id=task_id, procedure_id=uuid.uuid4(),
                service_id=None, kind="deploy", operation="register",
            )
        assert tm.cancel(task_id) is True
        assert tm.get(task_id)["status"] == "cancelled"

    def test_submit_refuses_at_capacity(self):
        """A saturated pool must NOT accept-and-queue forever (doc §4d)."""
        import time
        from lib.task_manager import TaskCapacityError, TaskManager
        tm = TaskManager(max_workers=1)
        started = __import__("threading").Event()

        def blocker():
            started.set()
            time.sleep(10)

        tm.submit("block", blocker)
        started.wait(2)
        # The single worker is busy → the next submit is refused, not queued.
        try:
            tm.submit("test", lambda: 42)
            assert False, "expected TaskCapacityError"
        except TaskCapacityError as exc:
            assert exc.running == 1 and exc.capacity == 1
            assert "at capacity" in str(exc)
            assert "cancel a task" in str(exc)

    def test_capacity_refusal_is_not_counted_as_a_task(self):
        """A refused submit must leave no task behind."""
        import time
        from lib.task_manager import TaskCapacityError, TaskManager
        tm = TaskManager(max_workers=1)
        started = __import__("threading").Event()

        def blocker():
            started.set()
            time.sleep(10)

        tm.submit("block", blocker)
        started.wait(2)
        before = len(tm.list_all())
        try:
            tm.submit("test", lambda: 42)
        except TaskCapacityError:
            pass
        assert len(tm.list_all()) == before

    def test_task_carries_its_service_id(self):
        """A task's target is the service identity (T4/R4), not a name tuple."""
        from lib.task_manager import TaskManager
        from registry_support import add_user

        service_id, _proc = add_user(
            {"user_name": "alice", "service_name": "n8n", "label": "0"}
        )
        tm = TaskManager(max_workers=2)
        tid = tm.submit("register", lambda **kw: None, service_id=service_id)
        assert tm.get(tid)["target"] == {"service_id": service_id}

    def test_get_nonexistent_returns_none(self):
        from lib.task_manager import TaskManager
        tm = TaskManager()
        assert tm.get("nonexistent") is None

    def test_cancel_nonexistent_returns_false(self):
        from lib.task_manager import TaskManager
        tm = TaskManager()
        assert tm.cancel("nonexistent") is False

    def test_cancel_completed_returns_false(self):
        from lib.task_manager import TaskManager
        tm = TaskManager(max_workers=2)
        task_id = tm.submit("test", lambda: None)
        import time
        for _ in range(50):
            if tm.get(task_id)["status"] == "completed":
                break
            time.sleep(0.01)
        assert tm.cancel(task_id) is False  # already completed

    def test_task_id_uniqueness(self):
        from lib.task_manager import TaskManager
        # Enough workers for every submission: submit() now REFUSES past
        # capacity instead of queueing (doc §4d), so a 4-worker pool would make
        # this a test of pool saturation rather than of id uniqueness.
        tm = TaskManager(max_workers=32)
        ids = {tm.submit("test", lambda: None) for _ in range(20)}
        assert len(ids) == 20

    # ── Per-task log file tests ──

    def test_task_log_file_created(self, tmp_path):
        """Each task gets a dedicated log file path."""
        from lib.task_manager import TaskManager
        log_dir = tmp_path / "task_logs"
        tm = TaskManager(max_workers=2, log_dir=str(log_dir))

        def noop():
            pass

        task_id = tm.submit("test", noop)
        log_path = tm.get_log_file(task_id)
        assert log_path is not None
        assert f"task-{task_id}" in log_path
        assert log_dir.as_posix() in log_path

        import time
        for _ in range(50):
            if tm.get(task_id)["status"] == "completed":
                break
            time.sleep(0.01)

    def test_task_log_file_written(self, tmp_path):
        """Docker ops commands are written to the per-task log."""
        from lib.task_manager import TaskManager
        from lib import docker_ops
        from pathlib import Path
        log_dir = tmp_path / "task_logs"
        tm = TaskManager(max_workers=2, log_dir=str(log_dir))

        def write_something():
            docker_ops._write_log("hello from task\n")

        task_id = tm.submit("test", write_something)
        import time
        for _ in range(50):
            if tm.get(task_id)["status"] == "completed":
                break
            time.sleep(0.01)

        log_path = tm.get_log_file(task_id)
        assert Path(log_path).exists()
        content = Path(log_path).read_text()
        assert "hello from task" in content

    def test_task_log_file_cleaned_on_ttl(self, tmp_path):
        """Finished tasks older than TTL have their log files deleted."""
        from lib.task_manager import TaskManager
        from lib import docker_ops
        from pathlib import Path
        import time
        log_dir = tmp_path / "task_logs"
        tm = TaskManager(max_workers=2, ttl_seconds=0.3, log_dir=str(log_dir))

        def task_that_logs():
            docker_ops._write_log("task output\n")

        task_id = tm.submit("test", task_that_logs)
        for _ in range(50):
            t = tm.get(task_id)
            if t and t["status"] == "completed":
                break
            time.sleep(0.01)

        log_path = tm.get_log_file(task_id)
        assert Path(log_path).exists(), f"Log should exist at {log_path}"

        # Wait for TTL to expire, then run the sweep.
        time.sleep(0.5)
        tm.prune()

        # T5: the tombstone is retained; its log is deleted and `log_ref` cleared.
        assert tm.get(task_id)["deleted_at"] is not None
        assert tm.get(task_id)["log_ref"] is None
        assert not Path(log_path).exists()  # log deleted
        assert all(t["task_id"] != task_id for t in tm.list_all())

    def test_task_log_file_cleaned_on_max(self, tmp_path):
        """Beyond max_tasks, the oldest FINISHED task + log are evicted.

        The sweep is triggered from `submit`, i.e. while the newest tasks are
        still live — so the eviction is expected once they have FINISHED, which
        is why it is driven explicitly here (G30: only finished records are
        eligible; `test_cap_never_evicts_a_live_task` covers the live case)."""
        from lib.task_manager import TaskManager
        from pathlib import Path
        log_dir = tmp_path / "task_logs"
        tm = TaskManager(max_workers=4, max_tasks=3, log_dir=str(log_dir))

        task_ids = []
        log_paths = []
        for _ in range(5):
            tid = tm.submit("test", lambda: None)
            task_ids.append(tid)
            log_paths.append(tm.get_log_file(tid))

        import time
        for tid in task_ids:
            for _ in range(50):
                t = tm.get(tid)
                if t and t["status"] == "completed":
                    break
                time.sleep(0.01)

        # All five are finished now ⇒ the cap applies on the next sweep.
        tm.prune()

        # Oldest 2 tasks are tombstoned (retained, log gone)
        assert tm.get(task_ids[0])["deleted_at"] is not None
        assert tm.get(task_ids[1])["deleted_at"] is not None
        assert not Path(log_paths[0]).exists()
        assert not Path(log_paths[1]).exists()

        # Newest 3 are still live
        assert tm.get(task_ids[2])["deleted_at"] is None
        assert tm.get(task_ids[3])["deleted_at"] is None
        assert tm.get(task_ids[4])["deleted_at"] is None

    def test_the_cleanup_defaults_are_the_contract(self):
        """G30: "one TTL number, one default, configured the same way".

        Pinned on this side too, against the gateway's
        (`test_unit.py::TestTheCleanupContract::test_the_ttl_and_cap_defaults_are_the_apis`).
        The env names differ because the engines do; the DEFAULTS must not, or
        the two stores age their records for different lengths of time and nobody
        sees it.
        """
        import os
        from lib.task_manager import TaskManager

        assert int(os.environ.get("TASK_TTL_SECONDS", "604800")) == 604800
        assert int(os.environ.get("TASK_MAX_COUNT", "1000")) == 1000
        # …and the singleton is built from them, not from a literal.
        tm = TaskManager()
        assert tm._ttl == 604800 and tm._max_tasks == 1000

    def test_cap_never_evicts_a_live_task(self, tmp_path):
        """G30: only FINISHED records are eligible for eviction.

        The sweep used to sort every task by age and delete the overflow
        whatever its status, so a RUNNING task could lose its registry entry —
        and its log, which the `LLM based fix` handoff needs — while its build
        carried on. The oldest task here is the live one, so eviction by age
        alone would take it."""
        import uuid
        from datetime import datetime, timedelta, timezone

        from lib import db
        from lib.task_manager import TaskManager

        log_dir = tmp_path / "task_logs"
        tm = TaskManager(max_workers=4, max_tasks=2, log_dir=str(log_dir))
        now = datetime.now(timezone.utc)

        rows = [("api-running", "running", now - timedelta(seconds=30))]
        rows += [(f"api-done{i}", "completed", now - timedelta(seconds=20 - i))
                 for i in range(3)]
        with db.connection() as conn:
            for task_id, status, created in rows:
                conn.execute(
                    "INSERT INTO registry.tasks (task_id, procedure_id, service_id, "
                    "executor, kind, operation, status, created_at, updated_at) "
                    "VALUES (%s,%s,NULL,'api','deploy','register',%s,%s,%s)",
                    (task_id, uuid.uuid4(), status, created, created),
                )

        tm.prune()

        assert tm.get("api-running")["deleted_at"] is None, "a live task keeps its record"
        assert tm.get("api-running")["status"] == "running"
        # cap 2 against three finished ⇒ exactly the oldest finished one goes
        assert tm.get("api-done0")["deleted_at"] is not None
        assert tm.get("api-done1")["deleted_at"] is None
        assert tm.get("api-done2")["deleted_at"] is None

    def test_get_log_file_nonexistent(self, tmp_path):
        """get_log_file returns None for nonexistent task."""
        from lib.task_manager import TaskManager
        log_dir = tmp_path / "task_logs"
        tm = TaskManager(max_workers=2, log_dir=str(log_dir))
        assert tm.get_log_file("nonexistent") is None

    def test_task_log_file_thread_isolation(self, tmp_path):
        """Two concurrent tasks write to separate log files."""
        from lib.task_manager import TaskManager
        from lib import docker_ops
        from pathlib import Path
        import time
        log_dir = tmp_path / "task_logs"
        tm = TaskManager(max_workers=4, log_dir=str(log_dir))

        def write_a():
            docker_ops._write_log("task-A\n")
            time.sleep(0.1)

        def write_b():
            docker_ops._write_log("task-B\n")
            time.sleep(0.1)

        tid_a = tm.submit("test", write_a)
        tid_b = tm.submit("test", write_b)

        for _ in range(50):
            a = tm.get(tid_a)
            b = tm.get(tid_b)
            if a["status"] == "completed" and b["status"] == "completed":
                break
            time.sleep(0.01)

        log_a = Path(tm.get_log_file(tid_a)).read_text()
        log_b = Path(tm.get_log_file(tid_b)).read_text()
        assert "task-A" in log_a
        assert "task-B" not in log_a  # no cross-contamination
        assert "task-B" in log_b
        assert "task-A" not in log_b  # no cross-contamination

    def test_task_dict_structure(self):
        from lib.task_manager import TaskManager
        tm = TaskManager(max_workers=2)

        def echo(x):
            return x

        tid = tm.submit("register", echo, 42)
        import time
        task = None
        for _ in range(50):
            task = tm.get(tid)
            if task["status"] in ("completed", "failed"):
                break
            time.sleep(0.01)

        assert set(task.keys()) == {
            "task_id", "procedure_id", "service_id", "executor", "kind",
            "operation", "type", "status", "phase", "progress", "error",
            "log_ref", "result", "target", "deleted_at", "created_at", "updated_at",
        }
        assert task["executor"] == "api"
        assert task["kind"] == "deploy"
        assert task["operation"] == "register"
        assert task["task_id"] == tid
        assert task["type"] == "register"
        assert task["status"] == "completed"
        assert task["result"] == 42

    def test_list_all_returns_all_tasks(self, tmp_path):
        from lib.task_manager import TaskManager
        log_dir = tmp_path / "task_logs"
        tm = TaskManager(max_workers=4, log_dir=str(log_dir))
        import time

        ids = [tm.submit("test", lambda x: x, i) for i in range(3)]
        for _ in range(50):
            tasks = tm.list_all()
            if all(t["status"] in ("completed", "failed") for t in tasks):
                break
            time.sleep(0.01)

        assert len(tasks) == 3
        # Newest first
        assert tasks[0]["task_id"] == ids[2]
        assert tasks[2]["task_id"] == ids[0]

    def test_list_all_empty(self, tmp_path):
        from lib.task_manager import TaskManager
        log_dir = tmp_path / "task_logs"
        tm = TaskManager(log_dir=str(log_dir))
        tasks = tm.list_all()
        assert tasks == []

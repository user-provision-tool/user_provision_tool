"""Unit/integration tests for the bounded durable queue (lib/task_manager.py).

The queue replaced the per-process 4-slot pool (GRILLING G92): admission is a
20-queued cap plus a planner heartbeat; the planner writes a 4-deep `planned`
buffer and the workers claim it. These tests drive the real PostgreSQL store.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def wait_state(tm, task_id, states=("completed", "failed", "cancelled"), timeout=8.0):
    deadline = time.monotonic() + timeout
    task = None
    while time.monotonic() < deadline:
        task = tm.get(task_id)
        if task and task["status"] in states:
            return task
        time.sleep(0.02)
    return task


def set_config(**values):
    """Change the singleton queue config for one test (restored by truncation)."""
    from lib import db
    sets = ", ".join(f"{k}=%s" for k in values)
    db.execute(f"UPDATE registry.queue_config SET {sets} WHERE id=1",
               tuple(values.values()))


def touch_heartbeat(seconds_ago: float = 0.0):
    from lib import db
    db.execute(
        "UPDATE registry.queue_state SET last_dispatch_at = now() - make_interval(secs => %s) "
        "WHERE id=1", (seconds_ago,))


class TestTaskManager:
    def test_submit_and_complete(self):
        from lib.task_manager import TaskManager
        tm = TaskManager(max_workers=2, planner_interval_s=0.05, worker_poll_s=0.01)
        tm.start()
        try:
            def slow_add(a, b):
                time.sleep(0.05)
                return a + b

            task_id = tm.submit("test", slow_add, 1, 2)
            assert task_id.startswith("api-") and len(task_id) == 16
            task = wait_state(tm, task_id)
            assert task["status"] == "completed"
            assert task["result"] == 3
            assert task["error"] is None
            assert task["type"] == "test"
            assert task["started_at"] and task["finished_at"]
        finally:
            tm.stop()

    def test_submit_and_fail(self):
        from lib.task_manager import TaskManager
        tm = TaskManager(max_workers=2, planner_interval_s=0.05, worker_poll_s=0.01)
        tm.start()
        try:
            def raiser():
                raise ValueError("boom")

            task_id = tm.submit("test", raiser)
            task = wait_state(tm, task_id)
            assert task["status"] == "failed"
            assert "boom" in (task["error"] or "")
            assert task["result"] is None
            assert task["finished_at"]
        finally:
            tm.stop()

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
        assert tm.get(task_id)["finished_at"] is not None

    def test_queue_full_at_twenty(self):
        """20 queued admitted; the 21st refused `queue_full`, no row written."""
        from lib.task_manager import QueueFullError, TaskManager
        tm = TaskManager(max_workers=4)  # NOT started: everything stays queued
        ids = []
        for i in range(20):
            ids.append(tm.submit("test", lambda i=i: i))
        assert len(tm.list_all()) == 20
        try:
            tm.submit("test", lambda: 21)
            assert False, "expected QueueFullError"
        except QueueFullError as exc:
            assert exc.queued == 20 and exc.capacity == 20
            assert "queue full" in str(exc)
        assert len(tm.list_all()) == 20, "a refused submit leaves no row"

    def test_queue_cap_is_configurable(self):
        from lib.task_manager import QueueFullError, TaskManager
        set_config(queue_cap=2)
        tm = TaskManager(max_workers=4)
        tm.submit("test", lambda: 1)
        tm.submit("test", lambda: 2)
        try:
            tm.submit("test", lambda: 3)
            assert False, "expected QueueFullError"
        except QueueFullError as exc:
            assert exc.capacity == 2

    def test_stale_heartbeat_refuses_queue_unavailable(self):
        from lib.task_manager import QueueUnavailableError, TaskManager
        touch_heartbeat(3600)
        tm = TaskManager(max_workers=4)
        before = len(tm.list_all())
        try:
            tm.submit("test", lambda: 1)
            assert False, "expected QueueUnavailableError"
        except QueueUnavailableError as exc:
            assert exc.stale_after_s == 30
        assert len(tm.list_all()) == before

    def test_a_fresh_heartbeat_admits(self):
        from lib.task_manager import TaskManager
        touch_heartbeat(1)
        tm = TaskManager(max_workers=4)
        tid = tm.submit("test", lambda: 1)
        assert tm.get(tid)["status"] == "queued"

    def test_workers_never_exceed_the_configured_count(self):
        from lib import db
        from lib.task_manager import TaskManager
        set_config(workers=1, plan_size=1)
        tm = TaskManager(max_workers=1, planner_interval_s=0.05, worker_poll_s=0.01)
        tm.start()
        try:
            gate = __import__("threading").Event()

            def blocker():
                gate.wait(5)

            ids = [tm.submit("test", blocker) for _ in range(3)]
            time.sleep(0.4)
            running = db.fetch_one(
                "SELECT count(*) AS n FROM registry.tasks WHERE status='running'")["n"]
            assert running <= 1
            gate.set()
            for tid in ids:
                wait_state(tm, tid)
        finally:
            gate.set()
            tm.stop()

    def test_plan_size_is_the_configured_depth(self):
        """The planner writes up to `plan_size` rows as `planned` (R3.2)."""
        from lib import db
        from lib.task_manager import TaskManager
        tm = TaskManager(max_workers=4)  # no workers: the plan stays planned
        for _ in range(6):
            tm.submit("test", lambda: None)
        tm.planner_tick()
        rows = db.fetch_all(
            "SELECT task_id, plan_seq, status FROM registry.tasks "
            "WHERE status='planned' ORDER BY plan_seq")
        assert len(rows) == 4
        assert [r["plan_seq"] for r in rows] == [1, 2, 3, 4]
        # A formed plan is not refilled.
        tm.planner_tick()
        assert db.fetch_one(
            "SELECT count(*) AS n FROM registry.tasks WHERE status='planned'")["n"] == 4

    def test_claim_moves_planned_to_running_with_started_at(self):
        from lib import db
        from lib.task_manager import TaskManager
        tm = TaskManager(max_workers=1)
        tid = tm.submit("test", lambda: None)
        tm.planner_tick()
        row = tm._claim_next()
        assert row is not None and row["task_id"] == tid
        stored = db.fetch_one(
            "SELECT status, started_at FROM registry.tasks WHERE task_id=%s", (tid,))
        assert stored["status"] == "running" and stored["started_at"] is not None
        # At the worker bound, no further claim is possible.
        tm.submit("test", lambda: None)
        tm.planner_tick()
        assert tm._claim_next() is None

    def test_run_timeout_fails_and_runs_runtime_cleanup(self, tmp_path, monkeypatch):
        """R5.1/R6.2: a run timeout aborts, fails the row and cleans runtime
        resources; a generated file is never deleted."""
        from lib import docker_ops, provisioner
        from lib.task_manager import TaskManager
        from registry_support import add_user

        set_config(run_timeout_deploy=1, cancel_timeout_deploy=10)
        calls = []
        monkeypatch.setattr(provisioner, "compensate", lambda **kw: calls.append(kw))
        service_id, _proc = add_user(
            {"user_name": "alice", "service_name": "myapp", "label": "0"})
        generated = tmp_path / "nginx.user-alice.0.conf"
        generated.write_text("rendered\n")

        tm = TaskManager(max_workers=1, planner_interval_s=0.05, worker_poll_s=0.01)
        tm.start()
        try:
            def blocker():
                ev = getattr(docker_ops._task_log, "cancel", None)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if ev is not None and ev.wait(0.05):
                        raise docker_ops.CommandCancelled("terminated")
                    time.sleep(0.05)

            tid = tm.submit("register", blocker, service_id=service_id,
                            previous_status="running")
            task = wait_state(tm, tid, states=("failed", "cancelled"), timeout=8)
            assert task["status"] == "failed", task
            assert "run timeout" in (task["error"] or "")
            assert calls and calls[0]["service_id"] == str(service_id)
            assert generated.exists(), "a generated file is never auto-deleted"
        finally:
            tm.stop()

    def test_restart_fails_only_running_and_keeps_the_queue(self):
        """R2.2/R10.1: running -> failed; queued/planned survive."""
        import uuid
        from lib import db
        from lib.task_manager import TaskManager
        tm = TaskManager(max_workers=2)
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        rows = [
            ("api-restart-run", "running"),
            ("api-restart-queue", "queued"),
            ("api-restart-plan", "planned"),
        ]
        with db.connection() as conn:
            for tid, status in rows:
                conn.execute(
                    "INSERT INTO registry.tasks (task_id, procedure_id, service_id, "
                    "executor, kind, operation, status, created_at, updated_at, started_at) "
                    "VALUES (%s,%s,NULL,'api','deploy','register',%s,%s,%s,%s)",
                    (tid, uuid.uuid4(), status, now, now,
                     now if status == "running" else None),
                )
        assert tm.recover_on_startup() == 1
        assert tm.get("api-restart-run")["status"] == "failed"
        assert "interrupted by restart" in tm.get("api-restart-run")["error"]
        assert tm.get("api-restart-queue")["status"] == "queued"
        assert tm.get("api-restart-plan")["status"] == "planned"

    def test_cancel_queued_is_immediate_and_running_is_requested(self):
        from lib import db
        from lib.task_manager import TaskManager
        tm = TaskManager(max_workers=1)
        queued = tm.submit("test", lambda: None)
        assert tm.cancel(queued) is True
        assert tm.get(queued)["status"] == "cancelled"

        running = tm.submit("test", lambda: None)
        tm.planner_tick()
        tm._claim_next()
        assert tm.cancel(running) is True
        stored = db.fetch_one(
            "SELECT status, cancel_requested_at FROM registry.tasks WHERE task_id=%s",
            (running,))
        assert stored["status"] == "running"
        assert stored["cancel_requested_at"] is not None

    def test_cancel_planned_is_immediate(self):
        from lib.task_manager import TaskManager
        tm = TaskManager(max_workers=4)
        tid = tm.submit("test", lambda: None)
        tm.planner_tick()
        assert tm.get(tid)["status"] == "planned"
        assert tm.cancel(tid) is True
        assert tm.get(tid)["status"] == "cancelled"
        assert tm.get(tid)["finished_at"] is not None

    def test_unsettled_cancel_reports_unconfirmed(self):
        """R6.3: the caller's settle bound reports `cancellation_unconfirmed`."""
        from lib import db
        from lib.task_manager import TaskManager
        set_config(cancel_timeout_deploy=1)
        tm = TaskManager(max_workers=1)
        tid = tm.submit("register", lambda: None, previous_status="running")
        tm.planner_tick()
        tm._claim_next()  # running, but nothing will settle it
        result = tm.cancel_and_wait(tid)
        assert result["confirmed"] is False
        assert result["error"] == "cancellation_unconfirmed"
        row = db.fetch_one(
            "SELECT status, cancel_requested_at FROM registry.tasks WHERE task_id=%s",
            (tid,))
        assert row["status"] == "running"  # visible to the reconciler/watchdog
        assert row["cancel_requested_at"] is not None

    def test_positions_and_eta(self):
        """Position = queued count before the task; ETA = pos/workers x median."""
        from lib.task_manager import TaskManager
        set_config(workers=4, plan_size=1)
        tm = TaskManager(max_workers=4)
        ids = [tm.submit("test", lambda: None) for _ in range(3)]
        stats = tm.stats()
        assert stats["tasks"][ids[0]]["position"] == 0
        assert stats["tasks"][ids[1]]["position"] == 1
        assert stats["tasks"][ids[2]]["position"] == 2
        for tid in ids:
            task = stats["tasks"][tid]
            # No finished history: the median (and therefore the ETA) is 0.
            assert task["eta_start_s"] == 0.0
            assert task["eta_finish_s"] == 0.0

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
        tm = TaskManager(max_workers=2, planner_interval_s=0.05, worker_poll_s=0.01)
        tm.start()
        try:
            task_id = tm.submit("test", lambda: None)
            wait_state(tm, task_id)
            assert tm.cancel(task_id) is False  # already completed
        finally:
            tm.stop()

    def test_task_id_uniqueness(self):
        from lib.task_manager import TaskManager
        tm = TaskManager(max_workers=4)
        ids = {tm.submit("test", lambda: None) for _ in range(20)}
        assert len(ids) == 20

    # ── Per-task log file tests ──

    def test_task_log_file_created(self, tmp_path):
        from lib.task_manager import TaskManager
        log_dir = tmp_path / "task_logs"
        tm = TaskManager(max_workers=2, log_dir=str(log_dir),
                         planner_interval_s=0.05, worker_poll_s=0.01)
        tm.start()
        try:
            task_id = tm.submit("test", lambda: None)
            log_path = tm.get_log_file(task_id)
            assert log_path is not None
            assert f"task-{task_id}" in log_path
            assert log_dir.as_posix() in log_path
            wait_state(tm, task_id)
        finally:
            tm.stop()

    def test_task_log_file_written(self, tmp_path):
        from lib.task_manager import TaskManager
        from lib import docker_ops
        log_dir = tmp_path / "task_logs"
        tm = TaskManager(max_workers=2, log_dir=str(log_dir),
                         planner_interval_s=0.05, worker_poll_s=0.01)
        tm.start()
        try:
            def write_something():
                docker_ops._write_log("hello from task\n")

            task_id = tm.submit("test", write_something)
            wait_state(tm, task_id)
            log_path = tm.get_log_file(task_id)
            assert Path(log_path).exists()
            assert "hello from task" in Path(log_path).read_text()
        finally:
            tm.stop()

    def test_task_log_file_cleaned_on_ttl(self, tmp_path):
        from lib.task_manager import TaskManager
        from lib import docker_ops
        log_dir = tmp_path / "task_logs"
        tm = TaskManager(max_workers=2, ttl_seconds=0.3, log_dir=str(log_dir),
                         planner_interval_s=0.05, worker_poll_s=0.01)
        tm.start()
        try:
            def task_that_logs():
                docker_ops._write_log("task output\n")

            task_id = tm.submit("test", task_that_logs)
            wait_state(tm, task_id)
            log_path = tm.get_log_file(task_id)
            assert Path(log_path).exists()
            time.sleep(0.5)
            tm.prune()
            assert tm.get(task_id)["deleted_at"] is not None
            assert tm.get(task_id)["log_ref"] is None
            assert not Path(log_path).exists()
            assert all(t["task_id"] != task_id for t in tm.list_all())
        finally:
            tm.stop()

    def test_task_log_file_cleaned_on_max(self, tmp_path):
        from lib.task_manager import TaskManager
        from pathlib import Path as _Path
        log_dir = tmp_path / "task_logs"
        tm = TaskManager(max_workers=4, max_tasks=3, log_dir=str(log_dir),
                         planner_interval_s=0.05, worker_poll_s=0.01)
        tm.start()
        try:
            task_ids, log_paths = [], []
            for _ in range(5):
                tid = tm.submit("test", lambda: None)
                task_ids.append(tid)
                log_paths.append(tm.get_log_file(tid))
            for tid in task_ids:
                wait_state(tm, tid)
            tm.prune()
            assert tm.get(task_ids[0])["deleted_at"] is not None
            assert tm.get(task_ids[1])["deleted_at"] is not None
            assert not _Path(log_paths[0]).exists()
            assert not _Path(log_paths[1]).exists()
            assert tm.get(task_ids[2])["deleted_at"] is None
            assert tm.get(task_ids[3])["deleted_at"] is None
            assert tm.get(task_ids[4])["deleted_at"] is None
        finally:
            tm.stop()

    def test_the_cleanup_defaults_are_the_contract(self):
        import os
        from lib.task_manager import TaskManager

        assert int(os.environ.get("TASK_TTL_SECONDS", "604800")) == 604800
        assert int(os.environ.get("TASK_MAX_COUNT", "1000")) == 1000
        tm = TaskManager()
        assert tm._ttl == 604800 and tm._max_tasks == 1000

    def test_cap_never_evicts_a_live_task(self, tmp_path):
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
        assert tm.get("api-done0")["deleted_at"] is not None
        assert tm.get("api-done1")["deleted_at"] is None
        assert tm.get("api-done2")["deleted_at"] is None

    def test_get_log_file_nonexistent(self, tmp_path):
        from lib.task_manager import TaskManager
        log_dir = tmp_path / "task_logs"
        tm = TaskManager(max_workers=2, log_dir=str(log_dir))
        assert tm.get_log_file("nonexistent") is None

    def test_task_log_file_thread_isolation(self, tmp_path):
        from lib.task_manager import TaskManager
        from lib import docker_ops
        from pathlib import Path as _Path
        log_dir = tmp_path / "task_logs"
        tm = TaskManager(max_workers=4, log_dir=str(log_dir),
                         planner_interval_s=0.05, worker_poll_s=0.01)
        tm.start()
        try:
            def write_a():
                docker_ops._write_log("task-A\n")
                time.sleep(0.1)

            def write_b():
                docker_ops._write_log("task-B\n")
                time.sleep(0.1)

            tid_a = tm.submit("test", write_a)
            tid_b = tm.submit("test", write_b)
            wait_state(tm, tid_a)
            wait_state(tm, tid_b)
            log_a = _Path(tm.get_log_file(tid_a)).read_text()
            log_b = _Path(tm.get_log_file(tid_b)).read_text()
            assert "task-A" in log_a
            assert "task-B" not in log_a
            assert "task-B" in log_b
            assert "task-A" not in log_b
        finally:
            tm.stop()

    def test_task_dict_structure(self):
        from lib.task_manager import TaskManager
        tm = TaskManager(max_workers=2, planner_interval_s=0.05, worker_poll_s=0.01)
        tm.start()
        try:
            def echo(x):
                return x

            tid = tm.submit("register", echo, 42)
            task = wait_state(tm, tid)
            assert set(task.keys()) == {
                "task_id", "procedure_id", "service_id", "executor", "kind",
                "operation", "type", "status", "phase", "progress", "error",
                "log_ref", "result", "plan_seq", "started_at", "finished_at",
                "cancel_requested_at", "target", "deleted_at", "created_at",
                "updated_at",
            }
            assert task["executor"] == "api"
            assert task["kind"] == "deploy"
            assert task["operation"] == "register"
            assert task["task_id"] == tid
            assert task["type"] == "register"
            assert task["status"] == "completed"
            assert task["result"] == 42
        finally:
            tm.stop()

    def test_list_all_returns_all_tasks(self, tmp_path):
        from lib.task_manager import TaskManager
        log_dir = tmp_path / "task_logs"
        tm = TaskManager(max_workers=4, log_dir=str(log_dir),
                         planner_interval_s=0.05, worker_poll_s=0.01)
        tm.start()
        try:
            ids = [tm.submit("test", lambda x: x, i) for i in range(3)]
            for tid in ids:
                wait_state(tm, tid)
            tasks = tm.list_all()
            assert len(tasks) == 3
            assert tasks[0]["task_id"] == ids[2]
            assert tasks[2]["task_id"] == ids[0]
        finally:
            tm.stop()

    def test_list_all_empty(self, tmp_path):
        from lib.task_manager import TaskManager
        log_dir = tmp_path / "task_logs"
        tm = TaskManager(log_dir=str(log_dir))
        assert tm.list_all() == []

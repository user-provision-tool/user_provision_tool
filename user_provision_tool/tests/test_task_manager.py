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
        assert len(task_id) == 12  # short UUID

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
        from lib.task_manager import TaskManager
        tm = TaskManager(max_workers=1)

        def blocker():
            import time
            time.sleep(10)

        # Use the only worker so the second task stays pending
        tm.submit("block", blocker)
        task_id = tm.submit("test", lambda: 42)

        assert tm.cancel(task_id) is True
        task = tm.get(task_id)
        assert task["status"] == "cancelled"

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
        tm = TaskManager(max_workers=4)
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

        # Wait for TTL to expire
        time.sleep(0.5)

        # Trigger cleanup by polling
        tm.get(task_id)
        assert tm.get(task_id) is None  # task cleaned up
        assert not Path(log_path).exists()  # log deleted

    def test_task_log_file_cleaned_on_max(self, tmp_path):
        """When max_tasks is exceeded, oldest task + log are evicted."""
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

        # Oldest 2 tasks should be evicted
        assert tm.get(task_ids[0]) is None
        assert tm.get(task_ids[1]) is None
        assert not Path(log_paths[0]).exists()
        assert not Path(log_paths[1]).exists()

        # Newest 3 should still exist
        assert tm.get(task_ids[2]) is not None
        assert tm.get(task_ids[3]) is not None
        assert tm.get(task_ids[4]) is not None

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
            "task_id", "type", "status", "created_at", "updated_at", "result", "error"
        }
        assert task["task_id"] == tid
        assert task["type"] == "register"
        assert task["status"] == "completed"
        assert task["result"] == 42

    def test_list_all_returns_all_tasks(self):
        from lib.task_manager import TaskManager
        tm = TaskManager(max_workers=4)
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

    def test_list_all_empty(self):
        from lib.task_manager import TaskManager
        tm = TaskManager()
        tasks = tm.list_all()
        assert tasks == []

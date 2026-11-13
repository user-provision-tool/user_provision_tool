"""The bounded task-queue contract (T0, GRILLING G92).

One home for the names both services must agree on: the task states, the
admission/control error codes, the per-kind timeouts and the stats payload
keys. The gateway has a mirror module with the same names — the two serializers
are tested against this contract, so a drift is a test failure rather than a
silent UI difference.

This module holds no policy: every value here is a DEFAULT that the per-system
``queue_config`` row can override (the row is the configuration; these are what
a fresh install starts with).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# States (R2.1). The task table is the queue.
# ---------------------------------------------------------------------------
QUEUED = "queued"
PLANNED = "planned"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
UNKNOWN = "unknown"  # legacy rows imported from the pre-queue store

STATES = (QUEUED, PLANNED, RUNNING, COMPLETED, FAILED, CANCELLED, UNKNOWN)
ACTIVE_STATES = (QUEUED, PLANNED, RUNNING)
TERMINAL_STATES = (COMPLETED, FAILED, CANCELLED)

# ---------------------------------------------------------------------------
# Refusal / control error codes (R1.1, R1.3, R6.3)
# ---------------------------------------------------------------------------
ERR_QUEUE_FULL = "queue_full"
ERR_QUEUE_UNAVAILABLE = "queue_unavailable"
ERR_CANCELLATION_UNCONFIRMED = "cancellation_unconfirmed"

ERROR_CODES = (ERR_QUEUE_FULL, ERR_QUEUE_UNAVAILABLE, ERR_CANCELLATION_UNCONFIRMED)

# ---------------------------------------------------------------------------
# Task kinds and their per-kind bounds (R5.1/R5.2, G92 defaults)
# ---------------------------------------------------------------------------
KINDS = ("generate", "fix_analysis", "deploy", "rebuild", "remove", "update")

RUN_TIMEOUTS = {
    "generate": 1200,
    "fix_analysis": 600,
    "deploy": 1800,
    "rebuild": 1800,
    "remove": 300,
    "update": 120,
}

CANCEL_TIMEOUTS = {
    "generate": 120,
    "fix_analysis": 120,
    "deploy": 300,
    "rebuild": 300,
    "remove": 120,
    "update": 60,
}

# ---------------------------------------------------------------------------
# Queue parameters (R1, R3)
# ---------------------------------------------------------------------------
DEFAULT_QUEUE_CAP = 20
DEFAULT_WORKERS = 4
DEFAULT_PLAN_SIZE = 4
DEFAULT_SPAN_S = 300
DEFAULT_HEARTBEAT_INTERVAL_S = 5
DEFAULT_HEARTBEAT_STALE_S = 30
DEFAULT_PLANNER_INTERVAL_S = 2

CONFIG_KEYS = (
    "queue_cap", "workers", "plan_size", "span_s",
    "heartbeat_interval_s", "heartbeat_stale_s", "planner_interval_s",
    "run_timeout_generate", "run_timeout_fix_analysis", "run_timeout_deploy",
    "run_timeout_rebuild", "run_timeout_remove", "run_timeout_update",
    "cancel_timeout_generate", "cancel_timeout_fix_analysis", "cancel_timeout_deploy",
    "cancel_timeout_rebuild", "cancel_timeout_remove", "cancel_timeout_update",
)

# ---------------------------------------------------------------------------
# Stats payload (R7) — the keys every stats surface may rely on.
# ---------------------------------------------------------------------------
STATS_TOP_KEYS = (
    "queue", "counts", "workers", "pinned_workers", "cap", "span_s",
    "plan_size", "heartbeat", "planner_interval_s", "tasks", "kinds", "config",
)

STATS_HEARTBEAT_KEYS = ("last_dispatch_at", "age_s", "alive", "stale_after_s")

STATS_TASK_KEYS = (
    "status", "kind", "plan_seq", "position",
    "eta_start_s", "eta_finish_s", "started_at", "finished_at",
)

STATS_KIND_KEYS = ("median_s", "samples")


def config_from_row(row: dict | None) -> dict:
    """The config dict with defaults applied (one shape for every reader)."""
    row = row or {}
    cfg = {
        "queue_cap": DEFAULT_QUEUE_CAP,
        "workers": DEFAULT_WORKERS,
        "plan_size": DEFAULT_PLAN_SIZE,
        "span_s": DEFAULT_SPAN_S,
        "heartbeat_interval_s": DEFAULT_HEARTBEAT_INTERVAL_S,
        "heartbeat_stale_s": DEFAULT_HEARTBEAT_STALE_S,
        "planner_interval_s": DEFAULT_PLANNER_INTERVAL_S,
    }
    for kind in KINDS:
        cfg[f"run_timeout_{kind}"] = RUN_TIMEOUTS[kind]
        cfg[f"cancel_timeout_{kind}"] = CANCEL_TIMEOUTS[kind]
    for key in CONFIG_KEYS:
        value = row.get(key)
        if value is not None:
            try:
                cfg[key] = int(value)
            except (TypeError, ValueError):
                pass
    return cfg

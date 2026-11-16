"""The bounded task-queue contract (T0, GRILLING G92/G94).

One home for the names both services must agree on: the task states, the two
**lanes**, the admission/control error codes, the per-kind timeouts, the
per-lane configuration keys and the stats payload keys. The gateway has a
mirror module with the same names — the two serializers are tested against this
contract, so a drift is a test failure rather than a silent UI difference.

This module holds no policy: every value here is a DEFAULT that the per-system
``queue_config`` row can override (the row is the configuration; these are what
a fresh install starts with).

The lane is a scheduling class over ONE durable table (G94): ``operator`` runs
deploy/rebuild/remove (and the gateway's generate/analyze), ``maintenance`` runs
the small, convergent ``update`` tasks (the resolv.conf set/clear). The legacy
``queue_cap``/``workers``/``plan_size`` fields ARE the operator lane (R8); the
``*_maintenance`` fields name the maintenance lane.
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
# Lanes (R1, G94). One table, two scheduling classes.
# ---------------------------------------------------------------------------
OPERATOR = "operator"
MAINTENANCE = "maintenance"
LANES = (OPERATOR, MAINTENANCE)

# `update` is the only maintenance kind today (the resolv.conf set/clear).
MAINTENANCE_KINDS = ("update",)


def lane_for_kind(kind: str | None) -> str:
    """The lane derived once at insert; it never changes afterwards (R1.1)."""
    return MAINTENANCE if kind in MAINTENANCE_KINDS else OPERATOR


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
# Queue parameters (R1, R3). Operator fields keep their G92 names; the
# maintenance lane has its own cap/workers/plan depth (R4, G94 D2/D3).
# ---------------------------------------------------------------------------
DEFAULT_QUEUE_CAP = 20
DEFAULT_WORKERS = 4
DEFAULT_PLAN_SIZE = 4
DEFAULT_QUEUE_CAP_MAINTENANCE = 20
DEFAULT_WORKERS_MAINTENANCE = 4
DEFAULT_PLAN_SIZE_MAINTENANCE = 4
DEFAULT_SPAN_S = 300
DEFAULT_HEARTBEAT_INTERVAL_S = 5
DEFAULT_HEARTBEAT_STALE_S = 30
DEFAULT_PLANNER_INTERVAL_S = 2

CONFIG_KEYS = (
    "queue_cap", "workers", "plan_size", "span_s",
    "queue_cap_maintenance", "workers_maintenance", "plan_size_maintenance",
    "heartbeat_interval_s", "heartbeat_stale_s", "planner_interval_s",
    "run_timeout_generate", "run_timeout_fix_analysis", "run_timeout_deploy",
    "run_timeout_rebuild", "run_timeout_remove", "run_timeout_update",
    "cancel_timeout_generate", "cancel_timeout_fix_analysis", "cancel_timeout_deploy",
    "cancel_timeout_rebuild", "cancel_timeout_remove", "cancel_timeout_update",
)

# ---------------------------------------------------------------------------
# Stats payload (R7) — the keys every stats surface may rely on.
#
# Top-level counts/workers/cap/plan_size/tasks are the OPERATOR lane (R8: the
# existing fields keep their meaning, and the per-card position/ETA stays
# operator-only, R5.3). `lanes` carries both lanes' numbers.
# ---------------------------------------------------------------------------
STATS_TOP_KEYS = (
    "queue", "counts", "workers", "pinned_workers", "cap", "span_s",
    "plan_size", "heartbeat", "planner_interval_s", "tasks", "kinds", "config",
    "lanes",
)

STATS_HEARTBEAT_KEYS = ("last_dispatch_at", "age_s", "alive", "stale_after_s")

STATS_TASK_KEYS = (
    "status", "kind", "lane", "plan_seq", "position",
    "eta_start_s", "eta_finish_s", "started_at", "finished_at",
)

STATS_KIND_KEYS = ("median_s", "samples")

STATS_LANE_KEYS = (
    "counts", "workers", "cap", "plan_size", "span_s", "pinned_workers",
    "last_plan_at", "tasks",
)


def lane_config(cfg: dict, lane: str) -> dict:
    """The per-lane admission/planning/telemetry parameters."""
    if lane == MAINTENANCE:
        return {
            "cap": int(cfg["queue_cap_maintenance"]),
            "workers": int(cfg["workers_maintenance"]),
            "plan_size": int(cfg["plan_size_maintenance"]),
            "span_s": None,  # FIFO: the maintenance plan has no dispatch window
        }
    return {
        "cap": int(cfg["queue_cap"]),
        "workers": int(cfg["workers"]),
        "plan_size": int(cfg["plan_size"]),
        "span_s": int(cfg["span_s"]),
    }


def config_from_row(row: dict | None) -> dict:
    """The config dict with defaults applied (one shape for every reader)."""
    row = row or {}
    cfg = {
        "queue_cap": DEFAULT_QUEUE_CAP,
        "workers": DEFAULT_WORKERS,
        "plan_size": DEFAULT_PLAN_SIZE,
        "span_s": DEFAULT_SPAN_S,
        "queue_cap_maintenance": DEFAULT_QUEUE_CAP_MAINTENANCE,
        "workers_maintenance": DEFAULT_WORKERS_MAINTENANCE,
        "plan_size_maintenance": DEFAULT_PLAN_SIZE_MAINTENANCE,
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
    # R8 — the legacy fields are the operator lane's numbers.
    cfg["queue_cap_operator"] = cfg["queue_cap"]
    cfg["workers_operator"] = cfg["workers"]
    cfg["plan_size_operator"] = cfg["plan_size"]
    return cfg

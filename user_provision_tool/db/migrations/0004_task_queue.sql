-- T1 — the bounded durable task queue (GRILLING G92; task doc §6.1–§6.7).
--
-- The task table IS the queue: the planner writes `planned` + `plan_seq`, the
-- workers claim `planned -> running`, and `started_at`/`finished_at` are the
-- telemetry. `queue_config` is the per-system configuration row; `queue_state`
-- carries the planner heartbeat that admission and the dashboard read.

ALTER TABLE registry.tasks DROP CONSTRAINT IF EXISTS tasks_status_check;
ALTER TABLE registry.tasks ADD CONSTRAINT tasks_status_check CHECK (
    status IN ('queued', 'planned', 'running', 'completed', 'failed',
               'cancelled', 'unknown')
);

ALTER TABLE registry.tasks ADD COLUMN IF NOT EXISTS plan_seq integer;
ALTER TABLE registry.tasks ADD COLUMN IF NOT EXISTS started_at timestamptz;
ALTER TABLE registry.tasks ADD COLUMN IF NOT EXISTS finished_at timestamptz;
-- The cancellation clock starts here, not at the terminal write (R5.2/R6.3):
-- the caller's settle bound and the pinned-worker count are measured from it.
ALTER TABLE registry.tasks ADD COLUMN IF NOT EXISTS cancel_requested_at timestamptz;

CREATE INDEX IF NOT EXISTS tasks_queue_order
    ON registry.tasks (status, plan_seq, created_at)
    WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS registry.queue_config (
    id                      integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    queue_cap               integer NOT NULL DEFAULT 20,
    workers                 integer NOT NULL DEFAULT 4,
    plan_size               integer NOT NULL DEFAULT 4,
    span_s                  integer NOT NULL DEFAULT 300,
    heartbeat_interval_s    integer NOT NULL DEFAULT 5,
    heartbeat_stale_s       integer NOT NULL DEFAULT 30,
    planner_interval_s      integer NOT NULL DEFAULT 2,
    run_timeout_generate    integer NOT NULL DEFAULT 1200,
    run_timeout_fix_analysis integer NOT NULL DEFAULT 600,
    run_timeout_deploy      integer NOT NULL DEFAULT 1800,
    run_timeout_rebuild     integer NOT NULL DEFAULT 1800,
    run_timeout_remove      integer NOT NULL DEFAULT 300,
    run_timeout_update      integer NOT NULL DEFAULT 120,
    cancel_timeout_generate integer NOT NULL DEFAULT 120,
    cancel_timeout_fix_analysis integer NOT NULL DEFAULT 120,
    cancel_timeout_deploy   integer NOT NULL DEFAULT 300,
    cancel_timeout_rebuild  integer NOT NULL DEFAULT 300,
    cancel_timeout_remove   integer NOT NULL DEFAULT 120,
    cancel_timeout_update   integer NOT NULL DEFAULT 60,
    updated_at              timestamptz NOT NULL DEFAULT now()
);

INSERT INTO registry.queue_config (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS registry.queue_state (
    id               integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    last_dispatch_at timestamptz,
    planner_id       text,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- Seeded with "now": a fresh install admits work immediately, and the planner
-- heartbeat is refreshed on every tick, so a stale one means a dead planner.
INSERT INTO registry.queue_state (id, last_dispatch_at)
    VALUES (1, now()) ON CONFLICT (id) DO NOTHING;

-- The gateway may read the queue state for its stats/health surfaces (R9.2);
-- it never writes these tables.
GRANT SELECT ON registry.queue_config, registry.queue_state TO gateway_app, gateway_reader;

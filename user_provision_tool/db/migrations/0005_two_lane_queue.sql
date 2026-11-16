-- T1 — two-lane task scheduling (GRILLING G94; task doc §6.1–§6.6).
--
-- The task table stays the single durable queue. `lane` is the scheduling
-- class, derived once at insert: `operator` (deploy/rebuild/remove) or
-- `maintenance` (the resolv.conf set/clear `update` rows). Each lane has its
-- own queued cap, worker count, plan depth and last-plan timestamp; the
-- operator lane keeps the existing `queue_cap`/`workers`/`plan_size` fields
-- (R8), so those columns are not renamed.
--
-- `restart_count` bounds the maintenance restart requeue (task doc §10): a row
-- that repeatedly crashes the process is failed after 5 requeues instead of
-- being recovered forever.

ALTER TABLE registry.tasks ADD COLUMN IF NOT EXISTS lane text NOT NULL
    DEFAULT 'operator';
ALTER TABLE registry.tasks DROP CONSTRAINT IF EXISTS tasks_lane_check;
ALTER TABLE registry.tasks ADD CONSTRAINT tasks_lane_check
    CHECK (lane IN ('operator', 'maintenance'));

-- Backfill existing rows from their kind (R9): every `update` is maintenance,
-- everything else operator. Idempotent and it never touches a terminal state.
UPDATE registry.tasks SET lane = 'maintenance'
    WHERE kind = 'update' AND lane <> 'maintenance';
UPDATE registry.tasks SET lane = 'operator'
    WHERE kind <> 'update' AND lane <> 'operator';

-- The maintenance restart requeue bound (task doc §10, recommended 5).
ALTER TABLE registry.tasks ADD COLUMN IF NOT EXISTS restart_count integer
    NOT NULL DEFAULT 0;

-- Lane-scoped plan/claim/position read path.
CREATE INDEX IF NOT EXISTS tasks_lane_queue_order
    ON registry.tasks (lane, status, plan_seq, created_at)
    WHERE deleted_at IS NULL;

-- Per-lane parameters. The existing fields are the operator lane (R8).
ALTER TABLE registry.queue_config ADD COLUMN IF NOT EXISTS
    queue_cap_maintenance integer NOT NULL DEFAULT 20;
ALTER TABLE registry.queue_config ADD COLUMN IF NOT EXISTS
    workers_maintenance integer NOT NULL DEFAULT 4;
ALTER TABLE registry.queue_config ADD COLUMN IF NOT EXISTS
    plan_size_maintenance integer NOT NULL DEFAULT 4;

-- Per-lane last-plan timestamps (R4.3): one heartbeat stays the liveness
-- signal; these distinguish a stuck lane from a dead planner.
ALTER TABLE registry.queue_state ADD COLUMN IF NOT EXISTS
    last_plan_operator_at timestamptz;
ALTER TABLE registry.queue_state ADD COLUMN IF NOT EXISTS
    last_plan_maintenance_at timestamptz;

UPDATE registry.queue_state
    SET last_plan_operator_at = COALESCE(last_plan_operator_at, last_dispatch_at, now()),
        last_plan_maintenance_at = COALESCE(last_plan_maintenance_at, last_dispatch_at, now())
    WHERE id = 1;

-- T2 — cutover stage marker. Each completed import step is recorded so a
-- retried cutover resumes instead of double-inserting; the row inserts stay
-- idempotent independently of the marker.
CREATE TABLE registry.import_state (
    stage        text PRIMARY KEY,
    completed_at timestamptz NOT NULL DEFAULT now(),
    detail       jsonb
);

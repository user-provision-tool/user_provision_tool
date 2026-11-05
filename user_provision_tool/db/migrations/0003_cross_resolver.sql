-- Cross-service network grants (GRILLING G83, 2026-11-03).
--
-- The -api stores the nameserver IP and network a caller supplies as OPAQUE
-- per-service state: it does not know they are the cross resolver and the cross
-- network, and it never reads a gateway table. The pair is written only after a
-- successful async update addressed by source service, and cleared on removal.
ALTER TABLE registry.service_instances
    ADD COLUMN extra_resolver text,
    ADD COLUMN extra_network  text;

-- The resolv.conf/membership update is a first-class task: it is pollable by the
-- gateway (task_id), it is fanned out to every container of the source service,
-- and a restart cannot recover its runner (the existing recovery sweep applies).
ALTER TABLE registry.tasks DROP CONSTRAINT tasks_kind_check;
ALTER TABLE registry.tasks
    ADD CONSTRAINT tasks_kind_check
    CHECK (kind IN ('deploy', 'rebuild', 'remove', 'update'));

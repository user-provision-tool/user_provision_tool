-- G93/C3.1 — the deploy selection recorded on the instance.
--
-- The card must list the SELECTED fix artifacts and show the SELECTED nginx
-- source, not the -api's converted `.j2` and not a recipe-wide scan. The three
-- columns keep the three source kinds separate, as the selection does:
--   * compose: the ordered compose sources (plain files);
--   * nginx:   one selected source per CATEGORY (http / stream / mail);
--   * env:     the selected env files (including `.env.fix` when one is used).
-- All nullable: a legacy instance has no recorded selection and the card shows
-- no fix line rather than guessing.

ALTER TABLE registry.service_instances
    ADD COLUMN IF NOT EXISTS selected_compose_paths jsonb,
    ADD COLUMN IF NOT EXISTS selected_nginx_paths   jsonb,
    ADD COLUMN IF NOT EXISTS selected_env_paths     jsonb;

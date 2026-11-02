-- T1 — registry schema (owned by registry_app).
-- Frozen by task doc §3.3.1. This file is the -api's whole DDL; the gateway
-- never creates or alters a registry object.

CREATE TABLE registry.api_users (
    id            uuid PRIMARY KEY,
    username      citext NOT NULL UNIQUE,
    state         text NOT NULL DEFAULT 'live'
                  CHECK (state IN ('pending', 'live')),
    pending_since timestamptz,
    created_at    timestamptz NOT NULL,
    deleted_at    timestamptz
);

CREATE TABLE registry.services (
    id                uuid PRIMARY KEY,
    owner_api_user_id uuid NOT NULL REFERENCES registry.api_users(id),
    service_name      text NOT NULL,
    label             text NOT NULL,
    recipe_path       text NOT NULL,
    created_at        timestamptz NOT NULL,
    deleted_at        timestamptz
);

CREATE UNIQUE INDEX services_active_key
    ON registry.services (owner_api_user_id, service_name, label)
    WHERE deleted_at IS NULL;

CREATE TABLE registry.service_instances (
    service_id                    uuid PRIMARY KEY REFERENCES registry.services(id),
    status                        text NOT NULL
                                  CHECK (status IN ('building', 'running', 'failed',
                                                    'deleting', 'unknown')),
    current_procedure_id          uuid,
    failure                       jsonb,
    previous_status               text,
    previous_failure              jsonb,
    hostname                      text,
    subnet                        text,
    gateway                       text,
    network_name                  text,
    compose_template_path         text,
    compose_file_path             text,
    nginx_conf_paths              jsonb,
    nginx_conf_template_path      text,
    nginx_conf_path               text,
    nginx_conf_paths_by_category  jsonb,
    htpasswd_path                 text,
    ssl_certificate_path          text,
    ssl_certificate_key_path      text,
    env_files                     jsonb,
    env_file_path                 text,
    container_names               text[],
    profiles                      text[],
    volumes                       jsonb,
    build_args                    jsonb,
    tls                           boolean,
    created_at                    timestamptz,
    updated_at                    timestamptz
);

CREATE UNIQUE INDEX service_instances_subnet
    ON registry.service_instances (subnet)
    WHERE subnet IS NOT NULL;

CREATE TABLE registry.service_endpoints (
    id          bigserial PRIMARY KEY,
    service_id  uuid NOT NULL REFERENCES registry.services(id),
    served_name citext NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('http', 'stream')),
    sub_service text,
    port        integer
);

CREATE UNIQUE INDEX service_endpoints_identity
    ON registry.service_endpoints (served_name, kind);

CREATE TABLE registry.service_basic (
    service_id      uuid PRIMARY KEY REFERENCES registry.services(id),
    basic_username  text NOT NULL,
    secret_enc      bytea,
    key_version     integer,
    basic_version   integer NOT NULL,
    reset_required  boolean NOT NULL DEFAULT false
);

CREATE TABLE registry.tasks (
    task_id         text PRIMARY KEY,
    procedure_id    uuid NOT NULL,
    service_id      uuid,
    executor        text NOT NULL DEFAULT 'api',
    kind            text NOT NULL CHECK (kind IN ('deploy', 'rebuild', 'remove')),
    operation       text NOT NULL,
    request         jsonb,
    status          text NOT NULL
                    CHECK (status IN ('queued', 'running', 'completed', 'failed',
                                      'cancelled', 'unknown')),
    phase           text,
    progress        text,
    error           text,
    log_ref         text,
    result          jsonb,
    target_snapshot jsonb,
    created_at      timestamptz NOT NULL,
    updated_at      timestamptz NOT NULL,
    deleted_at      timestamptz
);

CREATE INDEX tasks_by_procedure ON registry.tasks (procedure_id, created_at DESC);
CREATE INDEX tasks_by_service ON registry.tasks (service_id, created_at DESC);

-- Cross-schema read access for the gateway (R1.3). registry_app owns these
-- objects and is the only role that may grant on them. gateway_reader is the
-- per-request read path; gateway_app needs REFERENCES for the cross-schema FKs
-- users.api_user_id and stream_exposures.service_id.
GRANT USAGE ON SCHEMA registry TO gateway_app, gateway_reader;
GRANT SELECT, REFERENCES ON registry.api_users, registry.services TO gateway_app;
GRANT SELECT ON registry.service_instances, registry.service_endpoints,
                registry.service_basic, registry.tasks TO gateway_app;
GRANT SELECT ON registry.api_users, registry.services,
                registry.service_instances, registry.service_endpoints,
                registry.service_basic, registry.tasks TO gateway_reader;

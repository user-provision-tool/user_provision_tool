#!/bin/sh
# T1 — database bootstrap. Runs once on an empty PGDATA as the superuser.
# The database service owns the database, roles, extension and schemas; it
# creates no application table (R1.1).
set -eu

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     -v reg_pw="${REGISTRY_DB_PASSWORD}" \
     -v gw_pw="${GATEWAY_DB_PASSWORD}" \
     -v reader_pw="${GATEWAY_READER_DB_PASSWORD}" \
     -v db_name="${POSTGRES_DB}" <<'SQL'
CREATE EXTENSION IF NOT EXISTS citext;

DO $do$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'registry_app') THEN
        CREATE ROLE registry_app LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gateway_app') THEN
        CREATE ROLE gateway_app LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gateway_reader') THEN
        CREATE ROLE gateway_reader LOGIN;
    END IF;
END
$do$;

ALTER ROLE registry_app   PASSWORD :'reg_pw';
ALTER ROLE gateway_app    PASSWORD :'gw_pw';
ALTER ROLE gateway_reader PASSWORD :'reader_pw';

-- Separate schemas, one owner each (R1.1/R1.2).
CREATE SCHEMA IF NOT EXISTS registry AUTHORIZATION registry_app;
CREATE SCHEMA IF NOT EXISTS gateway  AUTHORIZATION gateway_app;

GRANT CONNECT ON DATABASE :"db_name" TO registry_app, gateway_app, gateway_reader;
GRANT USAGE ON SCHEMA gateway TO gateway_reader;
SQL

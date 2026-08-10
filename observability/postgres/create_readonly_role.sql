-- Read-only Postgres role for the Grafana observability profile.
--
-- Idempotent: safe to run on every `docker compose --profile observability up`.
-- This is required rather than an initdb mount, because
-- docker-entrypoint-initdb.d only fires on an EMPTY data directory and the
-- pgdata volume is already populated.
--
-- Invoked by: the grafana-db-init service in docker-compose.yml
-- Requires:   psql variable `grafana_password`

SELECT 'CREATE ROLE grafana_ro LOGIN PASSWORD ' || quote_literal(:'grafana_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_ro')
\gexec

-- Keep the password in step with the environment on every run.
ALTER ROLE grafana_ro WITH LOGIN PASSWORD :'grafana_password';

-- Least privilege: connect and read, nothing else. No CREATE on the schema,
-- and deliberately no grant on audit_log, users, orgs or any auth table —
-- no dashboard in this profile queries them.
--
-- current_database(), not a literal "nlrf": docker-compose.yml connects psql
-- with -d ${POSTGRES_DB:-nlrf}, so a hardcoded name here would fail under
-- ON_ERROR_STOP=1 (and abort grafana-db-init) the moment POSTGRES_DB is set
-- to anything else.
SELECT format('GRANT CONNECT ON DATABASE %I TO grafana_ro', current_database())
\gexec
GRANT USAGE ON SCHEMA public TO grafana_ro;

-- Revoke first, so a previous, wider version of this script can never leave
-- stale privileges behind; then grant exactly the seven tables this profile
-- is allowed to read.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM grafana_ro;
GRANT SELECT ON
    workflow_metrics,
    test_runs,
    llm_traces,
    execution_records,
    learning_metrics,
    nl_feedback_corrections,
    trigger_events
TO grafana_ro;

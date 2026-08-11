-- Schema and tables for the bench-history dashboards.
--
-- Idempotent: safe to run on every `docker compose --profile observability up`
-- and on every loader invocation. Two consumers run this file:
--   1. the grafana-db-init service in docker-compose.yml, BEFORE
--      create_readonly_role.sql — otherwise that script's GRANT on bench.*
--      would fail under ON_ERROR_STOP=1 and abort the init container.
--   2. bench/load_history.py, so the loader works with the observability
--      profile down.
--
-- Detachment: the application never references schema `bench`. Bench data must
-- never reach History or the production metrics dashboards.

CREATE SCHEMA IF NOT EXISTS bench;

CREATE TABLE IF NOT EXISTS bench.sweeps (
    sweep_name         text PRIMARY KEY,
    captured_at        timestamp NOT NULL,
    -- 'meta' when read from <sweep>.csv.meta.json, 'mtime' when that sidecar
    -- is absent. 3 of 78 sweeps have no sidecar and are dated by file mtime,
    -- which a copy resets — it is a weaker source and panels say so.
    captured_at_source text NOT NULL,
    family             text NOT NULL,
    run_count          integer NOT NULL,
    expected_count     integer NOT NULL,
    is_complete        boolean NOT NULL,
    is_flagged_invalid boolean NOT NULL,
    -- Set when this sweep's workflow_id set overlaps an already-loaded sweep
    -- by >= 90%: it is a re-rendering of that experiment, not a new one.
    -- Deliberately NOT a foreign key — an FK would force load order, and the
    -- derived sweeps are exactly the ones with the least reliable captured_at.
    derived_from       text,
    pins               jsonb,
    git_sha            text,
    git_branch         text,
    header_shape       integer NOT NULL
);

CREATE TABLE IF NOT EXISTS bench.runs (
    sweep_name                text NOT NULL
        REFERENCES bench.sweeps(sweep_name) ON DELETE CASCADE,
    query_id                  text NOT NULL,
    repeat_index              integer NOT NULL,
    -- Nullable: 3 corpus rows record a generation error before an id existed.
    workflow_id               text,
    query                     text,
    started_at                text,
    generation_status         text,
    test_status               text,
    dryrun_status             text,
    -- The empty-means-passed rule, resolved once here so no panel author can
    -- get it wrong. See history_lib.normalise_dryrun for the branch order.
    dryrun_status_normalised  text NOT NULL,
    plan_s                    double precision,
    identify_s                double precision,
    assemble_s                double precision,
    dryrun_s                  double precision,
    exec_s                    double precision,
    total_s                   double precision,
    llm_calls                 integer,
    llm_tokens                integer,
    prompt_tokens             integer,
    completion_tokens         integer,
    llm_cost_usd              double precision,
    total_elements            integer,
    successful_elements       integer,
    failed_elements           integer,
    locator_success_rate      double precision,
    flake_retries             integer,
    dryrun_repairs            integer,
    cold_start_s              double precision,
    cleanup_s                 double precision,
    locator_timer_count       integer,
    locator_latency_ms_median double precision,
    locator_latency_ms_p90    double precision,
    probe_total               integer,
    probe_unique              integer,
    duplicate_lookup_rate     double precision,
    submit_s                  double precision,
    queue_s                   double precision,
    session_setup_s           double precision,
    agent_setup_s             double precision,
    agent_run_s               double precision,
    postprocess_s             double precision,
    poll_wait_s               double precision,
    -- Present in exactly 1 of 78 sweeps: added at header shape 50 and dropped
    -- again at shape 51. Schema drift here is not monotone.
    agent_steps               integer,
    dom_elements_max          integer,
    dom_elements_median       double precision,
    llm_429_count             integer,
    retry_lost_s              double precision,
    llm_total_s               double precision,
    llm_max_s                 double precision,
    llm_calls_actual          integer,
    steps_total_s             double precision,
    llm_coverage_gap          double precision,
    browser_use_llm_calls     integer,
    step_budget_exhausted     boolean,
    -- Full captured workflow_metrics `data` object, or NULL for the 31
    -- referenced runs with no usable payload. No panel may read NULL as zero.
    metrics                   jsonb,
    test_run                  jsonb,
    -- Any CSV header key the loader does not recognise. Schema drift is not
    -- monotone, so forward-compatibility cannot be assumed.
    extra                     jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- On-disk path, rendered as copyable text. Never a link: Grafana cannot
    -- serve it and file:// from an http:// page is blocked by every browser.
    artifact_dir              text,
    PRIMARY KEY (sweep_name, query_id, repeat_index)
);

CREATE INDEX IF NOT EXISTS idx_bench_runs_query_id ON bench.runs (query_id);
CREATE INDEX IF NOT EXISTS idx_bench_runs_workflow_id ON bench.runs (workflow_id);

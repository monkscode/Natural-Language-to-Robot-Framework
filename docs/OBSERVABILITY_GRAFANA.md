# Observability — querying Mark 1 from Grafana

Point Grafana at the same PostgreSQL database the app uses (`DATABASE_URL`) and run
these queries. There is no separate metrics pipeline, no log shipper and no extra
compose file to run: the app already writes everything below on every run.

> **Not to be confused with `OBSERVABILITY_BACKEND=grafana`.** That setting selects
> where *OpenTelemetry orchestration spans* are exported (Tempo). It is a different
> path and is unrelated to the SQL here. The Tempo compose file referenced by
> `docs/LLM_TRACES_GUIDE.md` does not exist in this repo.

## The two tables

| Table | One row per | Holds |
|---|---|---|
| `workflow_metrics` | successful generation | cost, tokens, per-stage breakdown, gate outcome, locator counts — all inside a `data` JSONB column |
| `test_runs` | run (any outcome) | the user's query, status, and `error_message` when generation failed |

They join on **`workflow_metrics.workflow_id = test_runs.run_id`** — the same id under
two names.

Both are org-scoped (`org_id`). Add `WHERE org_id = '<org>'` to any query below for a
single tenant.

**Bench runs are not in here.** `bench/run_bench.py` captures and then deletes its own
rows from `workflow_metrics`, `llm_traces` and `test_runs`, so dashboards show real
traffic only. Bench evidence lives in `bench/runs/<workflow_id>/`.

**Rows written before this feature have none of the new keys.** `data` is JSONB and the
fields are optional, so old rows deserialize fine — but `data->>'dryrun_status'` and
friends return `NULL` for every run predating it (351 rows at the time of writing). Add
`WHERE data ? '<key>'` when a NULL bucket would distort a panel; the examples below do
this where it matters.

## Cost and tokens per workflow

```sql
SELECT
    workflow_id,
    ts,
    (data->>'total_cost')::numeric        AS cost_usd,
    (data->>'total_llm_calls')::int       AS llm_calls,
    (data->>'crewai_tokens')::int         AS crewai_tokens,
    (data->>'browser_use_tokens')::int    AS browser_use_tokens,
    (data->>'workflow_duration_s')::numeric AS duration_s
FROM workflow_metrics
WHERE ts > now() - interval '7 days'
ORDER BY ts DESC;
```

`total_cost` is crewai + browser-use combined. Gemini's implicit-cache behaviour swings
it by a couple of percent run to run, so treat a single row's cost as indicative and
compare medians over a window, never two individual runs.

## Cost and duration per stage

`crew_stage_metrics` is a JSONB object keyed by stage — `planner`, `assembler`, and
`repair` when the dryrun gate had to re-prompt. Expanding it gives one row per stage:

```sql
SELECT
    m.workflow_id,
    stage.key                                   AS stage,
    (stage.value->>'duration_s')::numeric       AS duration_s,
    (stage.value->>'llm_calls')::int            AS llm_calls,
    (stage.value->>'tokens')::int               AS tokens,
    (stage.value->>'cost')::numeric             AS cost_usd
FROM workflow_metrics m,
     jsonb_each(m.data->'crew_stage_metrics') AS stage
WHERE m.data ? 'crew_stage_metrics'
ORDER BY m.ts DESC;
```

Median cost share by stage, which is the number to watch when tuning prompts:

```sql
SELECT
    stage.key AS stage,
    count(*)  AS runs,
    round(avg((stage.value->>'cost')::numeric), 6)      AS avg_cost_usd,
    round(avg((stage.value->>'duration_s')::numeric), 2) AS avg_duration_s
FROM workflow_metrics m,
     jsonb_each(m.data->'crew_stage_metrics') AS stage
WHERE m.ts > now() - interval '30 days'
GROUP BY stage.key
ORDER BY avg_cost_usd DESC;
```

The deterministic element-identification stage sits *between* the planner and assembler
kickoffs and is deliberately not billed to either. Its time is in `phase_timings`, which
the browser service owns.

## Dryrun gate pass rate

```sql
SELECT
    data->>'dryrun_status'                    AS status,
    count(*)                                  AS runs,
    round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct
FROM workflow_metrics
WHERE ts > now() - interval '30 days'
  AND data ? 'dryrun_status'      -- exclude rows written before this field existed
GROUP BY 1
ORDER BY runs DESC;
```

Four values are possible: `passed`, `failed`, `skipped` (gate disabled or empty code) and
`unverified` (Docker or the executor was unavailable — the code was still delivered).
`unverified` is an infrastructure signal, not a quality one; keep it out of pass-rate
numerators and denominators alike.

Runs that needed repair rounds:

```sql
SELECT
    workflow_id,
    (data->>'dryrun_attempts')::int AS attempts,
    (data->>'dryrun_repairs')::int  AS repairs,
    data->>'dryrun_status'          AS status
FROM workflow_metrics
WHERE (data->>'dryrun_repairs')::int > 0
ORDER BY ts DESC;
```

## Guardrail retries

`guardrail_attempts` counts guardrail invocations per attachment site.
`assembly_output` is the main assembler, `repair_output` is the gate's repair crew. A
count above 1 means the model returned output the parser could not take and had to be
re-prompted.

```sql
SELECT
    site.key                  AS guardrail_site,
    sum((site.value)::int)    AS invocations,
    count(*) FILTER (WHERE (site.value)::int > 1) AS runs_needing_a_retry
FROM workflow_metrics m,
     jsonb_each_text(m.data->'guardrail_attempts') AS site
WHERE m.ts > now() - interval '30 days'
GROUP BY site.key;
```

## Locator success

```sql
SELECT
    date_trunc('day', ts)                        AS day,
    round(avg((data->>'success_rate')::numeric), 3) AS avg_locator_success,
    sum((data->>'total_elements')::int)          AS elements,
    sum((data->>'failed_elements')::int)         AS failed
FROM workflow_metrics
WHERE ts > now() - interval '30 days'
GROUP BY 1
ORDER BY 1;
```

## Failures, and what the user asked for

Failed generations write **no** `workflow_metrics` row — the metrics block runs after the
dryrun gate, which a failed run never reaches. They are recorded in `test_runs` instead:

```sql
SELECT
    created_at,
    run_id,
    user_query,
    error_message
FROM test_runs
WHERE status = 'error'
  AND created_at > now() - interval '7 days'
ORDER BY created_at DESC;
```

Grouping the reasons tells you whether you are looking at rate limits, a missing
credential, or the model:

```sql
SELECT
    left(error_message, 80) AS reason,
    count(*)                AS occurrences
FROM test_runs
WHERE status = 'error' AND error_message IS NOT NULL
GROUP BY 1
ORDER BY occurrences DESC
LIMIT 20;
```

## Runs that vanished

Every run opens a `test_runs` row at status `'running'` before any work begins, and both
terminal paths overwrite it — `'generated'` on success, `'error'` on failure. So a row
still sitting at `'running'` well after it was created is a run that died without
reaching either: the process was killed, the container restarted, the machine ran out of
memory. Those are invisible in `workflow_metrics`, which is only written on the delivery
path.

```sql
SELECT run_id, user_query, created_at, now() - created_at AS age
FROM test_runs
WHERE status = 'running'
  AND created_at < now() - interval '15 minutes'
ORDER BY created_at DESC;
```

Pick the threshold above the slowest legitimate run — the 30-query bench medians about
21s of generation, and execution adds Docker time on top, so 15 minutes is generous.
The execute path also uses `'running'`, so a row here may have died during execution
rather than generation; join `workflow_metrics` on `workflow_id = run_id` to tell the two
apart (a row with metrics got as far as delivering code).

## Query text alongside metrics

The user's query is deliberately **not** duplicated onto `workflow_metrics` — it already
lives on `test_runs`. Join for it:

```sql
SELECT
    m.workflow_id,
    r.user_query,
    (m.data->>'total_cost')::numeric        AS cost_usd,
    (m.data->>'workflow_duration_s')::numeric AS duration_s,
    m.data->>'dryrun_status'                AS dryrun_status
FROM workflow_metrics m
JOIN test_runs r ON r.run_id = m.workflow_id
WHERE m.ts > now() - interval '7 days'
ORDER BY cost_usd DESC
LIMIT 50;
```

## Success rate over everything

Combining the two tables gives the honest denominator — successes plus the failures that
never reached the metrics row:

```sql
SELECT
    count(*) FILTER (WHERE status <> 'error')            AS produced_code,
    count(*) FILTER (WHERE status = 'error')             AS failed,
    round(100.0 * count(*) FILTER (WHERE status <> 'error') / nullif(count(*), 0), 1)
                                                          AS success_pct
FROM test_runs
WHERE created_at > now() - interval '30 days';
```

## A note on `llm_traces`

`llm_traces` holds one row per LiteLLM call with LiteLLM's own `response_cost`, which
includes cache discounts and is more accurate than the recomputed figure on
`workflow_metrics`. Historical rows are largely unattributed — the callback used to read
the workflow id from OpenTelemetry baggage, which cannot cross the thread-pool hop
LiteLLM dispatches callbacks across. Calls are labelled through call metadata now, so
rows written from this version onward carry `workflow_id`. Older rows do not, and are not
backfilled.

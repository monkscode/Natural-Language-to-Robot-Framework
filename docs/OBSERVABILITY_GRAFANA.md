# Observability — querying Mark 1 from Grafana

Point Grafana — or any Postgres client — at the same database the app uses
(`DATABASE_URL`) and run these queries by hand. The app already writes everything
below on every run, so that SQL-only path needs nothing extra; it is one option,
not the only one.

> **Not to be confused with `OBSERVABILITY_BACKEND=grafana`.** That setting selects
> where *OpenTelemetry orchestration spans* are exported (Tempo). It is a different
> path and is unrelated to the SQL here. The Tempo compose file referenced by
> `docs/LLM_TRACES_GUIDE.md` does not exist in this repo.

> **Running this in Grafana.** The queries below are provisioned as five
> dashboards behind a Compose profile — see
> [`observability/README.md`](../observability/README.md). Start them with
> `docker compose --profile observability up -d`. A plain `docker compose up`
> is unaffected.

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

## Read this before writing a time filter

The two tables do not keep time the same way, and mixing them silently shifts results.

| Column | Type | Written by |
|---|---|---|
| `workflow_metrics.ts` | `TIMESTAMP` (naive) | the app, as `datetime.now()` — the **backend process's local wall clock** |
| `test_runs.created_at` | `TIMESTAMPTZ` | Postgres, as `now()` |
| `llm_traces.created_at` | `TIMESTAMPTZ` | Postgres, as `now()` |

**Two duration fields, and they are not interchangeable.**
`workflow_metrics.data->>'workflow_duration_s'` is the wall clock of the whole
generation run — both crew kickoffs, the element stage and the dryrun gate. It exists
only on rows written from 2026-08 onward. `data->>'execution_time'` is the browser-use
element stage alone, passed straight through from the browser service, and it has been
written since the beginning. Measured 2026-08-12 over the 15 rows carrying both,
`execution_time` is 45% of `workflow_duration_s` on average and never exceeds it. Alias
them `run_wall_s` and `browser_stage_s` so a reader can tell which one a column is; a
test enforces that on the dashboards.

Comparing a naive `timestamp` to a `timestamptz` makes Postgres reinterpret the naive
value in the **session** time zone. So when the backend and the database disagree about
time zone, `ts` is read as if it were the database's clock and lands off by the offset.

- **Docker stack** — both containers default to UTC and no `TZ` is set in
  `docker-compose.yml`, so they agree and `ts` is correct.
- **Source stack (`./run.sh`)** — the backend runs on your host clock while Postgres runs
  in a UTC container. On an IST host, `ts` reads 5.5 hours ahead.

A 30-day window absorbs that. A **daily bucket does not** — with a +5:30 offset every run
after 18:30 local lands in the next day. An hourly panel would be badly wrong.

**So: bucket and window on `test_runs.created_at`, which is tz-aware and authoritative.**
Use `ts` for ordering within the metrics table, not for calendar arithmetic you care
about. Making `ts` tz-aware is a column migration with a backfill decision for the
existing rows — deliberately not done here.

## Cost and tokens per workflow

```sql
SELECT
    workflow_id,
    ts,
    (data->>'total_cost')::numeric        AS cost_usd,
    (data->>'total_llm_calls')::int       AS llm_calls,
    (data->>'crewai_tokens')::int         AS crewai_tokens,
    (data->>'browser_use_tokens')::int    AS browser_use_tokens,
    (data->>'workflow_duration_s')::numeric AS run_wall_s
FROM workflow_metrics
WHERE ts > now() - interval '7 days'
ORDER BY ts DESC;
```

`total_cost` is crewai + browser-use combined. Gemini's implicit-cache behaviour swings
it by a couple of percent run to run, so treat a single row's cost as indicative and
compare medians over a window, never two individual runs.

**Always group cost by model.** `data->>'model_provider'` and `data->>'model_name'` say
what produced the figures, so a median that spans a model or provider change does not
silently blend two different price points:

```sql
SELECT
    data->>'model_provider'                       AS provider,
    data->>'model_name'                           AS model,
    count(*)                                      AS runs,
    round(avg((data->>'total_cost')::numeric), 4) AS avg_cost_usd
FROM workflow_metrics
WHERE data ? 'model_name'
GROUP BY 1, 2
ORDER BY runs DESC;
```

`llm_traces.model` is not a substitute for `model_provider`: LiteLLM strips the provider
prefix before the trace callback runs, so those rows read `gemini-3.5-flash` and cannot
tell Vertex from AI Studio.

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
WHERE jsonb_typeof(m.data->'crew_stage_metrics') = 'object'
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
  AND jsonb_typeof(m.data->'crew_stage_metrics') = 'object'
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
  AND jsonb_typeof(m.data->'guardrail_attempts') = 'object'
GROUP BY site.key;
```

## Locator success

Bucketed on `test_runs.created_at`, not `ts` — see the time-filter note above; a daily
bucket is exactly the granularity the naive-`ts` offset breaks. The join below is a
`LEFT JOIN`, not the inner join this query used to run. An inner join here silently
drops every `workflow_metrics` row with no matching `test_runs` row, and as of
2026-08-10 that is 92% of them (35 of 434 join). Bucketing is still by `r.created_at`,
so a row needs that partner to land in any day's total even after the fix — the LEFT
JOIN stops the query from silently excluding rows it has no way to date, it does not
make this particular day-bucketed view cover all 434. For the honest total across every
row, not just the ones with a `test_runs` partner, see the locator-reliability Grafana
dashboard (`observability/grafana/dashboards/locator-reliability.json`), which reads
`workflow_metrics` directly with no join at all.

```sql
SELECT
    date_trunc('day', r.created_at)                   AS day,
    round(avg((m.data->>'success_rate')::numeric), 3) AS avg_locator_success,
    sum((m.data->>'total_elements')::int)             AS elements,
    sum((m.data->>'failed_elements')::int)            AS failed
FROM workflow_metrics m
LEFT JOIN test_runs r ON r.run_id = m.workflow_id
WHERE r.created_at > now() - interval '30 days'
GROUP BY 1
ORDER BY 1;
```

`total_elements` and `failed_elements` are the honest source of failure counts on this
row — keep using them. `success_rate` has the same limitation as the
`element_approach_metrics` array covered in `observability/README.md`: it is computed
only from elements the locator pipeline actually located, so it reads higher than the
real rate.

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
lives on `test_runs`. Join for it — as a **`LEFT JOIN`**, not the inner join this query
used to run: on the same 7-day window this query uses, `workflow_metrics` alone returns
49 rows, and an inner join against `test_runs` drops all but 7 of them (measured
2026-08-10). The `WHERE` clause below filters only on `m.ts`, not on any `test_runs`
column, so the `LEFT JOIN` genuinely keeps all 49 — `r.user_query` is simply `NULL` for
the rows with no `test_runs` partner:

```sql
SELECT
    m.workflow_id,
    r.user_query,
    (m.data->>'total_cost')::numeric        AS cost_usd,
    (m.data->>'workflow_duration_s')::numeric AS run_wall_s,
    m.data->>'dryrun_status'                AS dryrun_status
FROM workflow_metrics m
LEFT JOIN test_runs r ON r.run_id = m.workflow_id
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

## Tracing one run end to end

This is the path to walk when someone reports a bad run and gives you an id. Everything
below keys on the same UUID — `workflow_id` in two tables, `run_id` in the third.

**1. What the user asked for, and how it ended.**

```sql
SELECT status, user_query, error_message, created_at, updated_at
FROM test_runs WHERE run_id = '<id>';
```

`status = 'error'` means generation never produced code, and `error_message` is the
reason. A row still at `'running'` long after `created_at` means the run died without
reaching either terminal path.

**2. What it cost and what the gate did.**

```sql
SELECT
    data->>'workflow_duration_s'  AS run_wall_s,
    data->>'total_cost'           AS cost_usd,
    data->>'dryrun_status'        AS gate,
    data->>'dryrun_attempts'      AS attempts,
    data->>'dryrun_repairs'       AS repairs,
    jsonb_pretty(data->'crew_stage_metrics') AS by_stage,
    jsonb_pretty(data->'guardrail_attempts') AS guardrails
FROM workflow_metrics WHERE workflow_id = '<id>';
```

No row here means the run never got past the gate — go back to step 1 for the reason.

**3. Every model call it made.**

```sql
SELECT created_at, model, duration_ms, prompt_tokens, completion_tokens, cost_usd, status
FROM llm_traces WHERE workflow_id = '<id>'
ORDER BY start_time_ns;
```

These rows exist **even when the run failed**, because they are written per successful
LLM call as the run proceeds. So a run that died in the assembler has no metrics row but
its spend is still attributable here. That is the only place to recover the cost of a
failed run.

**4. The log lines.** Every record carries `workflow_id`, bound once at the start of the
run — so no call site had to thread it through. The browser service binds the *same* id
in its own logs, so one filter spans both processes. `application.log`'s format depends
on how the process was started: under `./run.sh`'s default dev mode it is structlog's
human-readable console text (`LOG_FORMAT=console`); under bench mode, or in a container,
it is one JSON object per line (`LOG_FORMAT=json`, or unset — JSON is the default). A
plain substring grep works against either, so it doesn't matter which one you're looking
at:

```bash
# local — matches whichever format wrote the file
grep '<id>' logs/application.log

# containers — same grep, against the container logs directly
docker compose logs fastapi browser-service | grep '<id>'
```

A log shipper now exists: Loki and Alloy, running behind the `observability` Compose
profile, ship both services' container logs automatically — see
[`observability/README.md`](../observability/README.md). Alloy keys on the
`com.docker.compose.service` label Docker Compose always sets on every container, not on
the `service=<name>` string under `logging.options.labels` in `docker-compose.yml` — that
string is a log-driver option, and only becomes an actual container label if a container
label with that exact key already exists, which none here do (verified with `docker
inspect --format '{{json .Config.Labels}}'`: no `service` key, only
`com.docker.compose.service`).

## A note on `llm_traces`

`llm_traces` holds one row per LiteLLM call **plus** one row per OpenTelemetry span,
in the same table. Measured 2026-08-12: 100,907 rows, of which 2,829 carry a `model`
and are LLM calls; the rest are HTTP, agent, task and orchestration spans. Filter with
`WHERE nullif(model, '') IS NOT NULL` before counting or averaging anything, or you are
measuring HTTP traffic. The LLM rows carry LiteLLM's own `response_cost`, which includes
cache discounts and is more accurate than the recomputed figure on `workflow_metrics`;
`cost_usd` is non-zero on 1,603 of the 2,829 and `total_tokens` on 1,874, so any spend
total from this table is a floor, not a total. Historical rows are largely unattributed — the callback used to read
the workflow id from OpenTelemetry baggage, which cannot cross the thread-pool hop
LiteLLM dispatches callbacks across. Calls are labelled through call metadata now, so
rows written from this version onward carry `workflow_id`. Older rows do not, and are not
backfilled.

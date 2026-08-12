# Running the Grafana observability stack

Four containers behind a Compose profile. A plain `docker compose up` does not
start any of them.

```bash
# Optional: set two passwords in the ROOT .env, next to docker-compose.yml —
# NOT src/backend/.env. Compose reads the root file for ${VAR} substitution
# in docker-compose.yml; src/backend/.env is injected into containers by a
# separate mechanism (env_file:) that neither of these two services uses, so
# setting them there has no effect. See the ROOT .env.example, not
# src/backend/.env.example. Skip this and compose falls back to
# grafana_ro / admin — fine for a throwaway local look, not for anything
# you'd leave running.
#   GRAFANA_DB_PASSWORD=...
#   GRAFANA_ADMIN_PASSWORD=...

docker compose --profile observability up -d
```

Grafana: <http://127.0.0.1:3001>, user `admin`, password whatever you set
`GRAFANA_ADMIN_PASSWORD` to (`admin` if you didn't).

If you also want the containerised `fastapi`/`browser-service` to be able to run a
Vertex generation — so there is fresh data for these dashboards to show — read
"The Vertex key needs the `docker-compose.vertex.yml` overlay" below before you start.

## What runs, and why

| Service | Role |
|---|---|
| `grafana` | Dashboards. Reads Postgres as `grafana_ro` and Loki over HTTP |
| `loki` | Log store, filesystem-backed, 7-day retention |
| `alloy` | Scrapes Docker `json-file` logs for `fastapi` and `browser-service` |
| `grafana-db-init` | One-shot. Creates the read-only role. Idempotent |

`grafana-db-init` exists rather than an initdb mount because
`docker-entrypoint-initdb.d` only fires on an empty data directory, and the
`pgdata` volume is already populated. It reruns its `ALTER ROLE ... PASSWORD`
step every time you bring the profile up, so changing `GRAFANA_DB_PASSWORD`
between runs re-syncs the role instead of leaving it stale.

`grafana_ro` can `SELECT` on exactly seven tables — `workflow_metrics`,
`test_runs`, `llm_traces`, `execution_records`, `learning_metrics`,
`nl_feedback_corrections`, `trigger_events` — and nothing else. No dashboard
here queries `users`, `orgs` or `audit_log`, and the role has no grant on them.

## The dashboards

| Dashboard | uid | Use it when |
|---|---|---|
| Trace one run | `mark1-trace-run` | Someone reports a bad run and gives you an id |
| Locator reliability | `mark1-locators` | Asking how often element location fails, and on which runs — see the caveat below |
| Execution outcomes | `mark1-execution` | Asking whether generated tests pass when they run |
| Cost, latency and capacity | `mark1-cost` | Investigating spend, slowness or rate limiting |
| Learning health | `mark1-learning` | Asking whether hints are helping, and what they cost |
| Bench — where the framework is weakest now | `bench-weakest-now` | Ranking queries by how much worse they are doing in a recent window of sweeps than all-time |
| Bench — did my change help | `bench-change-impact` | Comparing two chosen sweeps head to head — pass rate, locator success, flake and cost |

Open one directly at `http://127.0.0.1:3001/d/<uid>` — Grafana fills in the
rest of the URL for you.

The two bench dashboards query only the `bench` schema — described below —
and never an application table. That is the detachment rule this whole corpus
depends on, and a test enforces it.

**Locator reliability shows the real number, not the flattering one.**
`element_approach_metrics` — a JSON array on `workflow_metrics` — records only
elements the locator pipeline actually located. A failed attempt is routed to
a rejected-payloads bucket and has its metrics stripped before storage
(`workflow.py:2181` in the browser-service repo — a deliberate, documented
deferral, not a bug). Counting that array alone reports 100% success, which is
an artifact of what gets written, not a measurement. The dashboard instead
sums the row-level `total_elements` / `successful_elements` / `failed_elements`
fields that sit alongside the array, which do count failures: **56.79%**
success across real runs, measured 2026-08-10 — 405 elements attempted, 230
found, 172 failed. A per-element breakdown by locator approach, element tag,
DOM shape or domain is **not available** with today's data: four panels that
tried it were removed, because every failure-rate column they produced was
arithmetically 0% for every grouping — the array they queried cannot hold a
failure. That gap only closes if the browser service stops discarding
rejected-payload metrics, which is a change to a different repository and out
of scope here.

## Loading the bench sweep corpus

`bench.sweeps` and `bench.runs`, in the same Postgres instance, hold every
dated bench sweep so the two dashboards above can compare runs across time
instead of only watching production traffic.

**`run_bench.py` loads the sweep it just finished automatically — you rarely
need to run the loader by hand.** The call is best-effort: a database that is
down, or any other failure, costs the dashboard refresh, never the sweep,
because the sweep's CSV is already safely written to disk before the load is
attempted; the failure is logged with the exact command to re-run. To load
the whole corpus, or catch up after the database was down for a sweep:

```bash
PYTHONPATH=. DATABASE_URL=... venv/Scripts/python.exe bench/load_history.py
```

Verified against the full corpus on 2026-08-12: `loaded 78 sweeps / 1984 runs
(3 derived, 0 unknown columns)`.

**The loader is idempotent — re-running it is always safe.** It is an upsert
over files already on disk, not an append. Run twice back to back on
2026-08-12 and both runs printed the identical line: `loaded 78 sweeps / 1984
runs (3 derived, 0 unknown columns)`.

**Three of the 78 sweeps are dated by file mtime, not a recorded
`captured_at`.** They have no `.meta.json` sidecar, and mtime is weaker
provenance — copying a file resets it. Find them yourself rather than trusting
a name in this doc:

```sql
SELECT sweep_name FROM bench.sweeps WHERE captured_at_source = 'mtime';
```

Their filenames are deliberately not printed here — this repo is public, and
one of the three carries an ASTPP customer-query prefix. They are also,
exactly, the three sweeps `derived_from` marks below: no fourth mtime-only
sweep, and no derived sweep that has a sidecar.

**Three sweeps carry `derived_from` and are hidden from both sweep selectors
on Bench — did my change help.** They are re-costings of another sweep
already in the corpus, sharing every one of its run ids, so comparing one to
its parent would report a cost delta on a pass delta of zero.

**A blank `dryrun_status` usually means the gate passed — but not on a run
that never reached it, and you should never re-derive this yourself.**
`workflow_service.py:889` only attaches `dryrun_status` to the SSE `complete`
event when the gate did *not* pass, so silence is success on a completed run.
But 34 runs never completed generation at all (`generation_status = 'error'`)
and are also blank — reading those as `passed` invents a gate result they
never reached. The loader resolves this once, into
`dryrun_status_normalised`, so no panel has to get it right itself. Measured
2026-08-12: 1,948 rows `passed`, 34 `not_reached`, 2 `failed`.

**`llm_429_count` is not a trust signal, and its correlation with pass count
has the wrong sign to read either way.** As of 2026-08-12, 46 of the 78 sweeps
carry the column at all; 32 don't. Across the 46 that do, more 429s go with a
slightly *higher* pass count, r = +0.29 (+0.25 restricted to the 36 that are also
complete, non-derived and non-invalid). That is confounding by time — later
sweeps ran busier and also generally scored better — not a quality signal in
either direction. Counting 429 lines in `application.log` is still the only
honest way to get this number.

**None of the 78 historical sweeps carries a `git_sha` or `git_branch`.**
That provenance cannot be recovered after the fact and will not be
back-filled. Every sweep captured from now on records both.

**Artifacts and `llm_traces` stay on disk — only `bench.sweeps` and
`bench.runs` are loaded.** As of 2026-08-12 that's roughly 1.9 GiB of run
artifacts and about 95 MiB of `llm_traces.json` against under 10 MiB of source
JSON actually loaded — all three grow with every sweep, so the ratio between
them is the point, not the digits, and the dashboards say which run, not what
to fetch. `artifact_dir` is loaded as plain text where a run has one; nothing
should turn it into a link — Grafana cannot serve local files, and a
`file://` link from an `http://` page is blocked by every browser.

## Things that will mislead you if you do not know them

- **The log panel stays empty if you're running the app with `./run.sh`.**
  Alloy only scrapes Docker `json-file` container logs. `./run.sh` runs
  FastAPI and the browser service as local processes, not containers, so
  nothing they log ever reaches Loki. This is the single most likely reason
  an operator will think the trace dashboard's log panel is broken. It isn't
  — bring the app up as containers (`docker compose up -d fastapi
  browser-service runner-exec postgres`, or the full default stack) for logs
  to show up next to the SQL.
- **The time picker moves some panels and not others.** Narrow the range to
  six hours on "Cost, latency and capacity" and exactly one panel of nine
  changes — "LLM calls and failures per hour", the only one spined on a tz-aware
  column. The other eight read `workflow_metrics`, and that table's single
  time column, `ts`, cannot be filtered honestly: it is naive, and it is not
  consistently naive. Measured 2026-08-10 against `llm_traces.created_at`
  over the 146 rows that join, 143 sit 5:30:13 ahead of UTC (written by the
  host process in local time) and 3 sit within seconds of UTC (written by the
  containerised backend). The skew depends on which process wrote the row, so
  no `AT TIME ZONE` correction fixes it. Every affected panel now says
  "All-time" in its description, and a test enforces that. The full picture:

  | Dashboard | Panels that follow the picker |
  |---|---|
  | Execution outcomes | 7 of 7 — `execution_records` is tz-aware throughout |
  | Learning health | 3 of 7 — `learning_metrics`/`trigger_events` cast their text timestamps; the two hint-lifecycle panels are all-time on purpose, being a snapshot of the current hint set |
  | Cost, latency and capacity | 1 of 9 |
  | Locator reliability | 0 of 4 |
  | Trace one run | SQL panels select one run by id, so time is not a dimension; the Loki log panel does follow the picker |

  Making `ts` tz-aware is an application change plus a ruling on how to
  backfill 143 rows whose intended instant is ambiguous. That is a separate
  effort, not this branch.
- **The compose stack runs prebuilt images, and `docker compose build` is a
  no-op for them.** `fastapi`, `runner-exec` and `browser-service` are
  declared image-only (`image: ${FASTAPI_IMAGE_TAG}`, etc.) with no `build:`
  section. Verified 2026-08-10:

  ```bash
  $ docker compose build fastapi
  time="..." level=warning msg="No services to build"
  $ echo $?
  0
  ```

  That is success having done nothing. The real sequence to pick up a local
  code change is:

  ```bash
  docker build -f Dockerfile.fastapi -t monkscode/nlrf:fastapi-local .
  FASTAPI_IMAGE_TAG=monkscode/nlrf:fastapi-local \
    docker compose up -d --force-recreate fastapi runner-exec
  ```

  The tag has to be named on the second line too. `docker-compose.yml`
  resolves `${FASTAPI_IMAGE_TAG:-monkscode/nlrf:fastapi-latest}`, so without
  it compose recreates both containers on the *published* image and silently
  discards the one you just built. The root `.env.example` does set this
  variable, so an operator who copied it is already fine — the inline
  assignment makes the command right either way. `runner-exec` reads the same
  variable, which is why one assignment covers both services.

  This matters for these dashboards specifically: a stale image keeps writing
  `workflow_metrics`/`llm_traces` rows that are missing every field a newer
  panel reads, so the dashboards degrade into empty panels with no error
  anywhere — nothing tells you the image is old. On this deployment a June
  image ran for weeks before anyone noticed, and it was the SPA breaking, not
  a dashboard, that gave it away.
- **The Vertex key needs the `docker-compose.vertex.yml` overlay, and it must
  exist at the repo root.** The base `docker-compose.yml` does not mount
  `credentials.json` — that is deliberate, see `docker-compose.vertex.yml`'s
  own header. Bring the stack up with both files:

  ```bash
  docker compose -f docker-compose.yml -f docker-compose.vertex.yml --profile observability up -d
  ```

  The overlay mounts the file into both `fastapi` and `browser-service` **and**
  sets `VERTEXAI_CREDENTIALS=/app/credentials.json` — without it,
  `VERTEXAI_CREDENTIALS=credentials.json` is a relative path that resolves
  under `./run.sh` but not inside a container, so the containerised stack can
  never complete a Vertex generation — meaning no fresh rows for any
  dashboard here to show, silently. The file is gitignored and not shipped;
  put your own at the repo root before bringing the stack up (see the root
  `README.md`'s Quick Start). Verify it landed inside the container:

  ```bash
  MSYS_NO_PATHCONV=1 docker exec nlrf-fastapi ls -l /app/credentials.json
  ```
- **`docker exec` with an absolute container path fails from Git Bash —
  without warning that it failed for that reason.** Drop the
  `MSYS_NO_PATHCONV=1` prefix from the command just above and Git Bash's MSYS
  layer rewrites `/app/credentials.json` into a Windows host path
  (`C:/Program Files/Git/app/credentials.json`) before Docker ever sees it.
  Verified 2026-08-10 — the un-prefixed form fails with "No such file or
  directory" against a container where the file is actually present and
  readable; the prefixed form succeeds. Any `docker exec ... /absolute/path`
  command needs the same prefix from Git Bash, not just this one.
- **Never filter on `workflow_metrics.ts`.** It is a naive timestamp written
  from the backend's local clock. On this deployment it runs 5.5 hours ahead
  of the tz-aware columns. Every dashboard here filters on a tz-aware column
  instead; a test enforces it.
- **Bench runs are absent from SQL but present in logs.** `bench/run_bench.py`
  deletes its own rows from `workflow_metrics`, `llm_traces` and `test_runs`,
  but its log lines still reach Loki. The "Aggregate error logs" panel on
  Execution outcomes reads that same stream and is knowingly exposed to this
  pollution — its own description says so, because a bench sweep targets the
  same containerised backend address `run_bench.py` uses by default, with no
  label separating the two. The run-scoped panel on Trace one run is still the
  one that cannot be polluted; use it when you need certainty about which run
  a log line belongs to.
- **Cross-table history is sparse.** As of 2026-08-10, 35 of 434
  `workflow_metrics` rows have a matching `test_runs` row; 14 of 123
  `execution_records` join to `workflow_metrics`. Aggregate panels are built
  on one table each, not a join, for exactly this reason. An empty panel on
  the trace dashboard usually means the partner row does not exist, not that
  something broke.
- **`workflow_metrics` carries synthetic rows too, and dashboards disagree on
  whether to exclude them.** 32 of its 434 rows carry a non-UUID
  `workflow_id` — `t1`, `t2`, `t3`, `status-test`, `msg-test`, `ts-test`,
  `mapping-test`, `full-e2e-1`, `live-t1`, `multi-test` — all written
  2026-06-25 by test runs, not real traffic. The locator-reliability
  dashboard filters them out by UUID shape; the cost and learning dashboards
  do not. Comparing a number across two dashboards means you may be looking
  at slightly different populations — check which filter a panel uses before
  trusting a discrepancy as real.
- **`test_runs` used to be mostly synthetic. It no longer is.** Earlier notes
  from this effort said the table was 88% test data. That was true, and was
  fixed: on 2026-08-10, 265 synthetic rows written by a test that had been
  hitting the live database were deleted, and the test was corrected so it
  cannot write there again. `test_runs` is now a small table — **38 rows**
  total, measured 2026-08-10 — so its trace-dashboard panel (one run, by id)
  is the trustworthy use of it; an aggregate over it is thin rather than
  polluted. `llm_traces`, by contrast, holds **100,907 rows** (measured
  2026-08-12) — but it is not a table of LLM calls. Only **2,829** of those
  rows carry a `model`; the rest are OpenTelemetry spans written by the same
  callback path: 60,796 HTTP client spans, 26,293 orchestration spans, 5,515
  agent spans and 5,474 task spans. Every panel that counts LLM calls must
  filter on `nullif(model, '') IS NOT NULL`, and a test enforces it. Both
  `ERROR` classes in the table belong to the span rows — the LLM failure
  count is 0.
- **A single run's cost is not comparable to another single run's.** Gemini's
  implicit cache swings it. Compare medians over a window.
- **Grafana is not embedded in the Mark 1 SPA, deliberately.** A Postgres
  datasource has one identity and cannot honour the platform-admin / org-admin
  / member scoping the API enforces.
- **Alloy mounts the Docker socket.** `docker-compose.yml` gives it
  `/var/run/docker.sock:/var/run/docker.sock:ro` so it can discover
  `fastapi`/`browser-service` containers and stream their logs. `:ro` only
  stops Alloy writing to the socket *file* — it does not restrict the Docker
  API calls made over that socket, so the container has root-equivalent
  access to the host. This is standard for any log scraper that discovers
  containers this way, and the profile is opt-in and loopback-bound; this is
  disclosure, not a change.

## Editing a dashboard

Committed JSON is the source of truth and `allowUiUpdates` is off, so edits made
in the Grafana UI are not persisted. Change the file under
`observability/grafana/dashboards/`, then:

```bash
venv/Scripts/python.exe -m pytest tests/test_observability/test_dashboards.py -v
docker compose --profile observability restart grafana
```

The SQL behind these panels, with its caveats, is documented in
[`docs/OBSERVABILITY_GRAFANA.md`](../docs/OBSERVABILITY_GRAFANA.md), which
remains the source of truth for the queries.

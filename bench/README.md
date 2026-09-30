# Benchmark harness (Task 2 — the referee for every later change)

Measures the full `/generate-and-run` pipeline on 10 frozen queries and writes
one CSV row per run. Every later task's "faster / better" claim is checked
against the baseline produced here. Guardrails that must never regress:
**locator success rate, generated-test pass rate, flake-retry count.**

## What gets measured, and from where

| Metric | Source |
|---|---|
| plan / identify / assemble / dryrun wall-clock | SSE checkpoints, timestamped client-side. Boundaries are anchored on stage STARTS (plan 5→22, identify 22→62, assemble 62→dryrun-start message, dryrun →100) because the task-completion checkpoints (20/60/80) are unreliable in the live stream: CrewAI fires the next task's start before the completion push, and the forward-only progress guard discards the completion event |
| exec wall-clock | first→last SSE `execution`-stage event |
| LLM calls / tokens / cost, locator success | the run's `workflow_metrics` Postgres row (deliberate deviation from `mark1-enhancements/00-OVERVIEW.md` §8 log-scraping — approved) |
| flake retries | `llm_cleaning_stats` (empty_response_retries + formatting_errors_detected) from the metrics row **+** dryrun repair rounds ("🔧 Fixing test code..." SSE events); repairs also get their own `dryrun_repairs` column |
| cold start / cleanup / per-element locator latency / duplicate-lookup rate | browser-service `logs/browser_use.log` (structlog JSON by default, console format when `LOG_FORMAT=console` — both parsed), offset-based read per run |
| identify-phase breakdown (`submit_s`, `queue_s`, `session_setup_s`, `agent_setup_s`, `agent_run_s`, `postprocess_s`, `poll_wait_s`) | `phase_timings` on the metrics row. The service owns the five middle spans; the backend owns `submit_s` and `poll_wait_s` |
| agent diagnostics (`dom_elements_max/median`, `llm_429_count`, `retry_lost_s`, `llm_total_s`, `llm_max_s`, `llm_calls_actual`, `steps_total_s`, `llm_coverage_gap`) | `agent_diagnostics` on the metrics row, extracted from the browser-use agent history |
| `browser_use_llm_calls` | the metrics row. Despite the name this is `len(agent_result.history)` — a STEP count. `llm_calls_actual` is the API-call count and differs whenever a step retries. Replaced the removed `agent_steps` column |
| `step_budget_exhausted` | derived: `browser_use_llm_calls >= 3 * total_elements + 10`. `1`/`0`/empty, where empty means it could not be scored |

Stage precision is **±1s** — the server's SSE drain loop polls once per
second. Accepted in the approved design; medians over ≥3 repeats absorb it.

**`poll_wait_s` is not a partition member.** It accumulates the poll interval
on every iteration, so it overlaps all five service-side spans rather than
sitting beside them. Summing the seven columns does not reconstruct
`identify_s`; the grid tail is `poll_wait_s` minus the sum of the service spans.

**`llm_calls` changed meaning on `c4df7d0`.** Before it, the browser-use
contribution to `total_llm_calls` was a step count; after it, the measured
API-call count (`llm_calls_actual`). Across 178 captured rows carrying both,
177 agree and one differs by 1 — so the series stays broadly comparable, but a
one-call delta across that boundary is a definition change, not a regression.

**`--out` refuses to append to a CSV written under a different schema.**
`agent_steps` was removed from the middle of the column list, and DictWriter
writes by current fieldnames without re-reading the header — appending across
that boundary silently shifted every later diagnostic onto the wrong column.
`gate_schema` now exits first. Use a fresh `--out` path.

Read **budget exhausted** and **runs w/ unresolved elems** together, never
alone: a change that makes the agent give up early instead of looping drives
exhaustion to zero while the miss count stays put. The miss count is scored as
`successful_elements < total_elements`, not from `failed_elements`, which
pre-2026-07-25 browser-service builds write as `0` while genuinely losing
elements.

The `LOCATOR_TIMER` log line comes from browser-service
(`agent/actions.py`, around the `find_unique_locator_at_coordinates` call) and
requires the synced browser-service version that emits it.

`duplicate_lookup_rate` (from `LOCATOR_PROBE` lines) is recorded now and
**parked for Task 15** — no decision is taken on it here.

## Pinned environment (a baseline is only comparable to runs pinned the same way)

On the **nlrf server** process:

- `OPTIMIZATION_ENABLED=false` — learning stays OFF for guardrail runs.
  Production runs with it on, but the referee needs determinism: the frozen
  queries repeat verbatim, so hints learned in run N would inject into run
  N+1, and remembered hints can mask locator regressions. Learning-ON bench
  runs are allowed for exploration but are **never comparable to the baseline**.
- Same model provider/name as the baseline run (`MODEL_PROVIDER`,
  `ONLINE_MODEL` in `.env`) — a model change invalidates the baseline.
- `DRYRUN_ENABLED` unchanged from the baseline run.

On the **browser-service** process:

- `BROWSER_HEADLESS=true`
- log file at `logs/browser_use.log` (default; point `BROWSER_SERVICE_LOG`
  at it)

Queries in `bench/queries.json` are **frozen verbatim** (owner-confirmed).
Known caveat: q01 reads a GitHub profile's pinned project — if the pin
changes, re-baseline.

## Running a baseline

Full stack up first (nlrf API on :5000, browser-service, Postgres, runner-exec
executor, docker). Then, from the nlrf repo root:

```powershell
# run.sh launches the service from tools/, so the live log is in THIS repo —
# pointing at the browser-service repo's logs/ yields empty log-derived metrics
$env:BROWSER_SERVICE_LOG = (Join-Path (Get-Location) "logs/browser_use.log")
# $env:BENCH_TOKEN = "<jwt>"   # only when AUTH_ENFORCED=true — see note below
# --out APPENDS (see the schema note above), and the day is not unique enough:
# several benches a day is normal, and a second run would land its rows in the
# first one's file. Name the run after the change it measures — that is what
# every committed baseline does (2026-08-03-playwright-1.62-chromium-151-run2).
python -m bench.run_bench --out "bench/baselines/$(Get-Date -Format 'yyyy-MM-dd')-what-changed.csv"
```

Auth note: when the server runs with `AUTH_ENFORCED=true`, `BENCH_TOKEN` must
be a **real login token** (POST `/auth/login` with a registered, active user).
`require_user` re-validates every token against the users table (existence,
active status, token_version), so a hand-minted JWT is rejected with 401.
Simplest alternative for a local bench stack: run the server with
`AUTH_ENFORCED=false` and no token.

Defaults: 10 queries × 3 repeats, sequential (no parallelism), base URL
`http://127.0.0.1:5000`. Expect **≈1–2.5 hours** wall-clock and real LLM
spend (~30 full generate-and-run workflows).

Reports:

```powershell
python -m bench.report bench/baselines/2026-07-03-baseline.csv
python -m bench.report bench/baselines/2026-07-03-baseline.csv candidate.csv   # deltas
python -m bench.report <csv> --by-query                                        # per-query
```

## Detachment — bench data never pollutes History / metrics / pricing

Owner requirement. After every run the runner:

1. **Captures evidence first** into `bench/runs/<workflow_id>/`:
   `workflow_metrics.json`, `llm_traces.json`, `test_runs.json`, and a copy of
   the artifact dir (`test.robot`, `log.html`, `output.xml`, screenshots).
2. **Then deletes** that run's rows from `workflow_metrics`, `llm_traces`,
   `test_runs` (by workflow_id == run_id) and removes the artifact run dir.

Capture failure → **nothing is deleted** and a loud warning is printed.
`audit_log` rows are deliberately **kept** (append-only audit trail).
Assumes the local artifact store (dev stack); S3 mode is out of scope.

Residual edge (rare, accepted): a run whose SSE stream dies after metrics are
written but before the `complete` event leaves a metrics row the runner cannot
identify (it never learned the workflow_id). If a bench session aborts
mid-run, check `workflow_metrics` for strays from that time window.

## Files

- `queries.json` — the 10 frozen queries (do not edit; see freeze note inside)
- `bench_lib.py` — pure logic (SSE mapping, parsers, report math); unit-tested in `tests/test_bench/`
- `run_bench.py` — the runner (POST → SSE → metrics row → log slice → capture → detach → CSV)
- `report.py` — median/p90 summary + baseline-vs-candidate compare
- `baselines/` — one CSV per baseline, named `<date>-baseline.csv`
- `runs/` — captured evidence per bench run (git-ignored)

## Gating a bench

`bench/gate.py` gives one verdict for a bench CSV. It compares the CSV with the one
baseline, `bench/baselines/2026-09-24-develop-b57b683-bs-1.0.39.csv`, and reads only
`bench/baselines/`, `bench/runs/` and `logs/` — no database, no network.

```powershell
python -m bench.gate bench/baselines/2026-10-01-what-changed.csv
```

It prints one line per gate and exits 0 when every gate passes, 1 when one fails, and 2
when it cannot compare:

| Gate | Passes when |
|---|---|
| PINS/INPUTS | same model/provider/optimization/dryrun pins and the same queries as the baseline, and every run's `bench/runs/<id>/` capture is present (otherwise REFUSED; a model or provider change needs a new baseline) |
| PASS RATE | >= 96.7% after provider misses are re-run |
| LOCATOR | every generated run has `locator_success_rate` 1.0 |
| FLAKE | `flake_retries - dryrun_repairs` is 0 on every generated run |
| SALVAGE | no crewai Converter (salvage) call in any run's traces, and every generated run's planner answer is captured and parses (a generated run with no planner answer in its traces makes the line CANNOT) |
| TOKENS (x4) | crewai prompt <= +10%, crewai completion <= +15%, browser-use prompt <= +5%, browser-use completion <= +15% (sum of per-query medians vs the baseline; a query whose own baseline runs spread more than 25%, or whose candidate median is 0, is listed, not summed; a family whose summed medians fall more than 25% below the baseline reads CANNOT — a lost measurement until shown otherwise) |
| HOLLOW | no hollow pass outside the registry {(q10, READ_NOT_PERFORMED), (q05, READ_LOCATOR_IS_THE_ANSWER)} (`src/backend/core/pass_quality.py`) |

Reported, never gated: the verified pass rate (passes minus hollow passes), passes read by
a numeric id, dollars, the browser-use cache share, the median paired token change, and the
number of 429 / 503 log lines in the bench's window (it says whether timing can be trusted;
timing is compared with `bench.report` against `2026-08-02-rule6-url-backstop.csv`).

**Provider misses.** A run whose generation ended on a temporary provider failure — a 429, a
5xx, a timeout or a connection error (its `test_runs.error_message` is one of the provider
sentences, or a raw `litellm.RateLimitError` / `ServiceUnavailableError` /
`InternalServerError` / `Timeout` / `APIConnectionError`) — is not a framework failure; a 400
or a setup error is. The gate writes a
one-query `--queries` file per missed query under `bench/private/gate-reruns/<csv stem>/`,
prints the `run_bench.py` command for each, and exits 2 until they are re-run (1 if another
gate already fails; a TOKENS fail waits as PENDING until then):

```powershell
python -m bench.gate <candidate.csv> --rerun <candidate>-rerun1-q03.csv --rerun <candidate>-rerun1-q10.csv
```

A re-run that misses on the provider again stays pending and may be re-run again; a real
failure is never re-run. A re-run recorded on other code (a different `git_sha` in its
`.meta.json`) is refused.

**The q10 add-on** (q10 run ten times after a planner change): write its query file with
`python -m bench.gate --write-q10-queries <file>`, run
`run_bench.py --queries <file> --repeats 10 --out <fresh>.csv`, then
`python -m bench.gate --q10-addon <fresh>.csv`. It passes when at least 6 of the 10 tests
read the titles.

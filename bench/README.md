# Benchmark harness (Task 2 — the referee for every later change)

Measures the full `/generate-and-run` pipeline on 10 frozen queries and writes
one CSV row per run. Every later task's "faster / better" claim is checked
against the baseline produced here. Guardrails that must never regress:
**locator success rate, generated-test pass rate, flake-retry count.**

## What gets measured, and from where

| Metric | Source |
|---|---|
| plan / identify / assemble / dryrun wall-clock | SSE progress checkpoints, timestamped client-side (plan 5→20, identify 22→60, assemble 62→80, dryrun 80→100) |
| exec wall-clock | first→last SSE `execution`-stage event |
| LLM calls / tokens / cost, locator success | the run's `workflow_metrics` Postgres row (deliberate deviation from `mark1-enhancements/00-OVERVIEW.md` §8 log-scraping — approved) |
| flake retries | `llm_cleaning_stats` (empty_response_retries + formatting_errors_detected) from the metrics row **+** dryrun repair rounds ("🔧 Fixing test code..." SSE events); repairs also get their own `dryrun_repairs` column |
| cold start / cleanup / per-element locator latency / duplicate-lookup rate | browser-service `logs/browser_use.log` (structlog JSON by default, console format when `LOG_FORMAT=console` — both parsed), offset-based read per run |

Stage precision is **±1s** — the server's SSE drain loop polls once per
second. Accepted in the approved design; medians over ≥3 repeats absorb it.

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
$env:BROWSER_SERVICE_LOG = "c:/Users/1dhru/Documents/Projects/browser-service/logs/browser_use.log"
# $env:BENCH_TOKEN = "<jwt>"   # only when AUTH_ENFORCED=true — see note below
python -m bench.run_bench --out bench/baselines/2026-07-03-baseline.csv
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

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
$env:BROWSER_SERVICE_LOG = "c:/Users/1dhru/Documents/Projects/Natural-Language-to-Robot-Framework/logs/browser_use.log"
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

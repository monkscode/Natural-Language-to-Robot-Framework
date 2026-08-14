"""Pure logic for the benchmark harness — no network, no DB, fully unit-testable.

Covers:
- SSE stage-duration mapping from client-timestamped /generate-and-run events
- report math (median / nearest-rank p90, summarize, compare)
- browser-service log parsing (structlog JSON *and* LOG_FORMAT=console lines)
- the read-coverage signal (a query asking for a value vs a test reading one)
- the frozen CSV row schema

Stage checkpoints are the fixed progress values pushed by
src/backend/crew_ai/progress_events.py (5/20 plan, 22/60 identify, 62/80
assemble) and workflow_service.py (100 after the dryrun gate). Execution time
is first-to-last event of the SSE 'execution' stage. Precision is ±1s (the
server's SSE drain loop polls at 1s) — accepted in the approved Task-2 design.

CSV semantics: `flake_retries` is the guardrail number =
llm_cleaning_stats(empty_response_retries + formatting_errors_detected) from
the workflow_metrics row PLUS the run's dryrun repair count. `dryrun_repairs`
is also kept as its own column for visibility. extract_metrics_fields()
returns only the metrics-row portion; run_bench adds the repairs on top.
"""

import json
import math
import re
import statistics
import subprocess
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# SSE stage mapping
# ---------------------------------------------------------------------------

# Observed live-stream reality (Task-2 Decision Log): the task-COMPLETION
# checkpoints (20/60/80) are unreliable — CrewAI fires the next task's start
# event before the completion push, and the forward-only progress guard then
# discards the completion. Boundaries are therefore anchored on the reliable
# stage STARTS (5/22/62), the dryrun-gate start message, and the terminal 100.
_PLAN_START, _IDENTIFY_START, _ASSEMBLE_START, _TERMINAL = 5, 22, 62, 100

_DRYRUN_START_MARKER = "🔬 Preparing verification environment..."
_DRYRUN_REPAIR_MARKER = "🔧 Fixing test code..."


def _scan_generation_event(t, ev, first_progress_t, dryrun_start_t):
    """Record a first-seen progress time and the dryrun-gate start; returns
    the (possibly updated) dryrun start time."""
    p = ev.get("progress")
    if p is not None and p not in first_progress_t:
        first_progress_t[p] = t
    if (dryrun_start_t is None
            and _DRYRUN_START_MARKER in (ev.get("message") or "")):
        dryrun_start_t = t
    return dryrun_start_t


def _scan_stage_boundaries(events):
    """One pass over the stream: first-seen progress times, dryrun-gate start,
    first/last execution-stage times."""
    first_progress_t = {}
    dryrun_start_t = None
    exec_first = exec_last = None
    for t, ev in events:
        if ev.get("stage") == "generation":
            dryrun_start_t = _scan_generation_event(
                t, ev, first_progress_t, dryrun_start_t)
        elif ev.get("stage") == "execution":
            if exec_first is None:
                exec_first = t
            exec_last = t
    return first_progress_t, dryrun_start_t, exec_first, exec_last


def _span(t0, t1):
    return (t1 - t0) if t0 is not None and t1 is not None else None


def stage_durations(events):
    """Map [(client_time, event_dict), ...] onto per-stage wall-clock seconds.

    Returns {'plan_s', 'identify_s', 'assemble_s', 'dryrun_s', 'exec_s',
    'total_s'} — a stage is None when either of its boundaries is missing.
    First occurrence of each boundary wins (progress only moves forward
    server-side; anything later is a stray).

    assemble ends at the dryrun-gate start message; when the gate is skipped
    (DRYRUN_ENABLED=false) it ends at progress 100 and dryrun_s is None.
    """
    first_progress_t, dryrun_start_t, exec_first, exec_last = (
        _scan_stage_boundaries(events))

    plan_t = first_progress_t.get(_PLAN_START)
    identify_t = first_progress_t.get(_IDENTIFY_START)
    assemble_t = first_progress_t.get(_ASSEMBLE_START)
    terminal_t = first_progress_t.get(_TERMINAL)
    assemble_end_t = dryrun_start_t if dryrun_start_t is not None else terminal_t

    return {
        "plan_s": _span(plan_t, identify_t),
        "identify_s": _span(identify_t, assemble_t),
        "assemble_s": _span(assemble_t, assemble_end_t),
        "dryrun_s": _span(dryrun_start_t, terminal_t),
        "exec_s": _span(exec_first, exec_last),
        "total_s": (events[-1][0] - events[0][0]) if events else None,
    }


def count_dryrun_repairs(events):
    """Number of dryrun repair rounds ("🔧 Fixing test code..." SSE events)."""
    return sum(
        1 for _, ev in events
        if ev.get("stage") == "generation"
        and _DRYRUN_REPAIR_MARKER in (ev.get("message") or "")
    )


def _apply_generation_event(ident, ev, status):
    """Fold one generation-stage event into the identity dict. Error events
    keep the workflow_id too — failed runs must still be detachable (their
    pre-failure LLM calls are already in llm_traces)."""
    if status == "complete":
        ident["generation_status"] = "complete"
        ident["workflow_id"] = ev.get("workflow_id")
        if "dryrun_status" in ev:
            ident["dryrun_status"] = ev["dryrun_status"]
    elif status == "error" and ident["generation_status"] != "complete":
        ident["generation_status"] = "error"
        ident["workflow_id"] = ident["workflow_id"] or ev.get("workflow_id")


def extract_run_identity(events):
    """Pull workflow_id / generation_status / test_status / dryrun_status."""
    ident = {"workflow_id": None, "generation_status": None,
             "test_status": None, "dryrun_status": None}
    for _, ev in events:
        stage, status = ev.get("stage"), ev.get("status")
        if stage == "generation":
            _apply_generation_event(ident, ev, status)
        elif stage == "execution" and "test_status" in ev:
            ident["test_status"] = ev["test_status"]
    return ident


# ---------------------------------------------------------------------------
# Report math
# ---------------------------------------------------------------------------

def median_p90(values):
    """(median, nearest-rank p90) of a sequence; (None, None) when empty."""
    vals = sorted(values)
    if not vals:
        return None, None
    rank = max(1, math.ceil(0.9 * len(vals)))
    return statistics.median(vals), vals[rank - 1]


def coerce(value):
    """CSV-tolerant numeric coercion: None/'' → None; numeric strings → float.

    Public because bench/report.py scores several columns with it. It was
    private once and report.py grew a near-clone (`_coerce_int`) that dropped
    an integer 0 and rejected "5.0" — two readings of the same CSV cell
    disagreeing inside one file. One coercion, one set of rules.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def summarize_rows(rows, metrics):
    """Per-metric {'median', 'p90', 'n', 'sum'} over rows (strings tolerated).

    'sum' is None — not 0 — when no row carried a usable value, so a column
    the CSV predates reads as unmeasured rather than as a measured zero.
    """
    out = {}
    for metric in metrics:
        vals = [v for v in (coerce(r.get(metric)) for r in rows) if v is not None]
        med, p90 = median_p90(vals)
        out[metric] = {"median": med, "p90": p90, "n": len(vals),
                       "sum": sum(vals) if vals else None}
    return out


def _delta_pct(base, cand):
    if base is None or cand is None:
        return None, None
    delta = cand - base
    pct = round(delta / base * 100, 2) if base != 0 else None
    return delta, pct


def compare_summaries(baseline, candidate):
    """Per-metric deltas between two summarize_rows() outputs."""
    out = {}
    for metric in baseline.keys() | candidate.keys():
        b = baseline.get(metric, {})
        c = candidate.get(metric, {})
        median_delta, median_pct = _delta_pct(b.get("median"), c.get("median"))
        p90_delta, p90_pct = _delta_pct(b.get("p90"), c.get("p90"))
        sum_delta, sum_pct = _delta_pct(b.get("sum"), c.get("sum"))
        out[metric] = {
            "baseline_median": b.get("median"), "candidate_median": c.get("median"),
            "median_delta": median_delta, "median_pct": median_pct,
            "baseline_p90": b.get("p90"), "candidate_p90": c.get("p90"),
            "p90_delta": p90_delta, "p90_pct": p90_pct,
            "baseline_sum": b.get("sum"), "candidate_sum": c.get("sum"),
            "sum_delta": sum_delta, "sum_pct": sum_pct,
            "baseline_n": b.get("n", 0), "candidate_n": c.get("n", 0),
        }
    return out


def query_ids(rows):
    """The set of query_id values present in `rows` (blank/absent → '?')."""
    return {(r.get("query_id") or "?") for r in rows}


def _median_by_query(rows, metric):
    """Per-query median of `metric`. A query with no usable value is absent."""
    groups = defaultdict(list)
    for r in rows:
        v = coerce(r.get(metric))
        if v is not None:
            groups[(r.get("query_id") or "?")].append(v)
    return {q: statistics.median(vals) for q, vals in groups.items()}


def compare_by_query(base_rows, cand_rows, metrics):
    """Paired per-query comparison: per metric, each query's median in each run.

    The bench runs a fixed query list, so every candidate row has a matching
    baseline row. Comparing them pairwise is the only reading of this bench
    that survives both of its distortions.

    A pooled median across all rows sits on a cluster boundary — the queries
    span ~31k to ~69k tokens — and flips on noise: the 2026-07-29 pair moved
    +14.2% while every per-query median was flat within ±0.7%. A pooled sum is
    just as fragile in the other direction: one runaway run (q06 repeat 3,
    180,035 tokens against a ~46,700 typical) moved the total by more than the
    entire real difference, printing -8.2% on two runs that were the same.

    Per metric: {'per_query': {qid: {'baseline', 'candidate', 'pct'}},
    'paired_pct', 'up', 'down', 'n_paired'}. 'paired_pct' is the median of the
    per-query percent changes — median, not mean, or the outlier this exists to
    survive would come straight back in. A query whose baseline median is 0 has
    no defined percentage; it is still listed, but it does not reach the median
    (failed_elements and llm_429_count are all-zero this way, and the totals
    line is what carries their signal).
    """
    out = {}
    for metric in metrics:
        base = _median_by_query(base_rows, metric)
        cand = _median_by_query(cand_rows, metric)
        per_query = {}
        pcts = []
        for q in sorted(set(base) & set(cand)):
            _, pct = _delta_pct(base[q], cand[q])
            per_query[q] = {"baseline": base[q], "candidate": cand[q], "pct": pct}
            if pct is not None:
                pcts.append(pct)
        out[metric] = {
            "per_query": per_query,
            "paired_pct": statistics.median(pcts) if pcts else None,
            "up": sum(1 for p in pcts if p > 0),
            "down": sum(1 for p in pcts if p < 0),
            "n_paired": len(per_query),
        }
    return out


# ---------------------------------------------------------------------------
# Browser-service log parsing (structlog JSON default / console variant)
# ---------------------------------------------------------------------------

# console renderer: `<iso-ts> [level   ] <event padded> [logger] key=value`
# Character classes are kept disjoint from their literal neighbours so the
# pattern matches in linear time (no backtracking blow-up on crafted lines).
_CONSOLE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^ ]*) \[\w+ *\] (?P<msg>.*)$"
)

_TIMER_RE = re.compile(
    r"LOCATOR_TIMER element_id=(?P<element_id>\S+) "
    r"duration_ms=(?P<duration_ms>[\d.]+) "
    r"found=(?P<found>True|False|true|false)"
)

_PROBE_RE = re.compile(
    r"LOCATOR_PROBE workflow_id=(?P<workflow_id>\S+) stable_hash=(?P<stable_hash>\S+)"
)


def _parse_ts(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def parse_log_line(line):
    """One log line → (timestamp | None, message text).

    JSON lines yield the 'event' field; console lines yield everything after
    the [level] bracket (trailing logger tag included — markers are matched by
    substring, so it does not interfere). Unrecognized lines (tracebacks,
    blanks) come back as (None, line) so marker matching still works.
    """
    stripped = line.rstrip("\r\n")
    candidate = stripped.strip()
    if candidate.startswith("{"):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(obj, dict):
                return _parse_ts(obj.get("timestamp")), str(obj.get("event", ""))
    m = _CONSOLE_RE.match(stripped)
    if m:
        return _parse_ts(m.group("ts")), m.group("msg")
    return None, stripped


def parse_locator_timers(lines):
    """All LOCATOR_TIMER records: [{'element_id', 'duration_ms', 'found'}, ...]."""
    out = []
    for line in lines:
        _, msg = parse_log_line(line)
        m = _TIMER_RE.search(msg)
        if m:
            out.append({
                "element_id": m["element_id"],
                "duration_ms": float(m["duration_ms"]),
                "found": m["found"].lower() == "true",
            })
    return out


def duplicate_lookup_rate(lines):
    """LOCATOR_PROBE dedup telemetry (parked for Task 15).

    A duplicate is a repeated (workflow_id, stable_hash) pair — the same DOM
    node probed again within the same workflow. The same hash in different
    workflows is expected (same page, different run), not a duplicate.
    """
    total = 0
    seen = set()
    for line in lines:
        _, msg = parse_log_line(line)
        m = _PROBE_RE.search(msg)
        if m:
            total += 1
            seen.add((m["workflow_id"], m["stable_hash"]))
    unique = len(seen)
    rate = (total - unique) / total if total else 0.0
    return {"total": total, "unique": unique, "duplicate_rate": rate}


def span_durations(lines, start_marker, end_marker):
    """Durations (s) between sequential start/end marker pairs.

    The latest unclosed start wins (a restart supersedes); an end without a
    start is ignored; an unclosed start is dropped. Lines without parseable
    timestamps cannot bound a span and are skipped.
    """
    out = []
    start_ts = None
    for line in lines:
        ts, msg = parse_log_line(line)
        if ts is None:
            continue
        if start_marker in msg:
            start_ts = ts
        elif end_marker in msg and start_ts is not None:
            out.append((ts - start_ts).total_seconds())
            start_ts = None
    return out


# ---------------------------------------------------------------------------
# Read coverage — does a query that asks for a value get a test that reads one
# ---------------------------------------------------------------------------

# Verbs that mean "give me a value back off the page". Whole words only:
# "target" contains "get" and must not count.
_READ_VERBS_RE = re.compile(r"\b(get|read|extract|retrieve|fetch)\b", re.IGNORECASE)

# Browser Library keywords that extract a VALUE from the page. Counting
# keywords are deliberately absent: "Get Elements", "Get Element",
# "Get Element Count" and "Get Length" produce the degenerate count-only test
# this signal exists to catch, so treating them as reads would defeat it.
#
# Plain substring matching is safe here because no entry is a prefix of a
# non-read Browser Library keyword. The supersets that do exist are still
# reads — "Get Texts" and "Get Attribute Names" both pull values off the page —
# so the prefixes below match them, correctly. That is also why "get texts"
# is absent: "get text" already covers it, and listing it would be dead.
_READ_KEYWORDS = (
    "get text",
    "get attribute",
    "get property",
    "get selected options",
)


def query_requests_read(query: str) -> bool:
    """True when the query asks for a value to be read back off the page.

    Word-boundary, case-insensitive match on get/read/extract/retrieve/fetch.
    On the frozen 30-query bench corpus this matches exactly q01, q05 and q10 —
    the only three that ask for a value — and nothing else.
    """
    return bool(_READ_VERBS_RE.search(query or ""))


def code_performs_read(code: str) -> bool:
    """True when the generated Robot source extracts at least one page VALUE.

    Named `code_performs_read`, not `test_performs_read`: the latter is
    collected as a test case by pytest the moment any test module imports it
    by name.

    Case-insensitive substring match against _READ_KEYWORDS. A test that only
    counts elements returns False — that is the whole point (see
    warn_if_read_query_never_reads in bench/run_bench.py for the measured
    q10 evidence).

    SUBSTRING, not Robot-cell parsing — asked and answered 2026-08-05, do not
    re-propose without new data. A read keyword sitting in `[Documentation]`,
    a comment or an argument cell would suppress the warning on a test that
    never executes a read; the hole is real. It has also never once opened: a
    keyword-cell parser (section tracking, comment stripping, assignment-target
    skipping) was built and replayed against all 1,808 captured artifacts in
    bench/runs, and the two detectors disagreed on ZERO of them. The parser is
    not a no-op — it correctly flags a synthetic `[Documentation] Get Text of
    each book title` on a count-only test. Trading 40 lines of Robot-syntax
    parsing, which this file would then have to keep correct, for a failure
    with no measured incidence is not a trade this bench needs.
    """
    lowered = (code or "").lower()
    return any(kw in lowered for kw in _READ_KEYWORDS)


# ---------------------------------------------------------------------------
# CSV schema
# ---------------------------------------------------------------------------

# The browser service caps its agent at `1 + (len(elements) * 3) + 1 + 8` steps
# (tools/browser_service/tasks/workflow.py:538) and never reports that cap, so
# the formula is mirrored here. TestCrossRepoDrift asserts the two still agree.
# Baselines predating the stored step_budget_exhausted column are scored by
# read-time derivation (bench/report.py exhaustion_counts) using whatever these
# constants are at read time — changing them retroactively re-scores every such
# CSV, not just new runs. Rows that DO carry a stored value are read from it and
# are unaffected.
STEP_BUDGET_PER_ELEMENT = 3
STEP_BUDGET_BASE = 10


def step_budget_cap(total_elements: int) -> int:
    """The browser-use max_steps the agent ran under, for `total_elements`."""
    return STEP_BUDGET_PER_ELEMENT * total_elements + STEP_BUDGET_BASE


def step_budget_exhausted(
    total_elements: int | None, browser_use_llm_calls: int | None
) -> int | None:
    """1 when the run consumed its whole step budget, 0 when it did not.

    None when it cannot be told — a run with no element count never reached
    element identification, and reporting that as "did not exhaust" would be a
    false green. Same convention as llm_coverage_gap.

    `>=`, not `==`: three runs in the captured corpus recorded one step past
    their cap, because browser-use increments its step counter in two places.
    """
    if not total_elements or browser_use_llm_calls is None:
        return None
    return int(browser_use_llm_calls >= step_budget_cap(total_elements))


CSV_COLUMNS = (
    # identity
    "query_id", "query", "repeat", "workflow_id", "started_at",
    "generation_status", "test_status", "dryrun_status",
    # per-stage wall clock (SSE checkpoints, ±1s)
    "plan_s", "identify_s", "assemble_s", "dryrun_s", "exec_s", "total_s",
    # LLM usage/cost (workflow_metrics row)
    "llm_calls", "llm_tokens", "prompt_tokens", "completion_tokens", "llm_cost_usd",
    # locator success (workflow_metrics row)
    "total_elements", "successful_elements", "failed_elements", "locator_success_rate",
    # flake retries (metrics row + SSE repair events) — see module docstring
    "flake_retries", "dryrun_repairs",
    # browser-service log metrics
    "cold_start_s", "cleanup_s",
    "locator_timer_count", "locator_latency_ms_median", "locator_latency_ms_p90",
    # duplicate-lookup telemetry (parked for Task 15)
    "probe_total", "probe_unique", "duplicate_lookup_rate",
    # identify_s phase breakdown (2026-07-26 efficiency check)
    "submit_s", "queue_s", "session_setup_s", "agent_setup_s", "agent_run_s",
    "postprocess_s", "poll_wait_s",
    # agent diagnostics
    "dom_elements_max", "dom_elements_median",
    "llm_429_count", "retry_lost_s",
    # agent_run_s split (2026-07-26). Raw measurements only — derive
    # step_non_llm_s = steps_total_s - llm_total_s and
    # agent_overhead_s = agent_run_s - steps_total_s at analysis time.
    "llm_total_s", "llm_max_s", "llm_calls_actual", "steps_total_s",
    "llm_coverage_gap",
    # The browser-use step count, replacing the removed agent_steps. Despite
    # the name this is len(agent_result.history), not an API-call count —
    # llm_calls_actual is the API-call count and differs whenever a step retries.
    "browser_use_llm_calls",
    # Derived from the two lines above: did this run burn its entire browser-use
    # step budget. 1 / 0 / empty, where empty means it could not be scored.
    # Read it beside the unresolved-elements count, never alone — see the design
    # doc section 7A.
    "step_budget_exhausted",
)


def build_csv_row(fields):
    """One complete CSV row in CSV_COLUMNS order.

    Missing fields and None become '' (empty CSV cell). Unknown keys raise —
    a typo must fail loudly, not silently drop a metric.
    """
    unknown = set(fields) - set(CSV_COLUMNS)
    if unknown:
        raise ValueError(f"Unknown CSV fields: {sorted(unknown)}")
    row = {}
    for col in CSV_COLUMNS:
        v = fields.get(col)
        row[col] = "" if v is None else v
    return row


def extract_metrics_fields(data):
    """Flatten a workflow_metrics row's data JSON into CSV fields.

    `data` is WorkflowMetrics.to_dict() as stored in the jsonb column.
    flake_retries here is the metrics-row portion only (empty-response retries
    + formatting errors detected); the runner adds dryrun repairs on top.
    """
    cleaning = data.get("llm_cleaning_stats") or {}
    timings = data.get("phase_timings") or {}
    diagnostics = data.get("agent_diagnostics") or {}
    return {
        "llm_calls": data.get("total_llm_calls", 0),
        "llm_tokens": data.get("crewai_tokens", 0) + data.get("browser_use_tokens", 0),
        "prompt_tokens": (data.get("crewai_prompt_tokens", 0)
                          + data.get("browser_use_prompt_tokens", 0)),
        "completion_tokens": (data.get("crewai_completion_tokens", 0)
                              + data.get("browser_use_completion_tokens", 0)),
        "llm_cost_usd": data.get("total_cost", 0.0),
        "total_elements": data.get("total_elements", 0),
        "successful_elements": data.get("successful_elements", 0),
        "failed_elements": data.get("failed_elements", 0),
        "locator_success_rate": data.get("success_rate", 0.0),
        "flake_retries": (cleaning.get("empty_response_retries", 0)
                          + cleaning.get("formatting_errors_detected", 0)),
        # identify_s phase breakdown — None (empty cell) on pre-instrumentation
        # rows and failed runs, which carry no phase data by construction.
        "submit_s": timings.get("submit_s"),
        "queue_s": timings.get("queue_s"),
        "session_setup_s": timings.get("session_setup_s"),
        "agent_setup_s": timings.get("agent_setup_s"),
        "agent_run_s": timings.get("agent_run_s"),
        "postprocess_s": timings.get("postprocess_s"),
        "poll_wait_s": timings.get("poll_wait_s"),
        "dom_elements_max": diagnostics.get("dom_elements_max"),
        "dom_elements_median": diagnostics.get("dom_elements_median"),
        "llm_429_count": diagnostics.get("llm_429_count"),
        "retry_lost_s": diagnostics.get("retry_lost_s"),
        "llm_total_s": diagnostics.get("llm_total_s"),
        "llm_max_s": diagnostics.get("llm_max_s"),
        "llm_calls_actual": diagnostics.get("llm_calls_actual"),
        "steps_total_s": diagnostics.get("steps_total_s"),
        # An empty cell here means coverage could not be checked at all — not
        # that it was clean. Treat it exactly like a positive gap.
        "llm_coverage_gap": diagnostics.get("llm_coverage_gap"),
        # Top-level, not under agent_diagnostics: it is a pre-existing
        # WorkflowMetrics field (BrowserUseTokenBreakdown:31).
        "browser_use_llm_calls": data.get("browser_use_llm_calls"),
        "step_budget_exhausted": step_budget_exhausted(
            data.get("total_elements"), data.get("browser_use_llm_calls")
        ),
    }


# ---------------------------------------------------------------------------
# Preflight pins (bench profile) — a baseline is only comparable to runs
# pinned the same way; these helpers gate the run and record the pins.
# ---------------------------------------------------------------------------

_COMPARABILITY_KEYS = ("optimization_enabled", "model_provider",
                       "online_model", "dryrun_enabled")


def preflight_violations(nlrf_health: dict, browser_health: dict) -> tuple[list[str], list[str]]:
    """(violations, warnings) for a pinned guardrail run. Violations block."""
    violations: list[str] = []
    warnings: list[str] = []
    pins = nlrf_health.get("pins")
    if pins is None:
        violations.append(
            "nlrf /health reports no 'pins' — server predates the bench "
            "profile; restart the stack with ./run.sh bench")
    elif pins.get("optimization_enabled") is not False:
        violations.append(
            f"OPTIMIZATION_ENABLED={pins.get('optimization_enabled')} — learning "
            f"must be OFF for a comparable run (start the stack with ./run.sh bench)")
    headless = browser_health.get("headless")
    if headless is None:
        warnings.append(
            "browser-service /health does not report 'headless' — the launcher "
            "pins it but it cannot be verified here (field arrives with a later sync)")
    elif headless is not True:
        violations.append(
            f"browser-service headless={headless} — must be true for a comparable run")
    return violations, warnings


def _git(*args: str) -> str | None:
    """A git value for the sweep sidecar, or None if git cannot answer.

    Provenance is worth having and never worth failing a 40-minute paid run
    for, so every failure mode degrades to None: no git binary (OSError), a
    non-zero exit such as `symbolic-ref` on a detached HEAD or a tarball
    checkout with no .git at all (CalledProcessError), a wedged git process
    (TimeoutExpired — both are subprocess.SubprocessError), and a branch/tag
    name whose bytes are not valid in the local codec (UnicodeDecodeError,
    which is a ValueError, raised by `text=True` decoding, not by git itself).
    The except tuple is intentionally that wide — narrower has already let one
    real failure mode (a fake branch name) through silently once.
    """
    try:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, check=True,
            timeout=10, cwd=Path(__file__).resolve().parents[1])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None


def build_meta(nlrf_health: dict, browser_health: dict,
               base_url: str, browser_url: str) -> dict:
    """Pins snapshot written beside the CSV as <out>.csv.meta.json."""
    return {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "git_sha": _git("rev-parse", "HEAD"),
        "git_branch": _git("symbolic-ref", "--quiet", "--short", "HEAD"),
        "base_url": base_url,
        "browser_url": browser_url,
        "nlrf_pins": nlrf_health.get("pins") or {},
        "browser_service": {
            "model_provider": browser_health.get("model_provider"),
            "headless": browser_health.get("headless"),
        },
    }


def meta_path_for(csv_path) -> Path:
    return Path(f"{csv_path}.meta.json")


def load_meta(csv_path) -> dict | None:
    path = meta_path_for(csv_path)
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def append_pin_conflict(csv_path, new_meta: dict) -> list[str]:
    """Pin mismatches between an existing CSV's recorded meta and this run.

    `--out` APPENDS rows, but the sidecar is ONE file per CSV: re-using a path
    across differently-pinned runs would leave a single meta claiming pins for
    rows it did not produce — destroying the comparability guarantee the
    sidecar exists to make. Returns [] when the CSV is new (nothing to
    conflict with) or when no meta was recorded (nothing to compare — the
    runner warns about that case instead of blocking).
    """
    if not Path(csv_path).exists():
        return []
    existing = load_meta(csv_path)
    if existing is None:
        return []
    return compare_pins(existing, new_meta)


def compare_pins(base_meta: dict, cand_meta: dict) -> list[str]:
    """Human-readable mismatches between two pins snapshots."""
    mismatches = []
    base_pins = base_meta.get("nlrf_pins") or {}
    cand_pins = cand_meta.get("nlrf_pins") or {}
    for key in _COMPARABILITY_KEYS:
        if base_pins.get(key) != cand_pins.get(key):
            mismatches.append(f"{key}: baseline={base_pins.get(key)!r} "
                              f"candidate={cand_pins.get(key)!r}")
    base_bp = (base_meta.get("browser_service") or {}).get("model_provider")
    cand_bp = (cand_meta.get("browser_service") or {}).get("model_provider")
    if base_bp != cand_bp:
        mismatches.append(f"browser_service.model_provider: "
                          f"baseline={base_bp!r} candidate={cand_bp!r}")
    return mismatches

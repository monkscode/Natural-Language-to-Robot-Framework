"""Benchmark runner: POST each frozen query to /generate-and-run, record one CSV row per run.

Usage (from the repo root, against a fully running dev stack):

    python -m bench.run_bench --out bench/baselines/2026-07-03-what-changed.csv

--out APPENDS, so name it after the change this run measures and never at an
existing baseline — 2026-07-03-baseline.csv is the committed frozen reference,
and pointing --out at it rewrites the distribution everything is compared to.

Environment:
    NLRF_BASE_URL        nlrf base URL (default http://127.0.0.1:5000)
    BROWSER_SERVICE_URL  browser-service base URL (default http://127.0.0.1:4999)
    BENCH_TOKEN          optional Bearer token (needed when AUTH_ENFORCED=true)
    BROWSER_SERVICE_LOG  path to the RUNNING browser-service's logs/browser_use.log;
                         unset → log-derived metrics (cold start, cleanup, locator
                         latency, probe dedup) are left empty and a warning is printed

Detachment (owner requirement — bench data must never appear in History /
metrics / pricing dashboards): after each run the runner captures evidence
locally FIRST into bench/runs/<workflow_id>/ (metrics row, llm_traces rows,
test_runs row, artifact dir copy), THEN deletes that run's rows from
workflow_metrics, llm_traces and test_runs and removes the artifact run dir.
Capture failure → nothing is deleted, a loud warning is printed. audit_log
rows are deliberately kept (append-only audit trail). Assumes the local
artifact store (dev stack) — S3 mode is out of scope for the bench.

One exception to "after each run": a run whose STREAM died is queued and
detached once the sweep is over, because at that moment the server is usually
still executing it — see queue_partial_detach.
"""

import argparse
import csv
import json
import os
import shutil
import sys
import time
from datetime import datetime
from itertools import zip_longest
from pathlib import Path

import psycopg
import requests
from psycopg.rows import dict_row

from bench.bench_lib import (
    CSV_COLUMNS,
    append_pin_conflict,
    build_csv_row,
    build_meta,
    code_performs_read,
    count_dryrun_repairs,
    duplicate_lookup_rate,
    extract_metrics_fields,
    extract_run_identity,
    median_p90,
    meta_path_for,
    parse_locator_timers,
    preflight_violations,
    query_requests_read,
    span_durations,
    stage_durations,
)
from src.backend.core.artifact_store import STAGING_ROOT
from src.backend.core.config import settings

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = REPO_ROOT / "bench" / "runs"

COLD_START_START = "🚀 Starting browser session..."
COLD_START_END = "✅ Browser session started successfully"
CLEANUP_START = "🧹 Starting browser cleanup..."
CLEANUP_END = "🧹 Cleanup complete"

# Read timeout must outlast the silent Docker-execution phase (no SSE bytes
# while robot runs in the container).
CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 1800
METRICS_ROW_RETRIES = 10


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _log(msg: str) -> None:
    print(f"[bench] {msg}", flush=True)


def _warn(msg: str) -> None:
    print(f"[bench] WARNING: {msg}", file=sys.stderr, flush=True)


def warn_if_zero_crewai_tokens(data: dict, query_id: str, repeat: int,
                               workflow_id: str) -> None:
    """Warn when a run captured no crewai tokens at all.

    crewai_tokens is ~19.9% of the baseline median llm_tokens (7,888.5 of
    39,722), so a zeroed accumulator reads as a 20% improvement against a
    flat-or-down token gate — an invisible false pass. The failure mode is
    exactly zero, not a small drift, so an equality check is the right shape.

    The reproducer is the IN-FLIGHT crewai 1.15 upgrade, where output_pydantic
    reroutes the agent loop through instructor and the accumulator never gets
    written. The bench itself still runs the pinned crewai==1.8.1 — the guard
    exists so that upgrade cannot land a silent 20% token "win".

    Deliberately a warning and not a CSV column: adding a column would trip
    gate_schema against every existing baseline.
    """
    if data.get("crewai_tokens", 0) == 0:
        _warn(f"{query_id} repeat {repeat}: crewai_tokens == 0 for {workflow_id} — "
              f"the crewai token accumulator is not being written. Do NOT trust "
              f"llm_tokens/llm_cost_usd from this run.")


def warn_if_read_query_never_reads(query: str, query_id: str, repeat: int,
                                   workflow_id: str) -> None:
    """Warn when a query asks for a value but the test never reads one.

    Measured on q10 ("Go to https://books.toscrape.com, get the titles of all
    books on the first page, and verify there are 20 books") over the 146
    captured q10 runs in bench/runs: 109 (75%) generated a test that only
    counts elements — Get Elements -> Get Length -> Should Be True — and never
    reads a title. 102 of those PASSED, so the bench reported a pass for a test
    that did half the query.

    The dryrun gate cannot catch this. It validates keyword and argument SHAPE,
    not semantics, and a count-only test is perfectly well-formed Robot
    Framework. This guard is what makes the gap visible.

    The generated test is read from bench/runs/<workflow_id>/artifacts (the
    capture_evidence copy) and falls back to the staging dir (which detach_run
    deletes, so it only survives when capture failed). Neither present → an
    unmeasurable run, and an unmeasurable run must not warn.

    Deliberately a warning and not a CSV column: adding a column would trip
    gate_schema against every existing baseline.
    """
    candidates = (RUNS_DIR / workflow_id / "artifacts" / "test.robot",
                  STAGING_ROOT / workflow_id / "test.robot")
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        return
    try:
        code = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # A guard must never be the thing that breaks a bench run.
        return
    if query_requests_read(query) and not code_performs_read(code):
        _warn(f"{query_id} repeat {repeat}: {workflow_id} — the query asks for "
              f"a value to be read off the page, but the generated test "
              f"performs no read (no Get Text/Texts/Attribute/Property/"
              f"Selected Options). A 'passed' result does NOT mean the test did "
              f"the work the query asked for.")


def fetch_health(url: str) -> dict:
    """GET {url}/health or exit with a clear pointer to ./run.sh bench."""
    try:
        resp = requests.get(f"{url}/health", timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        sys.exit(f"[bench] preflight: {url}/health unreachable ({e}) — "
                 f"start the stack with ./run.sh bench")


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------

def stream_generate_and_run(base_url: str, query: str, token: str | None,
                            sink: list | None = None):
    """POST the query, return [(client_monotonic_time, event_dict), ...].

    `sink`, when given, is the list the events are appended to, so a caller can
    still read the PARTIAL stream after this raises. Detachment is
    unconditional (see the module docstring) and the workflow already exists
    server-side by the time a read timeout can fire — without the partial
    events the caller never learns its id and the run's rows stay attached.
    """
    headers = {"Accept": "text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    events = [] if sink is None else sink
    with requests.post(
        f"{base_url}/generate-and-run",
        json={"query": query},
        headers=headers,
        stream=True,
        timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue  # heartbeats (": heartbeat") and blank keep-alives
            t = time.monotonic()
            try:
                ev = json.loads(line[len("data: "):])
            except json.JSONDecodeError:
                continue
            events.append((t, ev))
    return events


# ---------------------------------------------------------------------------
# Browser-service log (offset-based reads per run)
# ---------------------------------------------------------------------------

def log_offset(log_path: Path | None) -> int:
    if log_path is None or not log_path.exists():
        return 0
    return log_path.stat().st_size


def read_log_from(log_path: Path | None, offset: int) -> list[str]:
    if log_path is None or not log_path.exists():
        return []
    size = log_path.stat().st_size
    if size < offset:
        # RotatingFileHandler rolled over mid-run — the pre-rotation tail is
        # gone from this file; read what the fresh file has.
        _warn(f"{log_path} rotated mid-run; log metrics for this run are partial")
        offset = 0
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        return f.read().splitlines()


def log_metrics(lines: list[str]) -> dict:
    if not lines:
        # No log window (BROWSER_SERVICE_LOG unset or log unreadable) — leave
        # every column empty rather than writing zeros that read as measurements.
        return dict.fromkeys((
            "cold_start_s", "cleanup_s", "locator_timer_count",
            "locator_latency_ms_median", "locator_latency_ms_p90",
            "probe_total", "probe_unique", "duplicate_lookup_rate"))
    timers = parse_locator_timers(lines)
    lat_median, lat_p90 = median_p90([t["duration_ms"] for t in timers])
    probes = duplicate_lookup_rate(lines)
    cold = span_durations(lines, COLD_START_START, COLD_START_END)
    cleanup = span_durations(lines, CLEANUP_START, CLEANUP_END)
    return {
        "cold_start_s": round(sum(cold), 3) if cold else None,
        "cleanup_s": round(sum(cleanup), 3) if cleanup else None,
        "locator_timer_count": len(timers),
        "locator_latency_ms_median": lat_median,
        "locator_latency_ms_p90": lat_p90,
        "probe_total": probes["total"],
        "probe_unique": probes["unique"],
        "duplicate_lookup_rate": round(probes["duplicate_rate"], 4),
    }


# ---------------------------------------------------------------------------
# Postgres: metrics read + capture-before-delete detachment
# ---------------------------------------------------------------------------

def fetch_metrics_data(conn, workflow_id: str) -> dict | None:
    """The run's workflow_metrics data JSON (retried: the row lands just before
    the SSE 'complete' event, so it is normally there on the first try)."""
    for attempt in range(METRICS_ROW_RETRIES):
        row = conn.execute(
            "SELECT data FROM workflow_metrics WHERE workflow_id = %s "
            "ORDER BY ts DESC LIMIT 1",
            (workflow_id,),
        ).fetchone()
        if row:
            return row["data"]
        if attempt < METRICS_ROW_RETRIES - 1:
            time.sleep(1)
    return None


def _fetch_rows(conn, sql: str, workflow_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, (workflow_id,)).fetchall()]


def capture_evidence(conn, workflow_id: str) -> bool:
    """Copy everything the run produced into bench/runs/<workflow_id>/.

    Returns False (and deletes nothing downstream) on any failure — the
    capture-before-delete contract.
    """
    dest = RUNS_DIR / workflow_id
    try:
        dest.mkdir(parents=True, exist_ok=True)
        captures = {
            "workflow_metrics.json":
                "SELECT * FROM workflow_metrics WHERE workflow_id = %s",
            "llm_traces.json":
                "SELECT * FROM llm_traces WHERE workflow_id = %s",
            "test_runs.json":
                "SELECT * FROM test_runs WHERE run_id = %s",
        }
        for filename, sql in captures.items():
            rows = _fetch_rows(conn, sql, workflow_id)
            (dest / filename).write_text(
                json.dumps(rows, indent=2, default=str), encoding="utf-8")
        run_dir = STAGING_ROOT / workflow_id
        if run_dir.exists():
            shutil.copytree(run_dir, dest / "artifacts", dirs_exist_ok=True)
        return True
    except Exception as e:
        _warn(f"evidence capture FAILED for {workflow_id}: {e} — "
              f"run data left in place (nothing deleted)")
        return False


def detach_run(conn, workflow_id: str) -> None:
    """Delete the run's rows + artifacts. audit_log rows are kept on purpose."""
    for table, col in (("workflow_metrics", "workflow_id"),
                       ("llm_traces", "workflow_id"),
                       ("test_runs", "run_id")):
        cur = conn.execute(
            f"DELETE FROM {table} WHERE {col} = %s", (workflow_id,))
        _log(f"detached {cur.rowcount} row(s) from {table}")
    run_dir = STAGING_ROOT / workflow_id
    if run_dir.exists():
        try:
            shutil.rmtree(run_dir)
            _log(f"removed artifact dir {run_dir}")
        except OSError as e:
            _warn(f"could not remove artifact dir {run_dir}: {e}")


def queue_partial_detach(workflow_id: str | None, pending: list) -> None:
    """Record a run whose stream died mid-flight for detachment AFTER the sweep.

    The id reaches the client only on the terminal generation event
    (workflow_service.py yields "workflow_id" on complete/error, never on the
    in-progress pushes), so the failure this can act on is one during the
    EXECUTION phase — the long Docker one, and the realistic read-timeout window.

    Which is exactly why it is not detached here. The stream dying is not the
    RUN dying: at that moment the server is still blocked in
    runner_exec_client.execute, and robot_tests/<id> is a read-write bind mount
    inside the live container (docker_service.py). Deleting it there would pull
    the output directory out from under a running test, save a half-finished
    snapshot as that run's permanent evidence, and race the workflow_metrics
    insert that _process_learning makes AFTER the execution result — which would
    re-attach the run to History moments after we deleted it. By the end of the
    sweep that work has finished, and one capture sees the whole run.

    A failure before generation completes has no id to detach by, and
    capture_evidence(None) would write a bench/runs/None directory. Those
    llm_traces rows stay attached and have to be cleaned up by hand, so say so
    rather than returning quietly.
    """
    if not workflow_id:
        _warn("the stream failed before the generation event that carries the "
              "workflow_id, so this run could not be detached — any llm_traces "
              "rows it wrote are still attached to History/metrics")
        return
    # Named now, not only at drain time: if the process is killed before the
    # sweep ends, this line is the only record of what needs cleaning by hand.
    _log(f"{workflow_id}: stream failed — queued for detachment at end of sweep")
    pending.append(workflow_id)


def drain_deferred_detach(conn, pending: list) -> None:
    """Capture-then-detach every run queued by queue_partial_detach.

    Runs from a `finally`, so an aborted sweep still cleans up what it queued.
    Each id is isolated: detach_run talks to Postgres, and one connection error
    must not strand the ids behind it.
    """
    for workflow_id in pending:
        try:
            if capture_evidence(conn, workflow_id):
                detach_run(conn, workflow_id)
            else:
                _warn(f"{workflow_id}: the stream failed AND its evidence could "
                      f"not be captured — its rows are still attached to "
                      f"History/metrics")
        except Exception as e:
            _warn(f"{workflow_id}: deferred detachment failed ({e}) — its rows "
                  f"are still attached to History/metrics")


# ---------------------------------------------------------------------------
# Per-run orchestration
# ---------------------------------------------------------------------------

def run_once(base_url: str, token: str | None, query_id: str, query: str,
             repeat: int, browser_log: Path | None, conn,
             pending_detach: list) -> dict:
    """Execute one benchmark run and return its complete CSV row.

    `pending_detach` collects the ids of runs whose stream died mid-flight; see
    queue_partial_detach for why those cannot be detached inline. Required, not
    defaulted: a caller that forgets it would drop those ids on the floor and
    leave the runs attached to History with nothing said about it.
    """
    fields: dict = {
        "query_id": query_id, "query": query, "repeat": repeat,
        "started_at": _now_iso(),
    }
    offset = log_offset(browser_log)

    events: list = []
    try:
        stream_generate_and_run(base_url, query, token, events)
    except requests.RequestException as e:
        _warn(f"{query_id} repeat {repeat}: request failed: {e}")
        # generation_status stays "error": the partial stream's last status
        # describes a run that then died, and the CSV must not read as a
        # completed one. Only the id is taken, and only to detach by.
        fields["generation_status"] = "error"
        fields["workflow_id"] = extract_run_identity(events)["workflow_id"]
        queue_partial_detach(fields["workflow_id"], pending_detach)
        return build_csv_row(fields)

    ident = extract_run_identity(events)
    stages = stage_durations(events)
    repairs = count_dryrun_repairs(events)
    fields.update(ident)
    fields.update({k: (round(v, 3) if v is not None else None)
                   for k, v in stages.items()})
    fields["dryrun_repairs"] = repairs

    fields.update(log_metrics(read_log_from(browser_log, offset)))

    workflow_id = ident["workflow_id"]
    if workflow_id:
        # Failed generations never leave a workflow_metrics row (the service
        # deletes its temp metrics on error) — skip the retry poll, but still
        # capture+detach: pre-failure LLM calls are already in llm_traces.
        if ident["generation_status"] == "complete":
            data = fetch_metrics_data(conn, workflow_id)
            if data is not None:
                warn_if_zero_crewai_tokens(data, query_id, repeat, workflow_id)
                metrics = extract_metrics_fields(data)
                # Guardrail number: metrics-row flakes + dryrun repair rounds.
                metrics["flake_retries"] += repairs
                fields.update(metrics)
            else:
                _warn(f"{query_id} repeat {repeat}: no workflow_metrics row for "
                      f"{workflow_id} — LLM/locator columns left empty")

        if capture_evidence(conn, workflow_id):
            detach_run(conn, workflow_id)
        warn_if_read_query_never_reads(query, query_id, repeat, workflow_id)

    return build_csv_row(fields)


def existing_header(out_path: Path) -> list[str] | None:
    """The header already in `out_path`, or None if there isn't one."""
    try:
        with open(out_path, newline="", encoding="utf-8") as f:
            return next(csv.reader(f), None)
    except FileNotFoundError:
        return None


def gate_schema(out_path: Path) -> None:
    """Refuse to append rows written against a different column layout.

    DictWriter writes by CURRENT fieldnames and only emits a header for a new
    file, so appending to an older CSV silently misaligns every value after
    the first schema difference. `agent_steps` was removed from the middle of
    CSV_COLUMNS, so appending one current row to an agent_steps-era baseline
    made dom_elements_max read the step count, llm_429_count read
    dom_elements_median, and six values fall into DictReader's restkey — no
    exception, no warning, just a plausible-looking CSV that is wrong.

    Same shape and tone as the pin gate below: a bench that cannot be trusted
    must fail loudly before it burns an hour of wall clock.
    """
    header = existing_header(out_path)
    if header is None or header == list(CSV_COLUMNS):
        return
    missing = [c for c in CSV_COLUMNS if c not in header]
    extra = [c for c in header if c not in CSV_COLUMNS]
    # ASCII only, here and below: this message is the last thing the process
    # does, and a cp1252 stderr would turn a clear refusal into a
    # UnicodeEncodeError.
    if not missing and not extra:
        # Same column names, so the report below has nothing to list and would
        # print "(none)" on both lines, naming no cause at all - it reads like
        # the gate is broken rather than like the file is. Both checks above
        # ignore order and repeats, so what is left is a reorder (or, in
        # principle, a duplicated column). zip_longest rather than zip because
        # a duplicate makes the header LONGER, and pairwise zip would stop at
        # the short list and find nothing; header != CSV_COLUMNS is already
        # established, so padding guarantees the search below finds a position.
        index, (theirs, ours) = next(
            (i, pair)
            for i, pair in enumerate(zip_longest(header, CSV_COLUMNS))
            if pair[0] != pair[1]
        )
        # `is None` rather than a falsy test, so the substitution says exactly
        # what it means: None is zip_longest's padding and nothing else.
        past_end = "(past the last column)"
        theirs = past_end if theirs is None else theirs
        ours = past_end if ours is None else ours
        sys.exit(
            f"error: {out_path} lists the same columns in a different order. "
            f"Appending would misalign every value from that point on.\n"
            f"  first difference at column {index}: it has {theirs}, "
            f"we write {ours}\n"
            f"Use a fresh --out path."
        )
    sys.exit(
        f"error: {out_path} was written with a different CSV schema. Appending "
        f"would misalign every column after the first difference.\n"
        f"  columns it lacks:  {missing or '(none)'}\n"
        f"  columns it has that we no longer write: {extra or '(none)'}\n"
        f"Use a fresh --out path."
    )


def append_row(out_path: Path, row: dict) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # A zero-byte file counts as new. It reaches here because existing_header
    # cannot tell it from a missing file, so gate_schema passes it; without
    # this the header is skipped and the first data row is read back as one.
    new_file = not out_path.exists() or out_path.stat().st_size == 0
    with open(out_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(CSV_COLUMNS))
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def gate_pins(args, out_path: Path) -> None:
    """Preflight the live stack's pins, then record them beside the CSV.

    Exits the process on an unpinned stack (unless --allow-unpinned) or on an
    append whose pins disagree with what the target CSV already holds.
    """
    nlrf_health = fetch_health(args.base_url)
    browser_health = fetch_health(args.browser_url)
    violations, warnings = preflight_violations(nlrf_health, browser_health)
    for w in warnings:
        _warn(f"preflight: {w}")
    if violations:
        for v in violations:
            _warn(f"preflight: {v}")
        if not args.allow_unpinned:
            sys.exit("[bench] preflight FAILED — fix the pins (./run.sh bench) or pass "
                     "--allow-unpinned (run will NOT be comparable to the baseline)")
        _warn("preflight violations ignored (--allow-unpinned) — this run is "
              "not comparable to the baseline")

    meta = build_meta(nlrf_health, browser_health, args.base_url, args.browser_url)
    pins_str = ", ".join(f"{k}={v}" for k, v in meta["nlrf_pins"].items()) or "unavailable"
    # The bench log is a run's evidence record. --allow-unpinned reaches here
    # with violations intact, and an "OK" line there makes a non-comparable run
    # read as comparable to whoever analyses the CSV later.
    status = "UNPINNED (violations ignored)" if violations else "OK"
    _log(f"preflight {status} — nlrf pins: {pins_str}")
    _log(f"browser-service: model_provider={meta['browser_service']['model_provider']} "
         f"headless={meta['browser_service']['headless']}")

    # --out APPENDS. One sidecar cannot honestly describe rows recorded under
    # two different pin sets, so a conflicting append is refused outright.
    conflicts = append_pin_conflict(out_path, meta)
    if conflicts:
        for c in conflicts:
            _warn(f"append guard: {c}")
        sys.exit(f"[bench] {out_path} already holds rows recorded under different "
                 f"pins, and --out APPENDS — use a fresh --out path so its "
                 f"sidecar describes only its own rows")

    meta_file = meta_path_for(out_path)
    meta_file.parent.mkdir(parents=True, exist_ok=True)
    if meta_file.exists():
        # Pins already verified identical above — keep the original sidecar so
        # captured_at still marks when this CSV's first row was recorded.
        _log(f"appending to {out_path} — pins match existing {meta_file}")
        return
    if out_path.exists():
        _warn(f"{out_path} exists but has no pins sidecar — its earlier rows "
              f"cannot be verified as comparable to this run")
        # Recording pins now would vouch for those unverifiable rows on the
        # NEXT append — leave the CSV sidecar-less and keep warning instead.
        return
    with open(meta_file, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    _log(f"pins recorded -> {meta_file}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="NL-to-RF benchmark runner (sequential; detaches all bench "
                    "data from History/metrics after each run)")
    parser.add_argument("--queries", default=str(REPO_ROOT / "bench" / "queries.json"))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--out",
        default=str(REPO_ROOT / "bench" / "baselines"
                    / f"{datetime.now():%Y-%m-%d}-baseline.csv"))
    parser.add_argument("--base-url",
                        default=os.environ.get("NLRF_BASE_URL", "http://127.0.0.1:5000"))
    parser.add_argument("--browser-log",
                        default=os.environ.get("BROWSER_SERVICE_LOG", ""))
    parser.add_argument("--browser-url",
                        default=os.environ.get("BROWSER_SERVICE_URL",
                                               "http://127.0.0.1:4999"))
    parser.add_argument("--allow-unpinned", action="store_true",
                        help="downgrade preflight pin violations to warnings "
                             "(exploration runs — never comparable to the baseline)")
    args = parser.parse_args()

    token = os.environ.get("BENCH_TOKEN", "") or None
    browser_log = Path(args.browser_log) if args.browser_log else None
    if browser_log is None:
        _warn("BROWSER_SERVICE_LOG not set — cold-start/cleanup/locator-latency/"
              "probe columns will be empty")
    elif not browser_log.exists():
        _warn(f"browser log {browser_log} does not exist yet — log metrics "
              f"will be empty until the service writes it")

    with open(args.queries, encoding="utf-8") as f:
        queries = json.load(f)["queries"]
    out_path = Path(args.out)

    _log(f"target={args.base_url}  queries={len(queries)}  repeats={args.repeats}")

    gate_schema(out_path)
    gate_pins(args, out_path)

    # autocommit: each DELETE/SELECT stands alone; a failed capture must not
    # hold a transaction open across the next run.
    with psycopg.connect(settings.DATABASE_URL, autocommit=True,
                         row_factory=dict_row) as conn:
        total = len(queries) * args.repeats
        done = 0
        # Runs whose stream died mid-flight. Detached after the loop, once the
        # server has certainly finished with them — see queue_partial_detach.
        pending_detach: list[str] = []
        try:
            for spec in queries:
                for repeat in range(1, args.repeats + 1):
                    done += 1
                    _log(f"({done}/{total}) {spec['id']} repeat {repeat}: "
                         f"{spec['query'][:60]}...")
                    row = run_once(args.base_url, token, spec["id"], spec["query"],
                                   repeat, browser_log, conn, pending_detach)
                    append_row(out_path, row)
                    _log(f"({done}/{total}) {spec['id']} repeat {repeat}: "
                         f"gen={row['generation_status']} test={row['test_status']} "
                         f"total_s={row['total_s']}")
        finally:
            # Also on Ctrl-C or an unexpected raise: an abandoned sweep must not
            # leave the runs it already queued attached to History/metrics.
            if pending_detach:
                _log(f"detaching {len(pending_detach)} run(s) whose stream failed")
                drain_deferred_detach(conn, pending_detach)
    _log(f"done — {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

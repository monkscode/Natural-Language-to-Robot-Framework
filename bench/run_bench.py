"""Benchmark runner: POST each frozen query to /generate-and-run, record one CSV row per run.

Usage (from the repo root, against a fully running dev stack):

    python -m bench.run_bench --out bench/baselines/2026-07-03-baseline.csv

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
"""

import argparse
import csv
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import psycopg
import requests
from psycopg.rows import dict_row

from bench.bench_lib import (
    CSV_COLUMNS,
    append_pin_conflict,
    build_csv_row,
    build_meta,
    count_dryrun_repairs,
    duplicate_lookup_rate,
    extract_metrics_fields,
    extract_run_identity,
    median_p90,
    meta_path_for,
    parse_locator_timers,
    preflight_violations,
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

def stream_generate_and_run(base_url: str, query: str, token: str | None):
    """POST the query, return [(client_monotonic_time, event_dict), ...]."""
    headers = {"Accept": "text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    events = []
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


# ---------------------------------------------------------------------------
# Per-run orchestration
# ---------------------------------------------------------------------------

def run_once(base_url: str, token: str | None, query_id: str, query: str,
             repeat: int, browser_log: Path | None, conn) -> dict:
    """Execute one benchmark run and return its complete CSV row."""
    fields: dict = {
        "query_id": query_id, "query": query, "repeat": repeat,
        "started_at": _now_iso(),
    }
    offset = log_offset(browser_log)

    try:
        events = stream_generate_and_run(base_url, query, token)
    except requests.RequestException as e:
        _warn(f"{query_id} repeat {repeat}: request failed: {e}")
        fields["generation_status"] = "error"
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
        data = fetch_metrics_data(conn, workflow_id)
        if data is not None:
            metrics = extract_metrics_fields(data)
            # Guardrail number: metrics-row flakes + dryrun repair rounds.
            metrics["flake_retries"] += repairs
            fields.update(metrics)
        else:
            _warn(f"{query_id} repeat {repeat}: no workflow_metrics row for "
                  f"{workflow_id} — LLM/locator columns left empty")

        if capture_evidence(conn, workflow_id):
            detach_run(conn, workflow_id)

    return build_csv_row(fields)


def append_row(out_path: Path, row: dict) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not out_path.exists()
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
    _log(f"preflight OK — nlrf pins: {pins_str}")
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

    gate_pins(args, out_path)

    # autocommit: each DELETE/SELECT stands alone; a failed capture must not
    # hold a transaction open across the next run.
    with psycopg.connect(settings.DATABASE_URL, autocommit=True,
                         row_factory=dict_row) as conn:
        total = len(queries) * args.repeats
        done = 0
        for spec in queries:
            for repeat in range(1, args.repeats + 1):
                done += 1
                _log(f"({done}/{total}) {spec['id']} repeat {repeat}: "
                     f"{spec['query'][:60]}...")
                row = run_once(args.base_url, token, spec["id"], spec["query"],
                               repeat, browser_log, conn)
                append_row(out_path, row)
                _log(f"({done}/{total}) {spec['id']} repeat {repeat}: "
                     f"gen={row['generation_status']} test={row['test_status']} "
                     f"total_s={row['total_s']}")
    _log(f"done — {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

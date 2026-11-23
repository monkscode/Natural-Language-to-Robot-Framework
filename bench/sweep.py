"""Held-out sweep runner (NLRF #179): generate each query, re-run the generated
test with a judge line, score, and append one JSON line per run.

It drives an isolated copy stack (default http://127.0.0.1:5002, learning ON,
over a copied database) and refuses production. Nothing is detached or deleted:
evidence goes to <out_dir>/runs/<wf>/ (the judge run's to .../judge/).

    python -m bench.sweep --queries <file> --out-dir <dir> [--repeats 3]
        [--base-url http://127.0.0.1:5002] [--only q1,q2]

The judge re-run happens only for `expect pass` queries whose generated test
generated AND passed: a failed test scores test_failed whatever the judge says,
so judging it would cost an execution for nothing. For judge_failed / judge_error
rows the tag comes from the judge run's own error text (judge_error_message);
every other non-handled row is tagged from the generation run's.

`--out-dir` must sit under bench/private/nlrf179/ (gitignored; runs/<wf>/*.json hold the query text); nothing checks it.
A dead generation stream captures no evidence; the server still finishes that run in the copy, so its rows live only in
the copy DB until down.sh drops it.

Environment: SWEEP_TOKEN (optional Bearer token), SWEEP_DATABASE_URL (required;
never settings.DATABASE_URL).

Referenced by: operator command line, tests/test_bench/test_sweep_runner.py
Depends on: bench/run_bench.py, bench/bench_lib.py, bench/sweep_lib.py,
    bench/sweep_judge.py, src/backend/core/pass_quality.py
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import requests
from psycopg.rows import dict_row

from bench.bench_lib import extract_run_identity
from bench.run_bench import (
    CONNECT_TIMEOUT_S,
    READ_TIMEOUT_S,
    _log,
    _warn,
    capture_evidence,
    fetch_health,
    stream_generate_and_run,
)
from bench.sweep_judge import JudgeRefused, insert_judge
from bench.sweep_lib import SweepQuery, failure_tag, load_sweep_queries, score_run
from src.backend.core.pass_quality import hollow_shapes

DEFAULT_BASE_URL = "http://127.0.0.1:5002"
PRODUCTION_HOSTS = ("127.0.0.1", "localhost")
PRODUCTION_PORT = 5000


def stream_execute(base_url: str, robot_code: str, token: str | None,
                   sink: list | None = None) -> list:
    """POST robot_code to /execute-test, return [(client_monotonic_time, event), ...].

    Same SSE parsing and timeouts as stream_generate_and_run; `sink` is filled in
    place so a caller can read the partial stream after a raise.
    """
    headers = {"Accept": "text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    events = [] if sink is None else sink
    with requests.post(
        f"{base_url}/execute-test",
        json={"robot_code": robot_code},
        headers=headers,
        stream=True,
        timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            t = time.monotonic()
            try:
                ev = json.loads(line[len("data: "):])
            except json.JSONDecodeError:
                continue
            events.append((t, ev))
    return events


def judge_identity(events: list) -> dict:
    """The judge run's id (first execution event carrying one) and its status."""
    ident = {"run_id": None, "judge_status": None}
    for _, ev in events:
        if ev.get("stage") != "execution":
            continue
        if ident["run_id"] is None and ev.get("run_id"):
            ident["run_id"] = ev["run_id"]
        if ev.get("status") == "error":
            ident["judge_status"] = "error"
        elif "test_status" in ev:
            ident["judge_status"] = ev["test_status"]
    return ident


def check_target(base_url: str, health: dict) -> list[str]:
    """Reasons to refuse this target; empty when the sweep may start.

    The real guards are not this list: production refuses the probe token (its user exists only in the copy, and
    the copy stack's up.sh gives that fastapi its own JWT secret), and run_one stops on a finished run missing from
    the copy DB.
    """
    reasons = []
    parsed = urlparse(base_url)
    if parsed.hostname in PRODUCTION_HOSTS and parsed.port == PRODUCTION_PORT:
        reasons.append(f"{base_url} is the production port ({PRODUCTION_PORT})")
    if (health.get("pins") or {}).get("optimization_enabled") is not True:
        reasons.append("learning is not ON (pins.optimization_enabled is not True)")
    return reasons


def _captured_row(dest: Path) -> dict:
    """First test_runs row captured under `dest`, or {} when none is readable."""
    try:
        rows = json.loads((dest / "test_runs.json").read_text(encoding="utf-8"))
        return rows[0] if rows else {}
    except (OSError, ValueError, IndexError):
        return {}


def _label(q: SweepQuery, repeat: int, pos: str = "") -> str:
    """How the console names a run: a dev query by id + repeat, a sealed one by position only."""
    return f"{pos}{q.id} repeat {repeat}" if q.half == "dev" else f"{pos}sealed"


def _judge(q: SweepQuery, robot_code: str | None, base_url: str, token: str | None,
           conn, out_dir: Path, wf: str, label: str) -> dict:
    """Re-run the generated test with the judge line; returns the judge_* fields."""
    out = {"judge_run_id": None, "judge_status": None, "judge_refused": None,
           "judge_error_message": None}
    if not robot_code:
        out["judge_refused"] = "no robot_code captured"
        return out
    try:
        code = insert_judge(robot_code, q.judge)
    except JudgeRefused as e:
        out["judge_refused"] = str(e)
        return out
    events: list = []
    try:
        stream_execute(base_url, code, token, events)
    except requests.RequestException as e:
        _warn(f"{label}: judge request failed: {e}")
        out["judge_status"] = "error"
    ident = judge_identity(events)
    out["judge_run_id"] = ident["run_id"]
    out["judge_status"] = out["judge_status"] or ident["judge_status"]
    if ident["run_id"]:
        dest = out_dir / "runs" / wf / "judge"
        if capture_evidence(conn, ident["run_id"], dest):
            out["judge_error_message"] = _captured_row(dest).get("error_message")
    return out


def run_one(q: SweepQuery, repeat: int, base_url: str, token: str | None,
            conn, out_dir: Path, pos: str = "") -> dict:
    label = _label(q, repeat, pos)
    row = {"query_id": q.id, "half": q.half, "source": q.source, "expect": q.expect,
           "repeat": repeat, "workflow_id": None, "generation_status": None,
           "test_status": None, "judge_run_id": None, "judge_status": None,
           "judge_refused": None, "error_message": None,
           "judge_error_message": None, "outcome": None,
           "tag": None, "hollow": [], "started_at": datetime.now().isoformat(
               timespec="seconds"), "total_s": None}
    events: list = []
    t0 = time.monotonic()
    died = False
    try:
        stream_generate_and_run(base_url, q.query, token, events)
    except requests.RequestException as e:
        _warn(f"{label}: request failed: {e}")
        died = True
    row["total_s"] = round(time.monotonic() - t0, 3)
    ident = extract_run_identity(events)
    wf = row["workflow_id"] = ident["workflow_id"]
    # A dead stream is not a completed run, whatever its partial events said.
    row["generation_status"] = "error" if died else ident["generation_status"]
    row["test_status"] = ident["test_status"]

    robot_code = None
    if wf and not died:
        dest = out_dir / "runs" / wf
        if capture_evidence(conn, wf, dest):
            captured = _captured_row(dest)
            robot_code = captured.get("robot_code") or None
            row["error_message"] = captured.get("error_message")
            if not captured and row["generation_status"] == "complete":
                # The opening row is written before the stream starts: a correct copy always has it.
                raise SystemExit(
                    f"[bench] workflow {wf} is not in SWEEP_DATABASE_URL - the target "
                    f"writes to another database; stopping")
        if q.expect == "pass" and row["generation_status"] == "complete" \
                and row["test_status"] == "passed":
            row.update(_judge(q, robot_code, base_url, token, conn, out_dir, wf, label))
        if row["generation_status"] == "complete" and row["test_status"] == "passed" \
                and robot_code:
            try:
                row["hollow"] = sorted(hollow_shapes(robot_code, q.query))
            except Exception as e:  # noqa: BLE001 - as production: a checker bug must not end a paid sweep
                _warn(f"{label}: hollow checker raised {type(e).__name__}")
                row["hollow"] = ["CHECKER_ERROR"]

    row["outcome"] = score_run(q, row["generation_status"], row["test_status"],
                               row["judge_status"], row["error_message"])
    if row["outcome"] in ("judge_failed", "judge_error"):
        # The generated test passed; what failed is the judge run, so its text tags.
        row["tag"] = failure_tag(row["judge_error_message"])
    elif row["outcome"] != "handled":
        row["tag"] = failure_tag(row["error_message"])
    return row


def run_sweep(queries: list[SweepQuery], repeats: int, base_url: str,
              token: str | None, conn, out_dir: Path) -> None:
    """File order, repeats inside each query; one flushed JSON line per run."""
    out_dir.mkdir(parents=True, exist_ok=True)
    results = out_dir / "results.jsonl"
    if results.exists() and results.stat().st_size > 0:  # appended rows would mix two sweeps in one report
        raise SystemExit(f"refusing to start: {results} already holds rows; use a fresh --out-dir")
    total = len(queries) * repeats
    done = 0
    with open(results, "a", encoding="utf-8") as f:
        for q in queries:
            for repeat in range(1, repeats + 1):
                done += 1
                dev = q.half == "dev"  # a sealed query's id, text and outcome stay off the console
                pos = f"({done}/{total}) "
                label = _label(q, repeat, pos)
                _log(f"{label}: {q.query[:60]}..." if dev else label)
                row = run_one(q, repeat, base_url, token, conn, out_dir, pos)
                f.write(json.dumps(row) + "\n")
                f.flush()
                if dev:
                    _log(f"{label}: {row['outcome']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NLRF #179 held-out sweep runner")
    parser.add_argument("--queries", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--only", default="", help="comma-separated query ids")
    args = parser.parse_args(argv)

    db_url = os.environ.get("SWEEP_DATABASE_URL")
    if not db_url:
        _warn("SWEEP_DATABASE_URL is required (the copy stack's database)")
        return 2
    queries = load_sweep_queries(Path(args.queries))
    if args.only:
        wanted = [s.strip() for s in args.only.split(",") if s.strip()]
        unknown = sorted(set(wanted) - {q.id for q in queries})
        if unknown:
            _warn(f"--only names unknown query ids: {unknown}")
            return 2
        queries = [q for q in queries if q.id in wanted]

    reasons = check_target(args.base_url, fetch_health(args.base_url))
    if reasons:
        for r in reasons:
            _warn(f"refusing to sweep: {r}")
        return 2

    token = os.environ.get("SWEEP_TOKEN", "") or None
    _log(f"target={args.base_url}  queries={len(queries)}  repeats={args.repeats}")
    with psycopg.connect(db_url, autocommit=True, row_factory=dict_row) as conn:
        run_sweep(queries, args.repeats, args.base_url, token, conn,
                  Path(args.out_dir))
    _log(f"done — {Path(args.out_dir) / 'results.jsonl'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

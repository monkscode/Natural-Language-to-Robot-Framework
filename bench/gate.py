"""The bench gate: one tracked verdict for a 30-query bench CSV.

Usage (repo root; reads bench/baselines, bench/runs and logs/ — no database, no network):

    python -m bench.gate <candidate.csv> [--rerun <r1.csv> --rerun <r2.csv> ...]

It compares the candidate with the ONE baseline, bench/gate_inputs.BASELINE_CSV
(2026-10-01-develop-fda923f-bs-1.0.39-gemini-3.8-flash-run2). It is also the
TIMING reference; timing is not gated here. It prints one line per gate —
PINS/INPUTS, PASS RATE (after provider-miss re-runs), LOCATOR, FLAKE, SALVAGE, the
four TOKENS families, HOLLOW — then the reported lines (VERIFIED, FRAGILE, NOT
CHECKED, COST, 429 WINDOW) and a VERDICT. Exit code: 0 every gate passes; 1 any
gate fails; 2 cannot compare (refused, a provider miss awaits a re-run, or a gate
had nothing to measure).

When provider misses await a re-run it writes one one-query --queries file per
query under bench/private/gate-reruns/<candidate stem>/ (or --rerun-dir) and
prints the bench/run_bench.py command for each; pass each resulting CSV back with
its own --rerun flag, in the order they were run.

It replaces the private copies bench/private/e2_gate_check.py and
bench/private/wrongel/fix/session4/bench/gates.py (kept as evidence, no longer
run). It lives outside bench/run_bench.py on purpose: that file carries two conflict
hunks with the crewai-upgrade branch, and the gate must not import it (it
imports config, psycopg and requests).

Referenced by: operators, after every bench (CLAUDE.md "Bench discipline", the
bench skill, bench/README.md "Gating a bench").
Depends on: bench/gate_inputs.py, bench/gate_checks.py, bench/gate_hollow.py.
"""

import argparse
import sys
import traceback
from datetime import tzinfo
from pathlib import Path

from bench.gate_checks import (
    flake_line,
    locator_line,
    reported_cost_lines,
    salvage_line,
    token_lines,
    window_line,
)
from bench.gate_hollow import hollow_lines
from bench.gate_inputs import (
    BASELINE_CSV,
    PROVIDER_MISS,
    TIMING_REFERENCE_CSV,
    GateLine,
    GateRefused,
    build_slots,
    check_captures,
    check_pins,
    check_same_code,
    check_same_queries,
    load_runs,
    pass_rate_line,
    write_rerun_files,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def exit_code(lines: list[GateLine]) -> int:
    statuses = {line.status for line in lines}
    if "REFUSED" in statuses:
        return 2
    if "FAIL" in statuses:
        return 1
    if statuses & {"PENDING", "CANNOT"}:
        return 2
    return 0


def run_gate(candidate_csv: Path, rerun_csvs: list[Path] | None = None, *,
             baseline_csv: Path | None = None, runs_dir: Path | None = None,
             logs_dir: Path | None = None, rerun_dir: Path | None = None,
             local_tz: tzinfo | None = None) -> list[GateLine]:
    """Every gate line for one candidate CSV (plus its re-run CSVs).

    Paths default to this checkout (resolved at call time from REPO_ROOT); local_tz
    None means the host's zone, which is the zone run_bench.py wrote started_at in.
    """
    rerun_csvs = list(rerun_csvs or [])
    baseline_csv = baseline_csv or REPO_ROOT / BASELINE_CSV
    runs_dir = runs_dir or REPO_ROOT / "bench" / "runs"
    logs_dir = logs_dir or REPO_ROOT / "logs"
    try:
        base_runs = load_runs(baseline_csv, runs_dir)
        cand_runs = load_runs(candidate_csv, runs_dir)
        check_pins(baseline_csv, candidate_csv)
        rerun_runs = []
        for path in rerun_csvs:
            check_pins(baseline_csv, path)
            check_same_code(candidate_csv, path)
            rerun_runs += load_runs(path, runs_dir)
        check_same_queries([r.row for r in base_runs], [r.row for r in cand_runs], candidate_csv)
        check_captures(base_runs)
        check_captures(cand_runs + rerun_runs)
        slots = build_slots(cand_runs, rerun_runs)
    except GateRefused as exc:
        return [GateLine("REFUSED", "REFUSED", str(exc))]
    lines = [GateLine("PINS/INPUTS", "PASS", f"same pins and queries as the baseline; "
                      f"{len(cand_runs)} rows, {len(rerun_runs)} re-run rows, every capture present")]
    rate = pass_rate_line(slots)
    if rate.status == "PENDING":
        target = rerun_dir or REPO_ROOT / "bench" / "private" / "gate-reruns" / candidate_csv.stem
        rate.details += [f"run: {c}" for c in write_rerun_files(slots, target, candidate_csv, len(rerun_csvs))]
        rate.details.append("then: python -m bench.gate <candidate.csv> --rerun <r1.csv> --rerun <r2.csv> ... "
                            "(one --rerun per re-run CSV, in the order run)")
    lines.append(rate)
    lines += [locator_line(slots), flake_line(slots), salvage_line(slots)]
    lines += token_lines(base_runs, slots)
    if rate.status == "PENDING":
        # Tokens are the one gate a re-run can flip from FAIL to PASS: a token FAIL waits with the pass rate.
        misses = sum(s.outcome == PROVIDER_MISS for s in slots)
        for line in lines:
            if line.name.startswith("TOKENS ") and line.status == "FAIL":
                line.status = "PENDING"
                line.details.append(f"provisional: {misses} provider miss(es) await a re-run")
    lines += hollow_lines(slots)
    lines += reported_cost_lines(base_runs, slots)
    # The bench's own window: re-runs come later and would stretch it over idle time.
    lines.append(window_line([s.history[0] for s in slots], logs_dir, local_tz))
    return lines


def format_report(title: str, lines: list[GateLine]) -> str:
    code = exit_code(lines)
    out = [title]
    for line in lines:
        out.append(f"{line.name:<30} {line.status:<8} {line.text}")
        out += [f"{'':<39}- {d}" for d in line.details]
    verdict = {0: "PASS", 1: "FAIL", 2: "CANNOT COMPARE"}[code]
    out.append(f"{'VERDICT':<30} {verdict} (exit {code})")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    # A piped run on Windows writes cp1252: page text echoed from an error_message
    # (e.g. "日本語") would crash with exit 1, which reads as "a gate failed".
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Gate one bench CSV against the baseline "
                                                 f"({BASELINE_CSV}).")
    parser.add_argument("candidate", type=Path, help="the bench CSV to gate")
    parser.add_argument("--rerun", type=Path, action="append", default=[],
                        help="a provider-miss re-run CSV (repeatable, in the order run)")
    parser.add_argument("--runs-dir", type=Path, default=None, help="default: bench/runs")
    parser.add_argument("--logs-dir", type=Path, default=None, help="default: logs")
    parser.add_argument("--rerun-dir", type=Path, default=None,
                        help="default: bench/private/gate-reruns/<candidate stem>")
    args = parser.parse_args(argv)
    title = (f"bench gate — candidate {args.candidate}\n"
             f"baseline {BASELINE_CSV} (timing reference {TIMING_REFERENCE_CSV}; timing is not gated)")
    # A crash is never a gate verdict: exit 1 would read as "a gate failed", so it exits 2.
    try:
        lines = run_gate(args.candidate, args.rerun, runs_dir=args.runs_dir,
                         logs_dir=args.logs_dir, rerun_dir=args.rerun_dir)
    except Exception as exc:   # argparse's SystemExit and KeyboardInterrupt are not Exception
        traceback.print_exc()
        lines = [GateLine("REFUSED", "REFUSED", f"the gate crashed: {type(exc).__name__}: {exc} "
                                                f"(traceback on stderr) — not a gate verdict")]
    print(format_report(title, lines))
    return exit_code(lines)


if __name__ == "__main__":
    sys.exit(main())

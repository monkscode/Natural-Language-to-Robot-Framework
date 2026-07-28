"""Benchmark report: median + p90 per metric, plus baseline-vs-candidate compare.

Usage (from the repo root):

    python -m bench.report bench/baselines/2026-07-03-baseline.csv
    python -m bench.report bench/baselines/2026-07-03-baseline.csv candidate.csv
    python -m bench.report <csv> --by-query        # per-query breakdown

Guardrail metrics (must never regress vs the frozen baseline, per plan
00-INDEX.md): locator_success_rate, pass rate (test_status == 'passed'),
flake_retries.
"""

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

from bench.bench_lib import _coerce, compare_pins, compare_summaries, load_meta, step_budget_exhausted, summarize_rows

NUMERIC_METRICS = (
    "plan_s", "identify_s", "assemble_s", "dryrun_s", "exec_s", "total_s",
    "llm_calls", "llm_tokens", "prompt_tokens", "completion_tokens", "llm_cost_usd",
    "total_elements", "successful_elements", "failed_elements", "locator_success_rate",
    "flake_retries", "dryrun_repairs",
    "cold_start_s", "cleanup_s",
    "locator_timer_count", "locator_latency_ms_median", "locator_latency_ms_p90",
    "probe_total", "probe_unique", "duplicate_lookup_rate",
    # identify_s phase breakdown (2026-07-26 efficiency check)
    "submit_s", "queue_s", "session_setup_s", "agent_setup_s", "agent_run_s",
    "postprocess_s", "poll_wait_s",
    "dom_elements_max", "dom_elements_median",
    "llm_429_count", "retry_lost_s",
    # agent_run_s split (2026-07-26)
    "llm_total_s", "llm_max_s", "llm_calls_actual", "steps_total_s",
    "llm_coverage_gap", "browser_use_llm_calls",
)

GUARDRAILS = ("locator_success_rate", "flake_retries")


def load_rows(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit(f"error: {path} contains no data rows")
    return rows


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.3f}".rstrip("0").rstrip(".") or "0"
    return str(v)


def pass_rate(rows: list[dict]) -> float:
    n = len(rows)
    passed = sum(1 for r in rows if r.get("test_status") == "passed")
    return round(passed / n * 100, 1) if n else 0.0


def measured_rows(rows: list[dict], column: str) -> list[dict]:
    """Rows where `column` was actually recorded.

    A missing key (a baseline predating the column) and an empty cell (a run
    that could not be scored) are the same thing, and neither may count as a
    zero.
    """
    return [r for r in rows if (r.get(column) or "") != ""]


def _coerce_int(s: str | None) -> int | None:
    """Safely coerce a CSV string to int or None.

    Empty string, missing value, or non-numeric are treated the same — None.
    """
    if not s:
        return None
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


def exhaustion_counts(rows: list[dict]) -> tuple[int, int]:
    """(runs that consumed their whole step budget, runs that could be scored).

    A row is counted if:
    - It has a stored step_budget_exhausted value (stored always wins), or
    - It can be derived from total_elements and browser_use_llm_calls

    If derivation is not possible (missing or non-numeric inputs), the row
    is unmeasurable and does not count.
    """
    hits = 0
    measured = 0

    for r in rows:
        # Stored value always wins when present and non-empty
        stored = (r.get("step_budget_exhausted") or "").strip()
        if stored:
            measured += 1
            if stored == "1":
                hits += 1
            continue

        # Try to derive from total_elements and browser_use_llm_calls
        total_elements = _coerce_int(r.get("total_elements"))
        browser_use_calls = _coerce_int(r.get("browser_use_llm_calls"))

        # Use the existing function to derive; it returns 1, 0, or None
        derived = step_budget_exhausted(total_elements, browser_use_calls)

        if derived is not None:
            measured += 1
            if derived == 1:
                hits += 1

    return hits, measured


def miss_counts(rows: list[dict]) -> tuple[int, int]:
    """(runs finishing with an unresolved element, runs that could be scored).

    Read beside exhaustion_counts and never without it: a change that makes the
    agent give up early instead of looping drives exhaustion to zero while this
    number stays put. A count, not a median — the median reads 0.0 on a set
    where a fifth of the runs lost elements.

    A non-numeric or empty failed_elements cell is unmeasurable, exactly like
    exhaustion_counts — a malformed cell must not crash a report.

    This figure undercounts on browser-service builds retired before
    2026-07-25 (successful + failed != total on 47 of 913 captured rows, every
    one reading failed_elements = 0 while elements were genuinely missed) — it
    is only trustworthy for runs captured on or after that date.
    """
    hits = 0
    measured = 0
    for r in measured_rows(rows, "failed_elements"):
        val = _coerce(r["failed_elements"])
        if val is None:
            continue
        measured += 1
        if val > 0:
            hits += 1
    return hits, measured


def rate_line(label: str, hits: int, measured: int, total: int) -> str:
    if not measured:
        return f"   {label:<24} 0/0 measured ({total} rows unmeasurable)"
    pct = hits / measured * 100
    tail = f"  [{total - measured} rows unmeasurable]" if measured < total else ""
    return f"   {label:<24} {hits}/{measured} measured ({pct:.1f}%){tail}"


def compare_rate_line(label: str, base: tuple[int, int], cand: tuple[int, int]) -> str:
    b_hits, b_n = base
    c_hits, c_n = cand
    if not b_n or not c_n:
        side = "baseline" if not b_n else "candidate"
        return f"   {label:<24} not comparable — {side} has no measured rows"
    return (f"   {label:<24} {b_hits}/{b_n} ({b_hits / b_n * 100:.1f}%) → "
            f"{c_hits}/{c_n} ({c_hits / c_n * 100:.1f}%)")


def print_summary(rows: list[dict], title: str) -> None:
    print(f"\n== {title} ({len(rows)} runs, pass rate {pass_rate(rows)}%) ==")
    print(rate_line("budget exhausted", *exhaustion_counts(rows), len(rows)))
    print(rate_line("runs w/ unresolved elems", *miss_counts(rows), len(rows)))
    s = summarize_rows(rows, NUMERIC_METRICS)
    print(f"{'metric':<28} {'n':>4} {'median':>12} {'p90':>12}")
    for metric in NUMERIC_METRICS:
        m = s[metric]
        print(f"{metric:<28} {m['n']:>4} {_fmt(m['median']):>12} {_fmt(m['p90']):>12}")


def print_by_query(rows: list[dict]) -> None:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r.get("query_id", "?")].append(r)
    for query_id in sorted(groups):
        print_summary(groups[query_id], f"query {query_id}")


def print_compare(base_rows: list[dict], cand_rows: list[dict]) -> None:
    base = summarize_rows(base_rows, NUMERIC_METRICS)
    cand = summarize_rows(cand_rows, NUMERIC_METRICS)
    c = compare_summaries(base, cand)
    print(f"\n== baseline vs candidate (medians; pass rate "
          f"{pass_rate(base_rows)}% → {pass_rate(cand_rows)}%) ==")
    print(compare_rate_line("budget exhausted",
                            exhaustion_counts(base_rows), exhaustion_counts(cand_rows)))
    print(compare_rate_line("runs w/ unresolved elems",
                            miss_counts(base_rows), miss_counts(cand_rows)))
    print(f"{'metric':<28} {'baseline':>12} {'candidate':>12} {'delta':>12} {'pct':>8}")
    for metric in NUMERIC_METRICS:
        d = c[metric]
        pct = f"{d['median_pct']:+.1f}%" if d["median_pct"] is not None else "-"
        print(f"{metric:<28} {_fmt(d['baseline_median']):>12} "
              f"{_fmt(d['candidate_median']):>12} "
              f"{_fmt(d['median_delta']):>12} {pct:>8}")
    print("\nguardrails (must hold or improve): "
          + ", ".join(GUARDRAILS) + ", pass rate")


def print_pin_check(base_csv: str, cand_csv: str) -> None:
    """Warn loudly when the two runs' recorded pins differ (or can't be read)."""
    base_meta, cand_meta = load_meta(base_csv), load_meta(cand_csv)
    if base_meta is None or cand_meta is None:
        missing = [p for p, m in ((base_csv, base_meta), (cand_csv, cand_meta)) if m is None]
        print(f"\nnote: no pins metadata ({', '.join(missing)}) — "
              f"comparability not verified")
        return
    mismatches = compare_pins(base_meta, cand_meta)
    if mismatches:
        print("\n!! PIN MISMATCH — these runs are NOT comparable !!")
        for m in mismatches:
            print(f"   {m}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark report / compare")
    parser.add_argument("csv", nargs="+",
                        help="one CSV → summary; two CSVs → baseline vs candidate")
    parser.add_argument("--by-query", action="store_true",
                        help="per-query breakdown (single-CSV mode)")
    args = parser.parse_args()

    if len(args.csv) > 2:
        parser.error("pass one CSV (summary) or two CSVs (compare)")

    rows = load_rows(args.csv[0])
    if len(args.csv) == 1:
        print_summary(rows, Path(args.csv[0]).name)
        if args.by_query:
            print_by_query(rows)
    else:
        print_pin_check(args.csv[0], args.csv[1])
        print_compare(rows, load_rows(args.csv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

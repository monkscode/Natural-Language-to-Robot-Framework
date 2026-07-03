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

from bench.bench_lib import compare_pins, compare_summaries, load_meta, summarize_rows

NUMERIC_METRICS = (
    "plan_s", "identify_s", "assemble_s", "dryrun_s", "exec_s", "total_s",
    "llm_calls", "llm_tokens", "prompt_tokens", "completion_tokens", "llm_cost_usd",
    "total_elements", "successful_elements", "failed_elements", "locator_success_rate",
    "flake_retries", "dryrun_repairs",
    "cold_start_s", "cleanup_s",
    "locator_timer_count", "locator_latency_ms_median", "locator_latency_ms_p90",
    "probe_total", "probe_unique", "duplicate_lookup_rate",
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


def print_summary(rows: list[dict], title: str) -> None:
    print(f"\n== {title} ({len(rows)} runs, pass rate {pass_rate(rows)}%) ==")
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

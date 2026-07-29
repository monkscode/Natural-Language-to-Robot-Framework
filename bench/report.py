"""Benchmark report: median + p90 per metric, plus baseline-vs-candidate compare.

Usage (from the repo root):

    python -m bench.report bench/baselines/2026-07-03-baseline.csv
    python -m bench.report bench/baselines/2026-07-03-baseline.csv candidate.csv
    python -m bench.report <csv> --by-query        # per-query breakdown
    python -m bench.report <base> <cand> --by-query   # per-query comparison

Single-CSV mode summarises one run with median + p90 — descriptive of that run
and safe. Compare mode does NOT use the pooled median: on this bench it reads
the boundary between token-size clusters and flips on noise. It reports totals
beside a paired per-query reading instead; see compare_by_query in bench_lib.

Guardrail metrics (must never regress vs the frozen baseline, per plan
00-INDEX.md): locator_success_rate, pass rate (test_status == 'passed'),
flake_retries.
"""

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

from bench.bench_lib import (
    coerce,
    compare_by_query,
    compare_pins,
    compare_summaries,
    load_meta,
    query_ids,
    step_budget_exhausted,
    summarize_rows,
)

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

# Metrics that get the full per-query breakdown under --by-query. The compact
# table already carries every metric; these three are the ones a bench decision
# actually turns on, and 43 metrics x 10 queries is 430 lines nobody reads.
HEADLINE_METRICS = ("llm_tokens", "llm_cost_usd", "total_s")


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


def exhaustion_counts(rows: list[dict]) -> tuple[int, int]:
    """(runs that consumed their whole step budget, runs that could be scored).

    A stored step_budget_exhausted value wins; otherwise the flag is derived
    from total_elements and browser_use_llm_calls, so baselines predating the
    column are still scored. A row whose inputs are missing or non-numeric is
    unmeasurable and counts toward neither total.

    Every cell goes through coerce(), including the stored one. Scoring the
    stored cell by string equality treated "1.0" — what a spreadsheet or a
    pandas round-trip leaves behind — as measured-and-clean, silently turning
    a real hit into a green reading while the derive path six lines below
    rejected the same garbage.
    """
    hits = 0
    measured = 0

    for r in rows:
        stored = coerce(r.get("step_budget_exhausted"))
        if stored is not None:
            measured += 1
            if stored == 1:
                hits += 1
            continue

        derived = step_budget_exhausted(
            coerce(r.get("total_elements")), coerce(r.get("browser_use_llm_calls"))
        )
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

    Scored as successful_elements < total_elements, NOT from failed_elements.
    Browser-service builds retired before 2026-07-25 write failed_elements = 0
    while genuinely missing elements (successful + failed != total on 47 of 913
    captured rows), so the failed_elements reading printed 0/18 on
    astpp-2026-07-18-collapse-gate.csv where 9 of the 18 runs really did lose
    an element — and in compare mode it turned a 28-point improvement into a
    displayed 22-point regression. The two columns agree on every row captured
    after that date and both exist in every baseline on disk, so this reading
    is strictly better everywhere.

    A row missing either column, or holding a non-numeric cell, is
    unmeasurable — a malformed cell must not crash a report.
    """
    hits = 0
    measured = 0
    for r in rows:
        total = coerce(r.get("total_elements"))
        successful = coerce(r.get("successful_elements"))
        if total is None or successful is None:
            continue
        measured += 1
        if successful < total:
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


def sign_disagreement(total_pct: float | None, paired_pct: float | None) -> bool:
    """True when the totals and the paired reading point opposite ways.

    That disagreement is the alarm: it means one runaway run, or one cluster
    boundary, is driving a reading. Zero on either side is 'did not move', not
    a contradiction.
    """
    if total_pct is None or paired_pct is None:
        return False
    if total_pct == 0 or paired_pct == 0:
        return False
    return (total_pct > 0) != (paired_pct > 0)


def _print_pairing_note(base_rows: list[dict], cand_rows: list[dict]) -> None:
    base_ids, cand_ids = query_ids(base_rows), query_ids(cand_rows)
    if not base_ids & cand_ids:
        print("\n!! NO SHARED QUERIES — the two runs have no query_id in common, "
              "so paired% cannot be computed and the totals below span "
              "different query sets !!")
        return
    unpaired = sorted(base_ids ^ cand_ids)
    if unpaired:
        print(f"\nnote: {len(unpaired)} unpaired query id(s) excluded from "
              f"paired% (totals still span every row): {', '.join(unpaired)}")


def print_compare(base_rows: list[dict], cand_rows: list[dict]) -> None:
    totals = compare_summaries(summarize_rows(base_rows, NUMERIC_METRICS),
                               summarize_rows(cand_rows, NUMERIC_METRICS))
    paired = compare_by_query(base_rows, cand_rows, NUMERIC_METRICS)
    print(f"\n== baseline vs candidate (totals + paired per-query; pass rate "
          f"{pass_rate(base_rows)}% → {pass_rate(cand_rows)}%) ==")
    print(compare_rate_line("budget exhausted",
                            exhaustion_counts(base_rows), exhaustion_counts(cand_rows)))
    print(compare_rate_line("runs w/ unresolved elems",
                            miss_counts(base_rows), miss_counts(cand_rows)))
    _print_pairing_note(base_rows, cand_rows)
    print(f"\n{'metric':<28}{'base_total':>13}{'cand_total':>13}"
          f"{'total%':>9}{'paired%':>9}{'q+/q-':>8}{'n':>4}")
    for metric in NUMERIC_METRICS:
        t, p = totals[metric], paired[metric]
        total_pct, paired_pct = t["sum_pct"], p["paired_pct"]
        tp = f"{total_pct:+.1f}%" if total_pct is not None else "-"
        pp = f"{paired_pct:+.1f}%" if paired_pct is not None else "-"
        split = f"{p['up']}/{p['down']}"
        mark = "  <>" if sign_disagreement(total_pct, paired_pct) else ""
        print(f"{metric:<28}{_fmt(t['baseline_sum']):>13}"
              f"{_fmt(t['candidate_sum']):>13}{tp:>9}{pp:>9}"
              f"{split:>8}{p['n_paired']:>4}{mark}")
    print("\npaired% is the median of the per-query changes — the reading to "
          "trust. total% is the cross-check: '<>' marks the two pointing "
          "opposite ways, which means one run or one query is driving it.")
    print("guardrails (must hold or improve): "
          + ", ".join(GUARDRAILS) + ", pass rate")


def print_by_query_compare(base_rows: list[dict], cand_rows: list[dict]) -> None:
    paired = compare_by_query(base_rows, cand_rows, HEADLINE_METRICS)
    totals = compare_summaries(summarize_rows(base_rows, HEADLINE_METRICS),
                               summarize_rows(cand_rows, HEADLINE_METRICS))
    for metric in HEADLINE_METRICS:
        d = paired[metric]
        print(f"\nper-query detail: {metric}")
        if not d["per_query"]:
            print("   no shared queries with a value for this metric")
            continue
        for query_id, v in d["per_query"].items():
            pct = f"{v['pct']:+.1f}%" if v["pct"] is not None else "-"
            print(f"   {query_id:<10}{_fmt(v['baseline']):>13} → "
                  f"{_fmt(v['candidate']):>13}{pct:>9}")
        total_pct = totals[metric]["sum_pct"]
        pp = f"{d['paired_pct']:+.1f}%" if d["paired_pct"] is not None else "-"
        tp = f"{total_pct:+.1f}%" if total_pct is not None else "-"
        print(f"   {'paired median':<10}{pp:>35}      total {tp}")


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
                        help="per-query breakdown: every metric in single-CSV "
                             "mode, the headline metrics in compare mode")
    args = parser.parse_args()

    if len(args.csv) > 2:
        parser.error("pass one CSV (summary) or two CSVs (compare)")

    rows = load_rows(args.csv[0])
    if len(args.csv) == 1:
        print_summary(rows, Path(args.csv[0]).name)
        if args.by_query:
            print_by_query(rows)
    else:
        cand_rows = load_rows(args.csv[1])
        print_pin_check(args.csv[0], args.csv[1])
        print_compare(rows, cand_rows)
        if args.by_query:
            print_by_query_compare(rows, cand_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())

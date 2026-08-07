"""Paired per-query comparison and the totals cross-check.

The pooled median across all rows is not a safe comparison statistic on this
bench: the 10 queries sit in distinct token-size clusters, so the pooled median
lands on a cluster boundary and flips on noise. The pooled sum is not safe
either — a single runaway run moves it more than the whole real difference.
Only the median of the per-query percent changes is robust to both.
"""

import statistics

from bench.bench_lib import coerce, compare_by_query, compare_summaries, summarize_rows

# The real 2026-07-29 bench pair, llm_tokens, 10 queries x 3 repeats.
# baseline = 2026-07-29-pre-phase2-baseline-cssfix.csv
# candidate = 2026-07-29-review-fixes.csv
# q06 repeat 3 is a genuine runaway run (180,035 vs a ~46,700 typical q06).
BASELINE_TOKENS = {
    "q01": [31423, 44141, 44193],
    "q02": [31964, 31895, 31734],
    "q03": [44177, 44133, 44020],
    "q04": [32181, 32272, 32365],
    "q05": [32424, 32254, 32378],
    "q06": [46508, 46941, 180035],
    "q07": [69042, 68915, 68945],
    "q08": [33146, 45249, 45311],
    "q09": [32150, 32157, 31786],
    "q10": [36621, 34152, 36609],
}
CANDIDATE_TOKENS = {
    "q01": [44144, 44149, 44176],
    "q02": [31669, 31693, 31907],
    "q03": [44231, 44404, 44288],
    "q04": [32306, 32282, 32301],
    "q05": [32269, 32222, 32239],
    "q06": [46660, 47681, 46787],
    "q07": [68795, 68757, 68994],
    "q08": [45290, 45357, 45424],
    "q09": [31823, 31993, 32060],
    "q10": [36673, 34058, 34149],
}


def rows_from(per_query: dict[str, list], metric: str = "llm_tokens") -> list[dict]:
    return [
        {"query_id": q, "repeat": str(i + 1), metric: str(v)}
        for q, vals in per_query.items()
        for i, v in enumerate(vals)
    ]


class TestSummarizeRowsSum:
    def test_sum_over_rows(self):
        rows = [{"llm_tokens": 10}, {"llm_tokens": 20}, {"llm_tokens": 30}]
        assert summarize_rows(rows, ["llm_tokens"])["llm_tokens"]["sum"] == 60

    def test_sum_coerces_csv_strings_and_skips_blanks(self):
        rows = [{"llm_tokens": "10.5"}, {"llm_tokens": ""}, {"llm_tokens": "4.5"}]
        s = summarize_rows(rows, ["llm_tokens"])["llm_tokens"]
        assert s["sum"] == 15.0
        assert s["n"] == 2

    def test_sum_is_none_when_column_absent(self):
        # None, not 0 — a column no row carries was never measured, and a
        # printed 0 would read as a measured zero.
        s = summarize_rows([{}, {}], ["step_budget_exhausted"])
        assert s["step_budget_exhausted"]["sum"] is None

    def test_sum_of_genuine_zeros_is_zero(self):
        s = summarize_rows([{"flake_retries": 0}, {"flake_retries": 0}], ["flake_retries"])
        assert s["flake_retries"]["sum"] == 0


class TestCompareSummariesSum:
    def test_sum_delta_and_pct(self):
        base = summarize_rows([{"m": 100}, {"m": 100}], ["m"])
        cand = summarize_rows([{"m": 80}, {"m": 80}], ["m"])
        c = compare_summaries(base, cand)["m"]
        assert c["baseline_sum"] == 200
        assert c["candidate_sum"] == 160
        assert c["sum_delta"] == -40
        assert c["sum_pct"] == -20.0

    def test_zero_baseline_sum_pct_is_none(self):
        base = summarize_rows([{"failed_elements": 0}], ["failed_elements"])
        cand = summarize_rows([{"failed_elements": 3}], ["failed_elements"])
        c = compare_summaries(base, cand)["failed_elements"]
        assert c["sum_delta"] == 3
        assert c["sum_pct"] is None

    def test_absent_column_gives_none_delta(self):
        base = summarize_rows([{}], ["agent_run_s"])
        cand = summarize_rows([{"agent_run_s": 5.0}], ["agent_run_s"])
        c = compare_summaries(base, cand)["agent_run_s"]
        assert c["baseline_sum"] is None
        assert c["sum_delta"] is None
        assert c["sum_pct"] is None


class TestCompareByQuery:
    def test_per_query_medians_and_pct(self):
        base = rows_from({"q01": [100, 110, 120], "q02": [200, 200, 200]})
        cand = rows_from({"q01": [110, 121, 132], "q02": [180, 180, 180]})
        d = compare_by_query(base, cand, ["llm_tokens"])["llm_tokens"]
        assert d["per_query"]["q01"]["baseline"] == 110
        assert d["per_query"]["q01"]["candidate"] == 121
        assert d["per_query"]["q01"]["pct"] == 10.0
        assert d["per_query"]["q02"]["pct"] == -10.0
        assert d["n_paired"] == 2

    def test_paired_pct_is_median_of_per_query_changes(self):
        base = rows_from({"q01": [100], "q02": [100], "q03": [100]})
        cand = rows_from({"q01": [101], "q02": [110], "q03": [500]})
        d = compare_by_query(base, cand, ["llm_tokens"])["llm_tokens"]
        # +1%, +10%, +400% -> median +10%, unmoved by the +400% outlier
        assert d["paired_pct"] == 10.0

    def test_up_and_down_counts_ignore_unchanged(self):
        base = rows_from({"q01": [100], "q02": [100], "q03": [100]})
        cand = rows_from({"q01": [110], "q02": [90], "q03": [100]})
        d = compare_by_query(base, cand, ["llm_tokens"])["llm_tokens"]
        assert (d["up"], d["down"]) == (1, 1)

    def test_query_present_on_one_side_only_is_excluded(self):
        base = rows_from({"q01": [100], "q02": [100]})
        cand = rows_from({"q01": [110], "q99": [110]})
        d = compare_by_query(base, cand, ["llm_tokens"])["llm_tokens"]
        assert set(d["per_query"]) == {"q01"}
        assert d["n_paired"] == 1

    def test_no_shared_queries(self):
        base = rows_from({"q01": [100]})
        cand = rows_from({"astpp_q01": [100]})
        d = compare_by_query(base, cand, ["llm_tokens"])["llm_tokens"]
        assert d["per_query"] == {}
        assert d["paired_pct"] is None
        assert d["n_paired"] == 0

    def test_metric_absent_from_both_files(self):
        # A pre-change baseline lacking a newer column must not crash.
        base = rows_from({"q01": [100]})
        cand = rows_from({"q01": [110]})
        d = compare_by_query(base, cand, ["step_budget_exhausted"])["step_budget_exhausted"]
        assert d["n_paired"] == 0
        assert d["paired_pct"] is None

    def test_zero_baseline_query_has_no_pct_but_is_still_listed(self):
        base = rows_from({"q01": [0], "q02": [100]})
        cand = rows_from({"q01": [5], "q02": [110]})
        d = compare_by_query(base, cand, ["llm_tokens"])["llm_tokens"]
        assert d["per_query"]["q01"]["pct"] is None
        assert d["per_query"]["q01"]["candidate"] == 5
        # the undefined query must not drag the median
        assert d["paired_pct"] == 10.0

    def test_all_zero_baselines_give_no_paired_pct(self):
        # failed_elements / llm_429_count behave this way: every per-query
        # baseline median is 0, so no percentage is defined. The totals line
        # is what carries the signal for these columns.
        base = rows_from({"q01": [0], "q02": [0]})
        cand = rows_from({"q01": [1], "q02": [2]})
        d = compare_by_query(base, cand, ["llm_tokens"])["llm_tokens"]
        assert d["paired_pct"] is None

    def test_blank_cells_are_skipped_not_zeroed(self):
        base = [
            {"query_id": "q01", "llm_tokens": "100"},
            {"query_id": "q01", "llm_tokens": ""},
        ]
        cand = [{"query_id": "q01", "llm_tokens": "110"}]
        d = compare_by_query(base, cand, ["llm_tokens"])["llm_tokens"]
        assert d["per_query"]["q01"]["baseline"] == 100
        assert d["per_query"]["q01"]["pct"] == 10.0

    def test_unequal_repeat_counts_are_tolerated(self):
        base = rows_from({"q01": [100, 100, 100, 100]})
        cand = rows_from({"q01": [110, 110]})
        d = compare_by_query(base, cand, ["llm_tokens"])["llm_tokens"]
        assert d["per_query"]["q01"]["pct"] == 10.0
        assert d["n_paired"] == 1


class TestRealBenchRegression:
    """The 2026-07-29 pair: pooled median says +14.2%, total says -8.2%,
    the truth is flat. Locks in that the paired statistic reads flat on the
    exact data that broke the old reading."""

    def setup_method(self):
        self.base = rows_from(BASELINE_TOKENS)
        self.cand = rows_from(CANDIDATE_TOKENS)

    def _pooled(self, rows):
        return statistics.median(
            [coerce(r["llm_tokens"]) for r in rows]
        )

    def test_the_pooled_median_moves_the_wrong_way(self):
        # This is the defect being fixed, asserted so it stays visible.
        base_med, cand_med = self._pooled(self.base), self._pooled(self.cand)
        pct = (cand_med - base_med) / base_med * 100
        assert pct > 10, f"expected the misleading pooled jump, got {pct:.1f}%"

    def test_the_total_overstates_the_move_because_of_one_runaway_run(self):
        b = summarize_rows(self.base, ["llm_tokens"])["llm_tokens"]["sum"]
        c = summarize_rows(self.cand, ["llm_tokens"])["llm_tokens"]["sum"]
        pct = (c - b) / b * 100
        assert pct < -5, f"expected the outlier-driven drop, got {pct:.1f}%"
        # the q06 r3 excess alone exceeds the entire delta
        excess = 180035 - statistics.median([46508, 46941])
        assert excess > abs(c - b)

    def test_paired_comparison_reads_flat(self):
        d = compare_by_query(self.base, self.cand, ["llm_tokens"])["llm_tokens"]
        assert d["n_paired"] == 10
        assert abs(d["paired_pct"]) < 1.0, (
            f"per-query medians are flat within +-0.7%; paired statistic "
            f"read {d['paired_pct']:+.1f}%")

    def test_every_query_except_q10_is_flat_within_one_percent(self):
        d = compare_by_query(self.base, self.cand, ["llm_tokens"])["llm_tokens"]
        for q, v in d["per_query"].items():
            if q == "q10":
                assert -8 < v["pct"] < -5   # the one real mover, -6.7%
            else:
                assert abs(v["pct"]) < 1.0, f"{q} moved {v['pct']:+.2f}%"

    def test_the_runaway_run_does_not_reach_the_paired_statistic(self):
        # Drop q06 r3 entirely; the paired reading must barely move.
        trimmed = {**BASELINE_TOKENS, "q06": BASELINE_TOKENS["q06"][:2]}
        with_outlier = compare_by_query(
            self.base, self.cand, ["llm_tokens"])["llm_tokens"]["paired_pct"]
        without_outlier = compare_by_query(
            rows_from(trimmed), self.cand, ["llm_tokens"])["llm_tokens"]["paired_pct"]
        assert abs(with_outlier - without_outlier) < 0.5

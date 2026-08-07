"""Report math: median, p90 (nearest-rank), summarize and compare."""

from bench.bench_lib import compare_summaries, median_p90, summarize_rows


class TestMedianP90:
    def test_one_through_ten(self):
        med, p90 = median_p90([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        assert med == 5.5
        assert p90 == 9  # nearest-rank: ceil(0.9*10)=9 → 9th smallest

    def test_single_value(self):
        assert median_p90([7.5]) == (7.5, 7.5)

    def test_three_values_p90_is_max(self):
        med, p90 = median_p90([3.0, 1.0, 2.0])
        assert med == 2.0
        assert p90 == 3.0  # ceil(0.9*3)=3 → 3rd smallest = max

    def test_empty(self):
        assert median_p90([]) == (None, None)

    def test_order_independent(self):
        assert median_p90([9, 1, 5]) == median_p90([1, 5, 9])


class TestSummarizeRows:
    def test_per_metric_median_p90_and_n(self):
        rows = [
            {"plan_s": 4.0, "llm_calls": 10},
            {"plan_s": 6.0, "llm_calls": 12},
            {"plan_s": 5.0, "llm_calls": 11},
        ]
        s = summarize_rows(rows, ["plan_s", "llm_calls"])
        assert s["plan_s"]["median"] == 5.0
        assert s["plan_s"]["p90"] == 6.0
        assert s["plan_s"]["n"] == 3
        assert s["llm_calls"]["median"] == 11

    def test_none_and_missing_values_are_skipped(self):
        rows = [
            {"plan_s": 4.0},
            {"plan_s": None},
            {},
            {"plan_s": 8.0},
        ]
        s = summarize_rows(rows, ["plan_s"])
        assert s["plan_s"]["n"] == 2
        assert s["plan_s"]["median"] == 6.0

    def test_string_numbers_from_csv_are_coerced(self):
        # csv.DictReader yields strings; empty string means missing.
        rows = [{"plan_s": "4.0"}, {"plan_s": ""}, {"plan_s": "6.0"}]
        s = summarize_rows(rows, ["plan_s"])
        assert s["plan_s"]["n"] == 2
        assert s["plan_s"]["median"] == 5.0

    def test_all_missing_metric(self):
        s = summarize_rows([{}, {}], ["plan_s"])
        assert s["plan_s"]["median"] is None
        assert s["plan_s"]["p90"] is None
        assert s["plan_s"]["n"] == 0


class TestCompareSummaries:
    def test_deltas_and_pct(self):
        base = summarize_rows([{"plan_s": 10.0}, {"plan_s": 10.0}], ["plan_s"])
        cand = summarize_rows([{"plan_s": 8.0}, {"plan_s": 8.0}], ["plan_s"])
        c = compare_summaries(base, cand)
        assert c["plan_s"]["baseline_median"] == 10.0
        assert c["plan_s"]["candidate_median"] == 8.0
        assert c["plan_s"]["median_delta"] == -2.0
        assert c["plan_s"]["median_pct"] == -20.0

    def test_zero_baseline_pct_is_none(self):
        base = summarize_rows([{"flake_retries": 0}], ["flake_retries"])
        cand = summarize_rows([{"flake_retries": 2}], ["flake_retries"])
        c = compare_summaries(base, cand)
        assert c["flake_retries"]["median_delta"] == 2
        assert c["flake_retries"]["median_pct"] is None

    def test_metric_missing_on_one_side(self):
        base = summarize_rows([{"plan_s": 10.0}], ["plan_s"])
        cand = summarize_rows([{}], ["plan_s"])
        c = compare_summaries(base, cand)
        assert c["plan_s"]["candidate_median"] is None
        assert c["plan_s"]["median_delta"] is None
        assert c["plan_s"]["median_pct"] is None

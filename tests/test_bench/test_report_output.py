"""What the compare report actually prints.

Guards the two readings that must never disagree silently: the totals line and
the paired per-query line. A sign disagreement between them is marked, because
on a 43-metric table a real one is easy to scroll past.
"""

from bench.report import (
    HEADLINE_METRICS,
    print_by_query_compare,
    print_compare,
    sign_disagreement,
)
from tests.test_bench.test_paired_compare import (
    BASELINE_TOKENS,
    CANDIDATE_TOKENS,
    rows_from,
)


class TestSignDisagreement:
    def test_opposite_signs_disagree(self):
        assert sign_disagreement(-4.6, 8.2) is True
        assert sign_disagreement(396.7, -0.2) is True

    def test_same_sign_agrees(self):
        assert sign_disagreement(-8.2, -0.3) is False
        assert sign_disagreement(4.7, 14.2) is False

    def test_zero_is_not_a_disagreement(self):
        # A flat total against a moving paired reading is not a contradiction.
        assert sign_disagreement(0.0, 5.0) is False
        assert sign_disagreement(-8.5, 0.0) is False

    def test_missing_side_is_not_a_disagreement(self):
        assert sign_disagreement(None, 5.0) is False
        assert sign_disagreement(-4.6, None) is False
        assert sign_disagreement(None, None) is False


class TestPrintCompare:
    def test_prints_totals_and_paired_columns(self, capsys):
        print_compare(rows_from(BASELINE_TOKENS), rows_from(CANDIDATE_TOKENS))
        out = capsys.readouterr().out
        assert "base_total" in out
        assert "cand_total" in out
        assert "total%" in out
        assert "paired%" in out

    def test_does_not_print_the_pooled_median(self, capsys):
        print_compare(rows_from(BASELINE_TOKENS), rows_from(CANDIDATE_TOKENS))
        out = capsys.readouterr().out
        # 40408 / 40408.5 is the misleading pooled candidate median.
        assert "40408" not in out.replace(",", "")

    def test_llm_tokens_line_shows_flat_paired_and_negative_total(self, capsys):
        print_compare(rows_from(BASELINE_TOKENS), rows_from(CANDIDATE_TOKENS))
        line = next(ln for ln in capsys.readouterr().out.splitlines()
                    if ln.startswith("llm_tokens"))
        assert "-8.2%" in line     # total
        assert "-0.3%" in line     # paired
        assert "4/6" in line       # 4 queries up, 6 down

    def test_warns_loudly_when_no_queries_are_shared(self, capsys):
        base = rows_from({"q01": [100], "q02": [100]})
        cand = rows_from({"astpp_q01": [100], "astpp_q02": [100]})
        print_compare(base, cand)
        out = capsys.readouterr().out
        assert "NO SHARED QUERIES" in out

    def test_notes_unpaired_queries_without_failing(self, capsys):
        base = rows_from({"q01": [100], "q02": [100]})
        cand = rows_from({"q01": [110], "q03": [110]})
        print_compare(base, cand)
        out = capsys.readouterr().out
        assert "unpaired" in out
        assert "q02" in out and "q03" in out
        assert "NO SHARED QUERIES" not in out

    def test_survives_a_baseline_missing_newer_columns(self, capsys):
        # Pre-change baselines are compared regularly and lack newer columns.
        # (step_budget_exhausted is not in this table by design — it is scored
        # as the 'budget exhausted' rate line above it.)
        base = [{"query_id": "q01", "llm_tokens": "100"}]
        cand = [{"query_id": "q01", "llm_tokens": "110",
                 "agent_run_s": "5.0", "llm_coverage_gap": "-1"}]
        print_compare(base, cand)
        out = capsys.readouterr().out
        line = next(ln for ln in out.splitlines() if ln.startswith("agent_run_s"))
        # baseline never measured it: absent, not zero
        assert "-" in line
        assert next(ln for ln in out.splitlines()
                    if ln.startswith("llm_coverage_gap"))

    def test_pass_rate_and_rate_lines_are_kept(self, capsys):
        base = [{"query_id": "q01", "test_status": "passed",
                 "total_elements": "2", "successful_elements": "2"}]
        cand = [{"query_id": "q01", "test_status": "failed",
                 "total_elements": "2", "successful_elements": "1"}]
        print_compare(base, cand)
        out = capsys.readouterr().out
        assert "pass rate" in out
        assert "budget exhausted" in out
        assert "runs w/ unresolved elems" in out

    def test_guardrail_footer_is_kept(self, capsys):
        print_compare(rows_from(BASELINE_TOKENS), rows_from(CANDIDATE_TOKENS))
        assert "guardrails" in capsys.readouterr().out


class TestPrintByQueryCompare:
    def test_lists_every_shared_query_for_headline_metrics(self, capsys):
        print_by_query_compare(rows_from(BASELINE_TOKENS), rows_from(CANDIDATE_TOKENS))
        out = capsys.readouterr().out
        for q in BASELINE_TOKENS:
            assert q in out
        assert "llm_tokens" in out

    def test_covers_exactly_the_headline_metrics(self, capsys):
        print_by_query_compare(rows_from(BASELINE_TOKENS), rows_from(CANDIDATE_TOKENS))
        out = capsys.readouterr().out
        for metric in HEADLINE_METRICS:
            assert f"per-query detail: {metric}" in out

    def test_shows_the_paired_and_total_summary_per_metric(self, capsys):
        print_by_query_compare(rows_from(BASELINE_TOKENS), rows_from(CANDIDATE_TOKENS))
        out = capsys.readouterr().out
        assert "paired median" in out
        assert "-6.7%" in out     # q10, the one real mover

    def test_handles_a_headline_metric_with_no_data(self, capsys):
        base = [{"query_id": "q01", "llm_tokens": "100"}]
        cand = [{"query_id": "q01", "llm_tokens": "110"}]
        print_by_query_compare(base, cand)
        out = capsys.readouterr().out
        assert "per-query detail: llm_cost_usd" in out
        assert "no shared queries" in out

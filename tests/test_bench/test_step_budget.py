"""The step-budget-exhaustion detector.

browser-use runs exactly `max_steps` iterations and forces the `done` tool on
the last one, so a run that consumed its whole budget lands on
`len(history) == max_steps`. The browser service sets that cap from the element
count and never reports it, so the formula is mirrored here — see
test_cap_formula_still_matches_the_browser_service.
"""

import re
from pathlib import Path

import pytest

from bench.bench_lib import (
    CSV_COLUMNS,
    build_csv_row,
    extract_metrics_fields,
    step_budget_cap,
    step_budget_exhausted,
)

VENDORED_WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / "tools" / "browser_service" / "tasks" / "workflow.py"
)


class TestStepBudgetExhausted:
    """Shapes are real rows from bench/baselines/astpp-2026-07-28-post-timeout-fix.csv."""

    @pytest.mark.parametrize("elements,steps", [
        (5, 25),   # astpp_q03 rep1 and astpp_q05 rep3 — exactly on the cap
        (5, 26),   # astpp_q03 rep3 — one past the cap, see spec section 3
        (4, 22),   # astpp_q06 rep3 — cap 22
    ])
    def test_fires_on_the_real_miss_shapes(self, elements, steps):
        """Three distinct shapes across the four miss rows; two rows share (5, 25)."""
        assert step_budget_exhausted(elements, steps) == 1

    @pytest.mark.parametrize("elements,steps", [
        (5, 8),    # astpp_q01 rep1
        (4, 6),    # astpp_q02 rep2
        (5, 7),    # astpp_q04 rep1
    ])
    def test_does_not_fire_on_clean_shapes(self, elements, steps):
        assert step_budget_exhausted(elements, steps) == 0

    def test_does_not_fire_one_step_short_of_the_cap(self):
        """cap-1 is a DIFFERENT mechanism: the agent called done voluntarily.

        Across the 913 captured runs, 12 rows sit here and none of them lost an
        element. Counting them would sweep in unrelated failures.
        """
        assert step_budget_cap(4) == 22
        assert step_budget_exhausted(4, 21) == 0

    def test_cap_is_three_per_element_plus_ten(self):
        assert step_budget_cap(1) == 13
        assert step_budget_cap(4) == 22
        assert step_budget_cap(5) == 25


class TestUnmeasurable:
    """None, never 0 — an unscorable run must not read as a clean run."""

    def test_missing_element_count_is_none(self):
        assert step_budget_exhausted(None, 25) is None

    def test_missing_step_count_is_none(self):
        assert step_budget_exhausted(5, None) is None

    def test_zero_elements_is_none(self):
        """The run never reached identification; the cap of 10 is meaningless."""
        assert step_budget_exhausted(0, 25) is None

    def test_zero_steps_is_measured_not_missing(self):
        """A crashed agent reports 0 steps. That is a measurement: not exhausted."""
        assert step_budget_exhausted(5, 0) == 0


class TestExtractAndSerialise:
    def test_extract_sets_the_flag(self):
        fields = extract_metrics_fields(
            {"total_elements": 5, "browser_use_llm_calls": 25}
        )
        assert fields["step_budget_exhausted"] == 1

    def test_extract_leaves_it_unmeasurable_without_the_step_count(self):
        fields = extract_metrics_fields({"total_elements": 5})
        assert fields["step_budget_exhausted"] is None

    def test_column_is_declared(self):
        """build_csv_row raises on unknown keys — these must move together."""
        assert "step_budget_exhausted" in CSV_COLUMNS

    def test_row_carries_one_zero_or_empty_never_a_bool(self):
        exhausted = build_csv_row(
            extract_metrics_fields({"total_elements": 5, "browser_use_llm_calls": 25})
        )
        clean = build_csv_row(
            extract_metrics_fields({"total_elements": 5, "browser_use_llm_calls": 8})
        )
        unmeasured = build_csv_row(extract_metrics_fields({"total_elements": 5}))
        assert exhausted["step_budget_exhausted"] == 1
        assert clean["step_budget_exhausted"] == 0
        assert unmeasured["step_budget_exhausted"] == ""
        for row in (exhausted, clean):
            assert not isinstance(row["step_budget_exhausted"], bool)


class TestCrossRepoDrift:
    def test_cap_formula_still_matches_the_browser_service(self):
        """The cap lives in the browser service and is never reported to us.

        tools/browser_service/* is gitignored, so this guard runs on the owner's
        machine — the only place a bench is ever run — and skips in CI.
        """
        if not VENDORED_WORKFLOW.exists():
            pytest.skip(
                f"vendored browser-service copy absent ({VENDORED_WORKFLOW}); "
                "tools/browser_service/* is gitignored, so this guard only runs "
                "where benches are run"
            )
        source = VENDORED_WORKFLOW.read_text(encoding="utf-8")
        match = re.search(r"dynamic_max_steps\s*=\s*(.+)", source)
        assert match, "dynamic_max_steps assignment not found — the cap moved"
        expression = re.sub(r"\s+", "", match.group(1))
        assert expression == "1+(len(elements)*3)+1+8", (
            f"browser-service step budget changed to `{match.group(1).strip()}`. "
            "step_budget_cap() in bench/bench_lib.py mirrors it and must be "
            "updated, or step_budget_exhausted silently reports the wrong thing. "
            "Updating the constants also retroactively re-scores every baseline "
            "CSV that lacks a stored step_budget_exhausted column, since those "
            "rows are derived at read time with whatever constants are current."
        )


class TestReportRates:
    def test_counts_hits_over_measured_rows(self):
        from bench.report import exhaustion_counts

        rows = [
            {"step_budget_exhausted": "1"},
            {"step_budget_exhausted": "0"},
            {"step_budget_exhausted": "1"},
        ]
        assert exhaustion_counts(rows) == (2, 3)

    def test_empty_cells_shrink_the_denominator(self):
        from bench.report import exhaustion_counts

        rows = [
            {"step_budget_exhausted": "1"},
            {"step_budget_exhausted": ""},
            {"step_budget_exhausted": ""},
        ]
        assert exhaustion_counts(rows) == (1, 1)

    def test_a_baseline_without_the_column_measures_nothing(self):
        """Every baseline before 4b91756 lacks it, so DictReader yields no key
        at all. Missing key and empty cell must take the same path."""
        from bench.report import exhaustion_counts

        rows = [{"total_s": "27.8"}, {"total_s": "31.2"}]
        assert exhaustion_counts(rows) == (0, 0)

    def test_zero_is_measured_not_missing(self):
        from bench.report import exhaustion_counts

        assert exhaustion_counts([{"step_budget_exhausted": "0"}]) == (0, 1)

    def test_derives_from_elements_and_calls_when_column_absent(self):
        """Pre-existing baselines lack step_budget_exhausted; derive from stored metrics.
        This is the shape of every baseline before this feature shipped."""
        from bench.report import exhaustion_counts

        rows = [{"total_elements": "5", "browser_use_llm_calls": "25"}]
        assert exhaustion_counts(rows) == (1, 1)

    def test_derives_clean_run_when_column_absent(self):
        """Same derivation, but the run is clean (below the cap)."""
        from bench.report import exhaustion_counts

        rows = [{"total_elements": "5", "browser_use_llm_calls": "8"}]
        assert exhaustion_counts(rows) == (0, 1)

    def test_stored_value_beats_derivation(self):
        """When step_budget_exhausted is recorded, it is the source of truth,
        even if the derived value would differ."""
        from bench.report import exhaustion_counts

        # Stored says clean (0), but cap would be 25, so 25 would trigger derivation
        rows = [
            {"step_budget_exhausted": "0", "total_elements": "5", "browser_use_llm_calls": "25"}
        ]
        assert exhaustion_counts(rows) == (0, 1)

    def test_unmeasurable_when_browser_use_calls_absent(self):
        """2026-07-26 main baseline lacks browser_use_llm_calls; cannot derive."""
        from bench.report import exhaustion_counts

        rows = [{"total_elements": "5"}]
        assert exhaustion_counts(rows) == (0, 0)

    def test_unmeasurable_when_browser_use_calls_non_numeric(self):
        """A malformed cell (e.g. 'n/a' or corruption) must not crash a report."""
        from bench.report import exhaustion_counts

        rows = [{"total_elements": "5", "browser_use_llm_calls": "n/a"}]
        assert exhaustion_counts(rows) == (0, 0)


class TestPairedMissRate:
    """Counts rows, never a median.

    On the 07-28 ASTPP set failed_elements has median 0.0 while four rows had
    misses — the same disease that let locator_success_rate read green at 22%
    failure.
    """

    def test_counts_rows_above_zero(self):
        from bench.report import miss_counts

        rows = [
            {"failed_elements": "0"},
            {"failed_elements": "3"},
            {"failed_elements": "0"},
            {"failed_elements": "2"},
        ]
        assert miss_counts(rows) == (2, 4)

    def test_a_median_of_zero_still_reports_the_misses(self):
        rows = [{"failed_elements": "0"}] * 14 + [{"failed_elements": "3"}] * 4
        from statistics import median

        from bench.report import miss_counts

        assert median([float(r["failed_elements"]) for r in rows]) == 0.0
        assert miss_counts(rows) == (4, 18)

    def test_float_formatting_is_accepted(self):
        from bench.report import miss_counts

        assert miss_counts([{"failed_elements": "3.0"}]) == (1, 1)

    def test_non_numeric_cell_is_unmeasurable_not_a_crash(self):
        """A malformed cell (e.g. 'n/a' or corruption) must not crash a
        report — the same guarantee exhaustion_counts already has."""
        from bench.report import miss_counts

        rows = [{"failed_elements": "n/a"}, {"failed_elements": "2"}]
        assert miss_counts(rows) == (1, 1)


class TestRateLines:
    def test_shows_the_percentage_when_everything_is_measured(self):
        """Asserts content, not column padding — a spacing tweak is not a bug."""
        from bench.report import rate_line

        line = rate_line("budget exhausted", 4, 18, 18)
        assert line.strip().startswith("budget exhausted")
        assert "4/18 measured (22.2%)" in line
        assert "unmeasurable" not in line

    def test_flags_unmeasurable_rows_when_some_are_missing(self):
        from bench.report import rate_line

        assert "2 rows unmeasurable" in rate_line("budget exhausted", 1, 16, 18)

    def test_reports_nothing_measured_rather_than_zero_percent(self):
        from bench.report import rate_line

        line = rate_line("budget exhausted", 0, 0, 30)
        assert "0/0 measured" in line
        assert "30 rows unmeasurable" in line

    def test_compare_labels_an_unmeasurable_baseline(self):
        """0/0 beside 4/18 reads as a regression when the baseline was simply
        never measured."""
        from bench.report import compare_rate_line

        line = compare_rate_line("budget exhausted", (0, 0), (4, 18))
        assert "not comparable" in line
        assert "baseline" in line

    def test_compare_shows_both_sides_when_both_are_measured(self):
        from bench.report import compare_rate_line

        line = compare_rate_line("budget exhausted", (4, 18), (0, 18))
        assert "4/18" in line and "0/18" in line


class TestPairedLinesArePrinted:
    """The unresolved-elements line is non-optional beside the exhaustion
    line (design doc §5.2/§7 A): exhaustion alone is gameable by a fix that
    makes the agent quit early instead of looping. Nothing today calls
    print_summary or print_compare, so deleting either print() leaves the
    rest of the bench suite green — lock both call sites here."""

    ROWS = [
        {"test_status": "passed", "step_budget_exhausted": "1", "failed_elements": "2"},
        {"test_status": "passed", "step_budget_exhausted": "0", "failed_elements": "0"},
    ]

    def test_print_summary_shows_both_lines(self, capsys):
        from bench.report import print_summary

        print_summary(self.ROWS, "test.csv")
        out = capsys.readouterr().out
        assert "budget exhausted" in out
        assert "runs w/ unresolved elems" in out

    def test_print_compare_shows_both_lines(self, capsys):
        from bench.report import print_compare

        print_compare(self.ROWS, self.ROWS)
        out = capsys.readouterr().out
        assert "budget exhausted" in out
        assert "runs w/ unresolved elems" in out

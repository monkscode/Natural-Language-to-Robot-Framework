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
            "updated, or step_budget_exhausted silently reports the wrong thing."
        )

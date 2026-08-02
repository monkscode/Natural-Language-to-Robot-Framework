"""The bench guard for a zeroed crewai token accumulator.

crewai_tokens is 19.9% of the baseline median llm_tokens (7,888.5 of 39,722),
so an accumulator that silently reads zero shows up as a ~20% IMPROVEMENT
against a flat-or-down token gate, and it is invisible in the CSV.

That is the exact failure mode the IN-FLIGHT crewai 1.15 upgrade produces —
its response_model reroute sends the agent loop through instructor and the
accumulator is never written. The bench runs the pinned crewai==1.8.1; the
guard exists so the upgrade cannot land a silent 20% token "win".

The guard warns at the capture point instead of adding a CSV column — a new
column would trip gate_schema against every existing baseline.
"""

from unittest.mock import patch


class TestCrewaiTokenGuard:
    def test_warns_when_crewai_tokens_is_zero(self):
        """Exactly zero is the failure signature — warn loudly."""
        from bench.run_bench import warn_if_zero_crewai_tokens

        with patch("bench.run_bench._warn") as mock_warn:
            warn_if_zero_crewai_tokens({"crewai_tokens": 0}, "q01", 1, "wf-abc")

        mock_warn.assert_called_once()
        msg = mock_warn.call_args.args[0]
        assert "crewai_tokens == 0" in msg
        assert "wf-abc" in msg
        assert "q01" in msg

    def test_warns_when_crewai_tokens_key_is_absent(self):
        """A missing key is the same blindness as a zero."""
        from bench.run_bench import warn_if_zero_crewai_tokens

        with patch("bench.run_bench._warn") as mock_warn:
            warn_if_zero_crewai_tokens({"browser_use_tokens": 500}, "q02", 2, "wf-def")

        mock_warn.assert_called_once()

    def test_silent_when_crewai_tokens_is_populated(self):
        """A healthy run must not emit noise."""
        from bench.run_bench import warn_if_zero_crewai_tokens

        with patch("bench.run_bench._warn") as mock_warn:
            warn_if_zero_crewai_tokens({"crewai_tokens": 7888}, "q03", 1, "wf-ghi")

        mock_warn.assert_not_called()

    def test_warning_says_not_to_trust_the_cost_columns(self):
        """The point of the warning is that llm_tokens and llm_cost_usd are
        derived from crewai_tokens, so both are untrustworthy for this run."""
        from bench.run_bench import warn_if_zero_crewai_tokens

        with patch("bench.run_bench._warn") as mock_warn:
            warn_if_zero_crewai_tokens({"crewai_tokens": 0}, "q04", 1, "wf-jkl")

        msg = mock_warn.call_args.args[0]
        assert "llm_tokens" in msg and "llm_cost_usd" in msg

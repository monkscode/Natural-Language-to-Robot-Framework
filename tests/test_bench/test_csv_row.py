"""CSV row completeness + workflow_metrics-row field extraction."""

import pytest

from bench.bench_lib import CSV_COLUMNS, build_csv_row, extract_metrics_fields

REQUIRED_COLUMNS = {
    # identity
    "query_id", "query", "repeat", "workflow_id", "started_at",
    "generation_status", "test_status", "dryrun_status",
    # per-stage wall clock (SSE checkpoints, ±1s)
    "plan_s", "identify_s", "assemble_s", "dryrun_s", "exec_s", "total_s",
    # LLM usage/cost (workflow_metrics row)
    "llm_calls", "llm_tokens", "prompt_tokens", "completion_tokens", "llm_cost_usd",
    # locator success (workflow_metrics row)
    "total_elements", "successful_elements", "failed_elements", "locator_success_rate",
    # flake retries (metrics row + SSE repair events)
    "flake_retries", "dryrun_repairs",
    # browser-service log metrics
    "cold_start_s", "cleanup_s",
    "locator_timer_count", "locator_latency_ms_median", "locator_latency_ms_p90",
    # duplicate-lookup telemetry (parked for Task 15)
    "probe_total", "probe_unique", "duplicate_lookup_rate",
}


class TestCsvColumns:
    def test_all_required_columns_present(self):
        missing = REQUIRED_COLUMNS - set(CSV_COLUMNS)
        assert not missing, f"CSV_COLUMNS missing: {sorted(missing)}"

    def test_no_duplicate_columns(self):
        assert len(CSV_COLUMNS) == len(set(CSV_COLUMNS))


class TestBuildCsvRow:
    def test_row_has_exactly_the_columns_in_order(self):
        row = build_csv_row({"query_id": "q1", "plan_s": 4.0})
        assert list(row.keys()) == list(CSV_COLUMNS)
        assert row["query_id"] == "q1"
        assert row["plan_s"] == 4.0

    def test_missing_fields_become_empty_string(self):
        row = build_csv_row({})
        assert all(v == "" for v in row.values())

    def test_none_becomes_empty_string(self):
        row = build_csv_row({"plan_s": None})
        assert row["plan_s"] == ""

    def test_unknown_field_raises(self):
        with pytest.raises(ValueError, match="not_a_column"):
            build_csv_row({"not_a_column": 1})


class TestExtractMetricsFields:
    def _metrics_data(self):
        """Shape of workflow_metrics.data (WorkflowMetrics.to_dict())."""
        return {
            "workflow_id": "wf-123",
            "url": "https://example.com",
            "total_llm_calls": 14,
            "total_cost": 0.0123,
            "execution_time": 42.0,
            "crewai_llm_calls": 9,
            "crewai_cost": 0.01,
            "crewai_tokens": 5000,
            "crewai_prompt_tokens": 4000,
            "crewai_completion_tokens": 1000,
            "browser_use_llm_calls": 5,
            "browser_use_cost": 0.0023,
            "browser_use_tokens": 2000,
            "browser_use_prompt_tokens": 1500,
            "browser_use_completion_tokens": 500,
            "total_elements": 4,
            "successful_elements": 3,
            "failed_elements": 1,
            "success_rate": 75.0,
            "llm_cleaning_stats": {
                "total_responses": 9,
                "cleaned_responses": 2,
                "clean_rate": 22.2,
                "formatting_errors_detected": 1,
                "formatting_errors_recovered": 1,
                "empty_response_retries": 2,
                "empty_response_recoveries": 2,
                "empty_response_failures": 0,
            },
        }

    def test_extracts_llm_and_locator_fields(self):
        f = extract_metrics_fields(self._metrics_data())
        assert f["llm_calls"] == 14
        assert f["llm_tokens"] == 7000        # crewai 5000 + browser_use 2000
        assert f["prompt_tokens"] == 5500     # 4000 + 1500
        assert f["completion_tokens"] == 1500  # 1000 + 500
        assert f["llm_cost_usd"] == 0.0123
        assert f["total_elements"] == 4
        assert f["successful_elements"] == 3
        assert f["failed_elements"] == 1
        assert f["locator_success_rate"] == 75.0

    def test_flake_retries_is_empty_retries_plus_formatting_errors(self):
        f = extract_metrics_fields(self._metrics_data())
        assert f["flake_retries"] == 3  # empty_response_retries 2 + formatting_errors_detected 1

    def test_missing_cleaning_stats_means_zero_flakes(self):
        data = self._metrics_data()
        del data["llm_cleaning_stats"]
        f = extract_metrics_fields(data)
        assert f["flake_retries"] == 0

    def test_every_extracted_key_is_a_csv_column(self):
        f = extract_metrics_fields(self._metrics_data())
        assert set(f) <= set(CSV_COLUMNS)

class TestPhaseTimingColumns:
    """identify_s phase breakdown (2026-07-26 efficiency check).

    build_csv_row raises on unknown keys by design, so CSV_COLUMNS and
    extract_metrics_fields must move together or the bench dies on row one.
    """

    def test_extract_metrics_fields_flattens_phase_timings(self):
        data = {
            "crewai_tokens": 7214, "browser_use_tokens": 34428,
            "phase_timings": {
                "submit_s": 0.05, "queue_s": 0.01, "session_setup_s": 3.2,
                "agent_setup_s": 0.4, "agent_run_s": 20.76,
                "postprocess_s": 0.9, "poll_wait_s": 0.95,
            },
            "agent_diagnostics": {
                "dom_elements_max": 2143, "dom_elements_median": 1876,
                "llm_429_count": 2, "retry_lost_s": 3.4,
                "llm_total_s": 14.2, "llm_max_s": 8.1, "llm_calls_actual": 3,
                "steps_total_s": 18.9, "llm_coverage_gap": 0,
            },
        }
        fields = extract_metrics_fields(data)
        assert fields["session_setup_s"] == 3.2
        assert fields["poll_wait_s"] == 0.95
        assert fields["llm_429_count"] == 2
        assert fields["dom_elements_max"] == 2143
        assert fields["llm_total_s"] == 14.2
        assert fields["llm_max_s"] == 8.1
        assert fields["llm_calls_actual"] == 3
        assert fields["steps_total_s"] == 18.9
        assert fields["llm_coverage_gap"] == 0

    def test_extract_metrics_fields_handles_missing_phase_timings(self):
        """Pre-instrumentation rows and failed runs give empty cells, not crashes."""
        fields = extract_metrics_fields({"crewai_tokens": 100})
        assert fields["session_setup_s"] is None
        assert fields["llm_429_count"] is None
        assert fields["llm_total_s"] is None
        assert fields["llm_coverage_gap"] is None

    def test_agent_steps_is_gone_from_the_schema(self):
        """It duplicated browser_use_llm_calls exactly on 30/30 rows.
        Historical baselines still load: extract uses .get() throughout."""
        assert "agent_steps" not in CSV_COLUMNS
        assert "agent_steps" not in extract_metrics_fields(
            {"agent_diagnostics": {"agent_steps": 3}}
        )

    def test_the_step_count_survives_as_browser_use_llm_calls(self):
        """agent_steps was the ONLY isolated browser-use step count in the CSV —
        llm_calls is total_llm_calls, which conflates crewai and browser-use
        (workflow_service.py:737). Dropping agent_steps without this would lose
        the step distribution the 2026-07-26 baseline records."""
        fields = extract_metrics_fields({"browser_use_llm_calls": 3})
        assert fields["browser_use_llm_calls"] == 3
        assert "browser_use_llm_calls" in CSV_COLUMNS

    def test_new_columns_are_declared_in_csv_columns(self):
        """build_csv_row raises on unknown keys — these must move together."""
        for col in ("submit_s", "queue_s", "session_setup_s", "agent_setup_s",
                    "agent_run_s", "postprocess_s", "poll_wait_s",
                    "dom_elements_max", "dom_elements_median", "llm_429_count",
                    "retry_lost_s", "llm_total_s", "llm_max_s",
                    "llm_calls_actual", "steps_total_s", "llm_coverage_gap",
                    "browser_use_llm_calls"):
            assert col in CSV_COLUMNS

    def test_build_csv_row_accepts_the_new_fields(self):
        """End-to-end guard: extract -> build must not raise on unknown keys."""
        fields = extract_metrics_fields({
            "phase_timings": {"poll_wait_s": 0.9},
            "agent_diagnostics": {"llm_total_s": 14.2, "llm_calls_actual": 3},
        })
        row = build_csv_row(fields)
        assert row["poll_wait_s"] == 0.9
        assert row["llm_total_s"] == 14.2
        assert row["llm_calls_actual"] == 3
        assert row["session_setup_s"] == ""     # None becomes an empty cell
        assert row["llm_coverage_gap"] == ""    # unmeasured coverage is visible


class TestReportMetrics:
    """NUMERIC_METRICS and CSV_COLUMNS drift silently: a metric with no column
    reports as '-' forever and nobody notices it was never measured."""

    def test_every_reported_metric_has_a_csv_column(self):
        from bench.report import NUMERIC_METRICS

        assert set(NUMERIC_METRICS) <= set(CSV_COLUMNS)

    def test_the_agent_run_split_is_reported(self):
        from bench.report import NUMERIC_METRICS

        for metric in ("llm_total_s", "llm_max_s", "llm_calls_actual",
                       "steps_total_s", "llm_coverage_gap",
                       "browser_use_llm_calls"):
            assert metric in NUMERIC_METRICS

    def test_agent_steps_is_no_longer_reported(self):
        from bench.report import NUMERIC_METRICS

        assert "agent_steps" not in NUMERIC_METRICS

    def test_step_budget_exhausted_is_not_a_numeric_metric(self):
        """1/0/empty is a rate, not a metric — a median over it is meaningless.
        It is reported by exhaustion_counts/rate_line instead (see the
        step-budget-exhausted design doc, §5.2)."""
        from bench.report import NUMERIC_METRICS

        assert "step_budget_exhausted" not in NUMERIC_METRICS

    def test_guardrails_exclude_step_budget_exhausted(self):
        """One corpus run exhausted its step budget and still passed — a hard
        gate on this family was already shown to fail the main bench (design
        doc §5.3). GUARDRAILS must stay exactly these two."""
        from bench.report import GUARDRAILS

        assert GUARDRAILS == ("locator_success_rate", "flake_retries")


class TestAppendSchemaGate:
    """`--out` appends, so a CSV written under an older schema must be refused.

    DictWriter writes by current fieldnames and only emits a header for a new
    file. `agent_steps` was removed from the MIDDLE of CSV_COLUMNS, so
    appending one current row to an agent_steps-era baseline shifted every
    diagnostic after the removal point onto the wrong column with no error.
    """

    def _write(self, tmp_path, header):
        path = tmp_path / "candidate.csv"
        path.write_text(",".join(header) + "\n", encoding="utf-8")
        return path

    def test_matching_schema_is_allowed(self, tmp_path):
        from bench.run_bench import gate_schema

        gate_schema(self._write(tmp_path, list(CSV_COLUMNS)))

    def test_a_new_file_is_allowed(self, tmp_path):
        from bench.run_bench import gate_schema

        gate_schema(tmp_path / "does-not-exist.csv")

    def test_an_agent_steps_era_header_is_refused(self, tmp_path):
        """The exact shape that corrupted silently: agent_steps sitting
        between poll_wait_s and dom_elements_max."""
        from bench.run_bench import gate_schema

        header = list(CSV_COLUMNS)
        header.insert(header.index("dom_elements_max"), "agent_steps")
        path = self._write(tmp_path, header)
        with pytest.raises(SystemExit) as exc:
            gate_schema(path)
        assert "agent_steps" in str(exc.value)

    def test_a_pre_instrumentation_header_is_refused(self, tmp_path):
        """A 33-column baseline: every new value would land in the restkey."""
        from bench.run_bench import gate_schema

        header = [c for c in CSV_COLUMNS if c not in {
            "submit_s", "queue_s", "session_setup_s", "agent_setup_s",
            "agent_run_s", "postprocess_s", "poll_wait_s",
        }]
        path = self._write(tmp_path, header)
        with pytest.raises(SystemExit) as exc:
            gate_schema(path)
        assert "queue_s" in str(exc.value)

    def test_a_reordered_header_is_refused_and_names_the_reason(self, tmp_path):
        """Same column names, different order.

        Both membership checks come back empty here, so the generic report has
        nothing to list: it printed `(none)` on both lines and named no cause,
        which reads like the gate itself is broken. The refusal was always
        correct; only the reason was missing.
        """
        from bench.run_bench import gate_schema

        header = list(CSV_COLUMNS)
        moved = header.index("llm_429_count")
        header[moved], header[moved - 1] = header[moved - 1], header[moved]
        path = self._write(tmp_path, header)

        with pytest.raises(SystemExit) as exc:
            gate_schema(path)

        message = str(exc.value)
        assert "(none)" not in message, "the empty-list report must not be reused here"
        assert "order" in message
        # Points at the first column that actually moved, not just "somewhere".
        assert f"column {moved - 1}" in message
        assert CSV_COLUMNS[moved] in message

    def test_a_duplicated_column_is_refused_without_crashing(self, tmp_path):
        """A header can reach the same-names branch without being a reorder.

        `not in` ignores repeats, so a header carrying a duplicate column has
        no missing and no extra entries either, while being LONGER than
        CSV_COLUMNS. Zipping the two lists pairwise would run off the short
        one and find no difference at all. Guard that the gate still refuses
        and still reports, rather than dying inside its own error path.
        """
        from bench.run_bench import gate_schema

        path = self._write(tmp_path, [*CSV_COLUMNS, CSV_COLUMNS[0]])
        with pytest.raises(SystemExit) as exc:
            gate_schema(path)
        assert "fresh --out path" in str(exc.value)

    def test_an_empty_file_gets_a_header_instead_of_a_headerless_row(self, tmp_path):
        """A zero-byte --out slipped past both guards. existing_header returns
        None for it exactly as it does for a missing file, so gate_schema
        allowed it; append_row then skipped writeheader() because the path
        exists, and wrote a bare data row. The first data row is then read back
        as the header, which is the same silent misalignment gate_schema was
        added to stop.

        Writing the header is the fix rather than refusing: an empty file is
        not a schema conflict, and there is nothing in it to misalign against.
        """
        from bench.run_bench import append_row, gate_schema

        path = tmp_path / "empty.csv"
        path.write_text("", encoding="utf-8")

        gate_schema(path)
        append_row(path, {c: "" for c in CSV_COLUMNS})

        assert path.read_text(encoding="utf-8").splitlines()[0] == ",".join(CSV_COLUMNS)

    def test_a_real_pre_change_baseline_is_refused(self, tmp_path):
        """Not a synthetic header — the oldest baseline actually on disk."""
        from pathlib import Path

        from bench.run_bench import gate_schema

        baseline = Path(__file__).resolve().parents[2] / "bench" / "baselines" / "2026-07-03-baseline.csv"
        if not baseline.exists():
            pytest.skip(f"baseline absent ({baseline})")
        with pytest.raises(SystemExit):
            gate_schema(baseline)

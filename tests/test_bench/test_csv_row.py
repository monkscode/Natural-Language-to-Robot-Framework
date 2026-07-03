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

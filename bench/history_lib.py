"""Pure helpers for loading the bench sweep corpus into schema `bench`.

No I/O and no database access lives here — that is bench/load_history.py — so
every rule in this module is unit-testable without a corpus or a connection.

Referenced by: bench/load_history.py, tests/test_bench/test_history_lib.py
Depends on: nothing outside the standard library
"""
import logging
import re

logger = logging.getLogger(__name__)

# Union of all four CSV header shapes observed in bench/baselines: 33 cols
# (32 sweeps), 45 (1), 50 (2), 51 (43). 52 columns in total. `agent_steps`
# appears in exactly one sweep — it was added at shape 50 and dropped at
# shape 51, which is why forward-compatibility is not assumed anywhere here.
CANONICAL_COLUMNS: dict[str, str] = {
    "query_id": "text",
    "query": "text",
    "repeat": "int",
    "workflow_id": "text",
    "started_at": "text",
    "generation_status": "text",
    "test_status": "text",
    "dryrun_status": "text",
    "plan_s": "float",
    "identify_s": "float",
    "assemble_s": "float",
    "dryrun_s": "float",
    "exec_s": "float",
    "total_s": "float",
    "llm_calls": "int",
    "llm_tokens": "int",
    "prompt_tokens": "int",
    "completion_tokens": "int",
    "llm_cost_usd": "float",
    "total_elements": "int",
    "successful_elements": "int",
    "failed_elements": "int",
    "locator_success_rate": "float",
    "flake_retries": "int",
    "dryrun_repairs": "int",
    "cold_start_s": "float",
    "cleanup_s": "float",
    "locator_timer_count": "int",
    "locator_latency_ms_median": "float",
    "locator_latency_ms_p90": "float",
    "probe_total": "int",
    "probe_unique": "int",
    "duplicate_lookup_rate": "float",
    "submit_s": "float",
    "queue_s": "float",
    "session_setup_s": "float",
    "agent_setup_s": "float",
    "agent_run_s": "float",
    "postprocess_s": "float",
    "poll_wait_s": "float",
    "agent_steps": "int",
    "dom_elements_max": "int",
    "dom_elements_median": "float",
    "llm_429_count": "int",
    "retry_lost_s": "float",
    "llm_total_s": "float",
    "llm_max_s": "float",
    "llm_calls_actual": "int",
    "steps_total_s": "float",
    "llm_coverage_gap": "float",
    "browser_use_llm_calls": "int",
    "step_budget_exhausted": "bool",
}

# `repeat` is a Postgres function name; everything else maps to itself.
CSV_TO_SQL: dict[str, str] = {
    name: ("repeat_index" if name == "repeat" else name)
    for name in CANONICAL_COLUMNS
}

_TRUE = {"true", "1", "yes", "t"}
_FALSE = {"false", "0", "no", "f"}


def coerce(value: str | None, kind: str) -> str | int | float | bool | None:
    """CSV cell -> typed value. A blank cell is NULL, never a zero.

    1,982 of 1,984 corpus rows have a blank `dryrun_status` and 33 have blank
    timings; coercing those to 0 would render as a real measurement.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if kind == "int":
        # Some sweeps write counts as floats ("3.0"); int("3.0") raises.
        return int(float(text))
    if kind == "float":
        return float(text)
    if kind == "bool":
        lowered = text.lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        return None
    return text


def split_row(row: dict[str, str]) -> tuple[dict[str, object], dict[str, str]]:
    """Split one CSV row into typed SQL columns and unrecognised leftovers."""
    typed: dict[str, object] = {}
    extra: dict[str, str] = {}
    for key, raw in row.items():
        if key is None:
            continue
        kind = CANONICAL_COLUMNS.get(key)
        if kind is None:
            extra[key] = raw
            continue
        typed[CSV_TO_SQL[key]] = coerce(raw, kind)
    return typed, extra

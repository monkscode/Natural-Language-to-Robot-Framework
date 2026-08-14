"""Pure helpers for loading the bench sweep corpus into schema `bench`.

No I/O and no database access lives here — that is bench/load_history.py — so
every rule in this module is unit-testable without a corpus or a connection.

Referenced by: bench/load_history.py, tests/test_bench/test_history_lib.py
Depends on: nothing outside the standard library
"""
import math


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

    1,982 of 1,984 corpus rows have a blank `dryrun_status` and 33 to 34 have
    blank timings, depending on the stage column; coercing those to 0 would
    render as a real measurement.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    # Both numeric kinds degrade to NULL rather than raising, for the same
    # reason the bool branch below already returns None on an unknown spelling:
    # nothing commits until the end of load_corpus, so one bad cell — an "n/a"
    # or a "NaN%" from a future sweep — aborted the whole corpus, and
    # run_bench._load_history_best_effort then swallowed the error, leaving the
    # operator with "bench history NOT loaded" and no column named. NULL keeps
    # the drift visible in the dashboards instead of fatal at the loader. No
    # cell in the 1,984-row corpus needs this today; the loader does not
    # control what a future sweep writes.
    if kind in ("int", "float"):
        # Some sweeps write counts as floats ("3.0"); int("3.0") raises.
        try:
            number = float(text)
        except ValueError:
            return None
        # isfinite is not belt-and-braces, it is the half that matters more.
        # "inf", "Infinity" and "1e400" all parse CLEANLY to a float, so they
        # never reach the guard above: in an int column they then raise
        # OverflowError out of int(), and in a float column they reach Postgres
        # as real values and poison every avg() over that column for the whole
        # sweep — measured, avg(40.5, 41.2, Infinity) is Infinity, and NaN
        # likewise. A corrupt cell renders as a plausible-looking timing rather
        # than as the gap it is, which is the worse of the two failures.
        if not math.isfinite(number):
            return None
        return int(number) if kind == "int" else number
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


# A sweep whose workflow_id set overlaps an already-loaded sweep by at least
# this much is a re-rendering of it, not a new experiment. The three real
# cases in the corpus overlap at 1.0; the threshold leaves room for a sweep
# that was resumed rather than re-costed.
DERIVED_THRESHOLD = 0.9

_EXPECTED = {"public": 30, "astpp": 18}


def normalise_dryrun(generation_status: str | None,
                     csv_cell: str | None,
                     recorded: str | None) -> str:
    """Resolve the dryrun outcome once, here, so no panel author can get it wrong.

    Branch order, and why each branch exists:

    1. A value recorded on the captured metrics row is ground truth. 60 of the
       1,950 rows that carry a captured payload carry one, and all 60 read
       `passed`.
    2. A non-blank CSV cell is the next best evidence. Only `failed` ever
       appears there, on 2 of 1,984 rows.
    3. `generation_status = 'error'` means the run never reached the gate.
       All 34 such rows have a blank cell, and none carries a recorded value,
       so this branch and branch 1 are disjoint on the real corpus. Calling
       these `passed` would invent a gate result.
    4. Blank on a completed run means PASSED. workflow_service.py:889 attaches
       dryrun_status to the SSE `complete` event only when the gate did not
       pass, so silence is success — for 1,982 of 1,984 rows.

    Never returns NULL and never returns 'unknown': rendering blank as
    "unknown" would invert the pipeline's main quality gate on almost the
    whole corpus.
    """
    if recorded:
        return recorded
    cell = (csv_cell or "").strip()
    if cell:
        return cell
    if (generation_status or "").strip() == "error":
        return "not_reached"
    return "passed"


def sweep_family(query_ids: list[str]) -> str:
    """`public` or `astpp`. Verified: no CSV mixes the two."""
    if any((q or "").startswith("astpp") for q in query_ids):
        return "astpp"
    return "public"


def expected_count(family: str) -> int:
    return _EXPECTED[family]


def is_flagged_invalid(sweep_name: str) -> bool:
    """The owner's manual quarantine marker, carried in the filename."""
    return sweep_name.upper().startswith("INVALID")


def overlap_ratio(ids_a: set[str], ids_b: set[str]) -> float:
    """Share of the smaller sweep's run ids that also appear in the larger.

    Blank ids are excluded by the caller. Returns 0.0 rather than dividing by
    zero when either side is empty.
    """
    if not ids_a or not ids_b:
        return 0.0
    return len(ids_a & ids_b) / min(len(ids_a), len(ids_b))


def pick_parent(name_a: str, name_b: str, captured_a, captured_b) -> str:
    """Of two sweeps that describe the same runs, which is the original.

    Earlier capture wins. The three real derived pairs have no `.meta.json`
    and fall back to file mtime, which can tie or even invert after a copy, so
    a tie breaks to the shorter name: in all three cases the derived file
    appends a suffix to its parent's name.
    """
    if captured_a != captured_b:
        return name_a if captured_a < captured_b else name_b
    if len(name_a) != len(name_b):
        return name_a if len(name_a) < len(name_b) else name_b
    return min(name_a, name_b)

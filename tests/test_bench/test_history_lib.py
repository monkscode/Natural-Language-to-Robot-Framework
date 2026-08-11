"""Unit tests for bench/history_lib.py and the bench schema DDL.

Referenced by: nothing — pytest entry point.
Depends on: bench/history_lib.py, observability/postgres/create_bench_schema.sql
"""
import re
from pathlib import Path

from bench import history_lib

DDL = Path("observability/postgres/create_bench_schema.sql")


def _ddl_columns(table: str) -> set[str]:
    """Column names declared inside one CREATE TABLE block of the DDL."""
    sql = DDL.read_text(encoding="utf-8")
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {re.escape(table)}\s*\((.*?)\n\);",
        sql, re.IGNORECASE | re.DOTALL,
    )
    assert match, f"{DDL.name} has no CREATE TABLE for {table}"
    cols = set()
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("--"):
            continue
        if re.match(r"^(PRIMARY|FOREIGN|UNIQUE|CHECK|CONSTRAINT)\b", line, re.IGNORECASE):
            continue
        ident = re.match(r"^([a-z_][a-z0-9_]*)\b", line)
        if ident:
            cols.add(ident.group(1))
    return cols


def test_ddl_is_idempotent_by_construction():
    """Every object the DDL creates must be guarded, because grafana-db-init
    runs this file on every `docker compose --profile observability up` under
    ON_ERROR_STOP=1. One unguarded CREATE aborts the init container, and
    Grafana then never starts."""
    sql = DDL.read_text(encoding="utf-8")
    creates = re.findall(r"CREATE\s+(SCHEMA|TABLE|INDEX)\s+(IF NOT EXISTS)?", sql,
                         re.IGNORECASE)
    assert creates, "DDL creates nothing"
    for kind, guard in creates:
        assert guard, f"CREATE {kind} without IF NOT EXISTS is not idempotent"


def test_sweeps_table_has_the_provenance_columns():
    cols = _ddl_columns("bench.sweeps")
    assert {"sweep_name", "captured_at", "captured_at_source", "family",
            "run_count", "expected_count", "is_complete", "is_flagged_invalid",
            "derived_from", "pins", "git_sha", "git_branch",
            "header_shape"} <= cols


def test_runs_primary_key_is_query_and_repeat_not_workflow_id():
    """3 corpus rows carry a blank workflow_id (all generation errors), so
    (sweep_name, workflow_id) is unique only by accident of which sweeps they
    landed in. (sweep_name, query_id, repeat) was verified unique across all
    78 files and 1,984 rows."""
    sql = DDL.read_text(encoding="utf-8")
    match = re.search(r"PRIMARY KEY\s*\(([^)]*)\)", sql, re.IGNORECASE)
    assert match, "bench.runs declares no PRIMARY KEY"
    key = [c.strip() for c in match.group(1).split(",")]
    assert key == ["sweep_name", "query_id", "repeat_index"], key


def test_runs_table_carries_the_normalised_dryrun_column():
    assert "dryrun_status_normalised" in _ddl_columns("bench.runs")


def test_canonical_columns_cover_the_union_of_all_four_header_shapes():
    """52 columns across shapes 33/45/50/51. `agent_steps` is in exactly one
    sweep — added at shape 50, dropped at shape 51 — and must still be typed,
    because a dropped-then-renamed column is the drift this guards against."""
    assert len(history_lib.CANONICAL_COLUMNS) == 52
    assert history_lib.CANONICAL_COLUMNS["agent_steps"] == "int"
    assert history_lib.CANONICAL_COLUMNS["query_id"] == "text"
    assert history_lib.CANONICAL_COLUMNS["step_budget_exhausted"] == "bool"


def test_repeat_is_renamed_because_it_is_a_postgres_function():
    assert history_lib.CSV_TO_SQL["repeat"] == "repeat_index"
    assert history_lib.CSV_TO_SQL["query_id"] == "query_id"


def test_every_canonical_column_has_a_sql_name():
    assert set(history_lib.CSV_TO_SQL) == set(history_lib.CANONICAL_COLUMNS)


def test_blank_cells_become_null_not_zero():
    """1,982 of 1,984 rows have a blank dryrun_status and 33 of 1,984 have
    blank timings. A blank coerced to 0 would read as a real measurement."""
    assert history_lib.coerce("", "float") is None
    assert history_lib.coerce("   ", "int") is None
    assert history_lib.coerce(None, "text") is None


def test_numeric_coercion():
    assert history_lib.coerce("12.5", "float") == 12.5
    # Counts are written by some sweeps as floats; int("3.0") raises.
    assert history_lib.coerce("3.0", "int") == 3
    assert history_lib.coerce("3", "int") == 3


def test_boolean_coercion_accepts_the_spellings_the_corpus_uses():
    assert history_lib.coerce("True", "bool") is True
    assert history_lib.coerce("true", "bool") is True
    assert history_lib.coerce("False", "bool") is False
    assert history_lib.coerce("", "bool") is None


def test_unknown_columns_are_routed_to_extra_not_dropped():
    """Schema drift is not monotone — shape45 is NOT a subset of shape50 — so
    a future rename must land somewhere visible rather than vanish."""
    row = {"query_id": "q01", "repeat": "1", "total_s": "40.5",
           "brand_new_column": "surprise"}
    typed, extra = history_lib.split_row(row)
    assert typed["query_id"] == "q01"
    assert typed["repeat_index"] == 1
    assert typed["total_s"] == 40.5
    assert extra == {"brand_new_column": "surprise"}


def test_known_columns_never_leak_into_extra():
    row = {c: "" for c in history_lib.CANONICAL_COLUMNS}
    _, extra = history_lib.split_row(row)
    assert extra == {}


def test_every_canonical_column_exists_in_the_ddl():
    """A column typed in Python but missing from the DDL fails at INSERT time
    against a real database, which no unit test would otherwise reach."""
    ddl_cols = _ddl_columns("bench.runs")
    missing = set(history_lib.CSV_TO_SQL.values()) - ddl_cols
    assert not missing, f"DDL is missing columns: {sorted(missing)}"

"""Unit tests for bench/history_lib.py and the bench schema DDL.

Referenced by: nothing — pytest entry point.
Depends on: bench/history_lib.py, observability/postgres/create_bench_schema.sql
"""
import re
from pathlib import Path

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

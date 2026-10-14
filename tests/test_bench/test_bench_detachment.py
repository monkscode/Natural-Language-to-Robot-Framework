"""The bench must leave no test rows behind.

Bench data reaching the Tests page is the detachment failure run_bench.py's
docstring exists to prevent.
"""
import inspect
import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from src.backend.core.config import settings


def test_the_detach_step_deletes_the_tests_it_creates():
    import bench.run_bench as rb
    src = inspect.getsource(rb)
    assert "DELETE FROM tests" in src, (
        "run_bench must delete the tests rows a bench run creates")


def test_the_capture_step_saves_the_new_tables_as_evidence():
    import bench.run_bench as rb
    src = inspect.getsource(rb)
    assert "tests.json" in src and "test_versions.json" in src


def test_the_docstring_still_describes_what_is_detached():
    import bench.run_bench as rb
    assert "test_versions" in rb.__doc__


@pytest.fixture
def schema():
    """The REAL tests/test_runs DDL in a throwaway schema, plus the two
    tables detach_run also clears. The FK cascade is the point, so it has to
    be the constraint the app actually creates."""
    name = f"bench_detach_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    admin.execute(f"CREATE SCHEMA {name}")
    sep = "&" if "?" in settings.DATABASE_URL else "?"
    dsn = settings.DATABASE_URL + f"{sep}options=-c%20search_path%3D{name}"
    from src.backend.core.run_registry import RunRegistry
    RunRegistry(dsn=dsn).close()
    admin.execute(f"CREATE TABLE {name}.workflow_metrics (workflow_id TEXT)")
    admin.execute(f"CREATE TABLE {name}.llm_traces (workflow_id TEXT)")
    conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    try:
        yield name, conn, admin
    finally:
        conn.close()
        admin.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
        admin.close()


def _share_one_test(conn, run_ids):
    conn.execute(
        "INSERT INTO tests (test_id, org_id, key_n, user_id, user_query,"
        " current_version) VALUES ('t-1', NULL, 1, 'u1', 'q', 1)")
    conn.execute(
        "INSERT INTO test_versions (version_id, test_id, n, user_query,"
        " robot_code) VALUES ('v-1', 't-1', 1, 'q', 'code')")
    for rid in run_ids:
        conn.execute(
            "INSERT INTO test_runs (run_id, user_id, user_query, status,"
            " test_id, test_version_id)"
            " VALUES (%s, 'u1', 'q', 'passed', 't-1', 'v-1')", (rid,))


@pytest.mark.integration
def test_detaching_a_run_cannot_destroy_a_run_that_shares_its_test(schema):
    """fk_test_runs_test is ON DELETE CASCADE on the REFERENCING side, so
    deleting a `tests` row deletes EVERY test_runs row pointing at it.
    Deletion order protects nothing: the bench run's own row is already gone,
    and the cascade still reaches the run that stayed behind.

    Two runs already share a test today wherever one is a re-run of the
    other: _attach_test's rerun branch returns the source's test_id. The
    bench never sets rerun_of, so its own runs are never the sharer — but
    that is a property of the bench, not of the schema, and a bench sweep
    must not be able to delete a user's history the day it changes.
    """
    name, conn, admin = schema
    _share_one_test(conn, ["bench-1", "keep-me"])

    from bench.run_bench import detach_run
    detach_run(conn, "bench-1")

    assert admin.execute(
        f"SELECT count(*) FROM {name}.test_runs WHERE run_id = 'keep-me'"
    ).fetchone()[0] == 1, "the cascade destroyed a run the bench never made"
    assert admin.execute(
        f"SELECT count(*) FROM {name}.tests").fetchone()[0] == 1, (
        "a test another run still points at must survive")


@pytest.mark.integration
def test_detaching_the_last_run_of_a_test_still_removes_the_test(schema):
    """The guard must not turn detachment off: with no run left pointing at
    it, the test (and its versions) still go, or bench data reaches the
    Tests page — the failure run_bench's docstring exists to prevent."""
    name, conn, admin = schema
    _share_one_test(conn, ["bench-1"])

    from bench.run_bench import detach_run
    detach_run(conn, "bench-1")

    assert admin.execute(
        f"SELECT count(*) FROM {name}.tests").fetchone()[0] == 0
    assert admin.execute(
        f"SELECT count(*) FROM {name}.test_versions").fetchone()[0] == 0

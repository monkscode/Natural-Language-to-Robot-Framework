"""list_runs' ORDER BY t.created_at DESC has no tiebreaker (F11).

created_at DEFAULT now() is TRANSACTION time, so batch-recorded rows can tie
exactly, and OFFSET paging over a non-total order can then skip or repeat
rows across pages: whichever arbitrary order two separate query executions
happen to agree on is not a contract, and anything that perturbs the table's
physical layout between page reads (autovacuum, CLUSTER, a plan change) can
change it. A run_id tiebreaker (the primary key, so always unique) turns the
ORDER BY into a total order that OFFSET paging can rely on regardless of
physical layout.

Referenced by: none (regression test only).
Depends on: src/backend/core/run_registry.py (list_runs).
"""

import uuid

import psycopg
import pytest

from src.backend.core.config import settings
from src.backend.core.run_registry import RunRegistry

pytestmark = pytest.mark.integration

_SCHEMA = "run_registry_tiebreak_test"


@pytest.fixture
def reg():
    """Isolated RunRegistry on its own throwaway schema (never touches the
    live/shared schema — this test deliberately CLUSTERs the table to force a
    physical reorder, which must not happen to data other tests depend on)."""
    admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    admin.execute(f"CREATE SCHEMA {_SCHEMA}")
    sep = "&" if "?" in settings.DATABASE_URL else "?"
    dsn = settings.DATABASE_URL + f"{sep}options=-c%20search_path%3D{_SCHEMA},public"
    r = RunRegistry(dsn=dsn)
    yield r
    r.close()
    admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    admin.close()


def test_paging_over_exactly_tied_created_at_neither_skips_nor_repeats(reg):
    """Seed 6 rows with an EXACT created_at tie (a real shape: batch-recorded
    rows share transaction time). Read page 1, then CLUSTER the table by
    primary key — a legitimate maintenance operation (autovacuum can do the
    same to a table's physical layout over time) that changes the heap order
    the untied query's tie-break silently rode on — then read page 2. A total
    order must agree with itself across that disruption; an order that is
    merely "whatever the last physical layout happened to produce" will not.
    """
    ids = [str(uuid.uuid4()) for _ in range(6)]
    for rid in ids:
        reg.record_start(rid, None, "tied row", "passed")
    with reg._pool.connection() as conn:
        conn.execute(
            "UPDATE test_runs SET created_at = now() WHERE run_id = ANY(%s)",
            (ids,),
        )

    page1, total = reg.list_runs(limit=3, offset=0)
    assert total == 6
    page1_ids = [r["run_id"] for r in page1]

    with reg._pool.connection() as conn:
        conn.execute(f"CLUSTER {_SCHEMA}.test_runs USING test_runs_pkey")
        conn.execute(f"ANALYZE {_SCHEMA}.test_runs")

    page2, _ = reg.list_runs(limit=3, offset=3)
    page2_ids = [r["run_id"] for r in page2]

    seen = page1_ids + page2_ids
    assert len(seen) == len(set(seen)), f"paging repeated a row: {seen}"
    assert set(seen) == set(ids), (
        f"paging skipped a row: missing {set(ids) - set(seen)}")

"""F2: trace dashboard reads use a POOLED connection, not a fresh connect per request.

Proves the pool recycles the underlying psycopg connection across sequential
checkouts (the old _get_db opened+closed a brand-new connection every request).
Runs against an isolated schema so it never touches the live public trace data.

Depends on: src/backend/api/trace_endpoints.py (_make_read_pool, _PooledCompatConnection),
src/backend/core/trace_store.py (_ensure_schema).
"""

import uuid

import psycopg
import pytest

pytestmark = [pytest.mark.integration]

_SCHEMA = "trace_pool_test"


@pytest.fixture
def trace_dsn():
    from src.backend.core.config import settings
    from src.backend.core.trace_store import _ensure_schema

    admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    admin.execute(f"CREATE SCHEMA {_SCHEMA}")
    sep = "&" if "?" in settings.DATABASE_URL else "?"
    dsn = settings.DATABASE_URL + f"{sep}options=-c%20search_path%3D{_SCHEMA}"
    conn = psycopg.connect(dsn)
    try:
        _ensure_schema(conn)
    finally:
        conn.close()
    yield dsn
    admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    admin.close()


def test_read_pool_recycles_connections(trace_dsn):
    """close() returns the connection to the pool (does NOT close it), and across
    many sequential checkouts the pool recycles a bounded set of connections —
    proving requests no longer pay a fresh connect each time."""
    from src.backend.api import trace_endpoints as te

    pool = te._make_read_pool(trace_dsn)  # max_size=4
    try:
        # 1) close() must recycle, not close, the underlying connection.
        c1 = te._PooledCompatConnection(pool)
        raw1 = c1._real
        assert c1.execute("SELECT 1 AS x").fetchone()["x"] == 1  # compat rows work
        c1.close()
        assert not raw1.closed, "close() must return the conn to the pool, not close it"

        # 2) 20 sequential checkout/close cycles must reuse a bounded set of
        #    connections (<= max_size), not open 20 fresh ones.
        seen = set()
        for _ in range(20):
            c = te._PooledCompatConnection(pool)
            assert c.execute("SELECT 1 AS x").fetchone()["x"] == 1
            seen.add(id(c._real))
            c.close()
        assert len(seen) <= 4, f"pool must recycle (<= max_size); saw {len(seen)} distinct"
        assert len(seen) < 20, "a fresh connect per call would yield 20 distinct conns"
    finally:
        pool.close()

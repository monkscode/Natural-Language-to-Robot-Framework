"""Postgres-backed fixtures for the trace-store tests (Phase 4 consolidation).

The trace store moved from SQLite to Postgres, so these tests run against an
isolated `trace_test` schema with a per-test TRUNCATE, mirroring the other
Postgres test conftests.
"""

import psycopg
import pytest

from src.backend.core.config import settings

_TRACE_SCHEMA = "trace_test"


@pytest.fixture(scope="session")
def _trace_session():
    from src.backend.core.trace_store import PostgresSpanExporter

    admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    admin.execute(f"DROP SCHEMA IF EXISTS {_TRACE_SCHEMA} CASCADE")
    admin.execute(f"CREATE SCHEMA {_TRACE_SCHEMA}")
    admin.execute(f"SET search_path TO {_TRACE_SCHEMA}")
    dsn = settings.DATABASE_URL + f"?options=-c%20search_path%3D{_TRACE_SCHEMA}"
    store = PostgresSpanExporter(dsn=dsn)
    yield store, admin, dsn
    store.shutdown()
    admin.execute(f"DROP SCHEMA IF EXISTS {_TRACE_SCHEMA} CASCADE")
    admin.close()


@pytest.fixture
def trace_store(_trace_session):
    """Shared PostgresSpanExporter on an isolated schema, truncated per test."""
    store, admin, _dsn = _trace_session
    admin.execute("TRUNCATE llm_traces")
    return store


@pytest.fixture
def trace_query(_trace_session):
    """Run a read query against the trace test schema; returns fetched rows."""
    _store, admin, _dsn = _trace_session

    def _q(sql, params=()):
        return admin.execute(sql, params).fetchall()

    return _q


@pytest.fixture
def trace_dsn(_trace_session):
    _store, _admin, dsn = _trace_session
    return dsn

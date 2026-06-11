"""Postgres-backed fixtures for the learning-endpoint API tests (Phase 4).

The learning dashboard endpoints became Postgres-only at slice 4.5 (native jsonb
KPI queries), so these tests run against a real PostgreSQL schema instead of the
SQLite ExecutionMemory. Mirrors tests/test_optimization/conftest.py: one
session-scoped PostgresExecutionMemory on an isolated schema, truncated per test.
"""

from unittest.mock import patch

import pytest

_API_PG_SCHEMA = "learning_api_test"


@pytest.fixture(autouse=True)
def _auth_not_enforced():
    """These tests exercise endpoint logic, not auth — disable JWT enforcement.

    The guard security matrix (401/403 per flag and role) is covered by
    tests/test_auth/test_guards.py.
    """
    from src.backend.core.config import settings
    with patch.object(settings, "AUTH_ENFORCED", False):
        yield
_API_PG_TABLES = (
    "execution_records", "intent_patterns", "structural_rules", "keyword_corrections",
    "anti_patterns", "learning_stats", "learning_metrics", "nl_feedback_corrections",
    "trigger_events", "hint_audit", "hint_review_sessions", "hint_review_recommendations",
    "hint_review_pages", "hint_workflow_trace", "learning_anchors", "execution_embeddings",
)


@pytest.fixture(scope="session")
def _api_pg_admin():
    import psycopg
    from src.backend.core.config import settings
    conn = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def _api_pg_em(_api_pg_admin):
    """One PostgresExecutionMemory bound to an isolated test schema for the
    whole session; each test truncates it for a clean slate."""
    from src.backend.core.config import settings
    from src.backend.crew_ai.optimization.postgres_execution_memory import PostgresExecutionMemory

    _api_pg_admin.execute(f"DROP SCHEMA IF EXISTS {_API_PG_SCHEMA} CASCADE")
    _api_pg_admin.execute(f"CREATE SCHEMA {_API_PG_SCHEMA}")
    # public is on the path so the pgvector `vector` type (installed in public) resolves.
    dsn = settings.DATABASE_URL + f"?options=-c%20search_path%3D{_API_PG_SCHEMA},public"
    em = PostgresExecutionMemory(dsn=dsn)
    em._chroma_client = PostgresExecutionMemory._CHROMADB_INIT_FAILED
    yield em, dsn
    em.close()
    _api_pg_admin.execute(f"DROP SCHEMA IF EXISTS {_API_PG_SCHEMA} CASCADE")


@pytest.fixture
def api_pg_em(_api_pg_em, _api_pg_admin):
    """Clean PostgresExecutionMemory + its DSN per test (ChromaDB disabled)."""
    em, dsn = _api_pg_em
    try:
        em._writer_conn.rollback()  # clear any aborted txn from a prior test
    except Exception:
        pass
    truncate = (
        "TRUNCATE "
        + ", ".join(f"{_API_PG_SCHEMA}.{t}" for t in _API_PG_TABLES)
        + " RESTART IDENTITY CASCADE"
    )
    import psycopg
    try:
        _api_pg_admin.execute(truncate)
    except psycopg.errors.UndefinedTable:
        from src.backend.crew_ai.optimization import pg_schema
        raw = psycopg.connect(dsn, autocommit=True)
        try:
            pg_schema.ensure_schema(raw)
        finally:
            raw.close()
        _api_pg_admin.execute(truncate)
    from src.backend.crew_ai.optimization.postgres_execution_memory import PostgresExecutionMemory
    em._chroma_client = PostgresExecutionMemory._CHROMADB_INIT_FAILED
    saved_attrs = dict(em.__dict__)
    yield em, dsn
    em.__dict__.clear()
    em.__dict__.update(saved_attrs)

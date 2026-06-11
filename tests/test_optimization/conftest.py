"""
Shared fixtures for optimization integration tests.

Provides reusable database connections, temporary directories,
and helper factories used across all test_day*.py files.
"""

import os
import sqlite3
import shutil
import tempfile
import threading
from unittest.mock import patch, MagicMock

import pytest

from src.backend.crew_ai.optimization.learning_config import WRITER_THREAD_NAME
from src.backend.crew_ai.optimization.schema_manager import SchemaManager


@pytest.fixture(autouse=True)
def _isolate_keyword_store():
    """Prevent a real FeedbackLoop construction from building the Postgres-backed
    KeywordVectorStore, which would write learned query patterns to the LIVE
    public schema (test pollution / non-isolation). Tests that exercise the
    keyword store inject their own mock store, so this never affects them.

    `_singleton` is patched to None as well: get_keyword_vector_store() caches
    its first instance process-wide, so without the reset a MagicMock cached by
    one test would be handed to every later test (or a real store cached before
    the class patch would bypass it entirely).
    """
    from src.backend.crew_ai.optimization import keyword_vector_store as kvs
    with patch.object(kvs, "KeywordVectorStore", MagicMock()), \
         patch.object(kvs, "_singleton", None):
        yield


class _EngineCompatConn:
    """Satisfies both sqlite3.Connection and ExecutionMemory interfaces.

    Tests pass this as both a raw connection (via .execute/.commit/.rollback)
    and as an ExecutionMemory substitute (via ._writer_conn and .read_conn()).
    This lets all 692+ in_memory_db fixture references work without changes
    while the engines now expect ExecutionMemory, not sqlite3.Connection.
    """

    def __init__(self, writer_conn, em):
        self._writer_conn = writer_conn
        self._em = em

    def execute(self, sql, params=()):
        return self._writer_conn.execute(sql, params)

    def commit(self):
        return self._writer_conn.commit()

    def rollback(self):
        return self._writer_conn.rollback()

    def read_conn(self):
        return self._em.read_conn()

    def filter_by_query_similarity(self, *args, **kwargs):
        return self._em.filter_by_query_similarity(*args, **kwargs)

    def add_anchor(self, *args, **kwargs):
        return self._em.add_anchor(*args, **kwargs)


@pytest.fixture(scope="session", autouse=True)
def enable_optimization_for_tests():
    """Force OPTIMIZATION_ENABLED=True for the entire optimization test suite.

    In CI (GitHub Actions) no .env file is present, so OPTIMIZATION_ENABLED
    defaults to False.  Every optimization test expects the learning system to
    be active, so we patch the live settings object here instead of requiring
    each test to patch it individually.

    Scope is 'session' so the patch is applied once and remains for all tests
    in this package.  The original value is restored when the session ends.
    """
    from src.backend.core.config import settings

    original = settings.OPTIMIZATION_ENABLED
    settings.OPTIMIZATION_ENABLED = True
    yield
    settings.OPTIMIZATION_ENABLED = original


@pytest.fixture(autouse=True)
def _disable_holdout_by_default():
    """Force the R7 random-holdout off for optimization tests.

    SmartKeywordProvider rolls a holdout coin in __init__; left at the
    production 5% rate it would suppress hints on ~5% of test runs and make
    hint-dependent tests flaky. Setting the rate to 0.0 makes the decision
    deterministically False. Tests that exercise the holdout set
    provider._holdout_decision = True explicitly after construction.
    """
    from src.backend.crew_ai.optimization.smart_keyword_provider import (
        SmartKeywordProvider,
    )
    original = SmartKeywordProvider._HOLDOUT_RATE
    SmartKeywordProvider._HOLDOUT_RATE = 0.0
    yield
    SmartKeywordProvider._HOLDOUT_RATE = original


@pytest.fixture(autouse=True)
def _rename_test_thread_to_writer():
    """Rename the test thread to WRITER_THREAD_NAME for the duration of each test.

    Engine write methods call _assert_writer_thread() as defense-in-depth.
    Tests call those methods directly from the pytest thread, so we rename it
    to satisfy the assertion without relaxing the production guard.
    """
    old_name = threading.current_thread().name
    threading.current_thread().name = WRITER_THREAD_NAME
    yield
    threading.current_thread().name = old_name


_PG_TEST_SCHEMA = "learning_test"
_PG_TABLES = (
    "execution_records", "intent_patterns", "structural_rules", "keyword_corrections",
    "anti_patterns", "learning_stats", "learning_metrics", "nl_feedback_corrections",
    "trigger_events", "hint_audit", "hint_review_sessions", "hint_review_recommendations",
    "hint_review_pages", "hint_workflow_trace", "learning_anchors", "execution_embeddings",
)


@pytest.fixture(scope="session")
def _pg_admin():
    import psycopg
    from src.backend.core.config import settings
    conn = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def _pg_test_em(_pg_admin):
    """One PostgresExecutionMemory bound to an isolated test schema for the
    whole session; each test truncates it for a clean slate (Phase 4 slice 4)."""
    from src.backend.core.config import settings
    from src.backend.crew_ai.optimization.postgres_execution_memory import PostgresExecutionMemory

    _pg_admin.execute(f"DROP SCHEMA IF EXISTS {_PG_TEST_SCHEMA} CASCADE")
    _pg_admin.execute(f"CREATE SCHEMA {_PG_TEST_SCHEMA}")
    # public is on the path so the pgvector `vector` type (installed in public) resolves.
    dsn = settings.DATABASE_URL + f"?options=-c%20search_path%3D{_PG_TEST_SCHEMA},public"
    em = PostgresExecutionMemory(dsn=dsn)
    em._chroma_client = PostgresExecutionMemory._CHROMADB_INIT_FAILED
    yield em
    em.close()
    _pg_admin.execute(f"DROP SCHEMA IF EXISTS {_PG_TEST_SCHEMA} CASCADE")


@pytest.fixture
def in_memory_em(_pg_test_em, _pg_admin):
    """PostgresExecutionMemory with a clean schema per test (ChromaDB disabled).

    The store is session-scoped (one connection/pool for the whole suite), so a
    test that monkeypatches an instance attribute (e.g. ``em.store = boom``) would
    leak into every later test. We snapshot the instance ``__dict__`` here and
    restore it on teardown, giving each test the per-test isolation the old
    fresh-SQLite-ExecutionMemory fixture provided.
    """
    try:
        _pg_test_em._writer_conn.rollback()  # clear any aborted txn from a prior test
    except Exception:
        pass
    _truncate = (
        "TRUNCATE "
        + ", ".join(f"{_PG_TEST_SCHEMA}.{t}" for t in _PG_TABLES)
        + " RESTART IDENTITY CASCADE"
    )
    import psycopg
    try:
        _pg_admin.execute(_truncate)
    except psycopg.errors.UndefinedTable:
        # A prior test dropped a table from the shared session schema (e.g. the
        # trace-failure test DROPs hint_workflow_trace). Rebuild and retry.
        from src.backend.crew_ai.optimization import pg_schema
        raw = psycopg.connect(_pg_test_em.dsn, autocommit=True)
        try:
            pg_schema.ensure_schema(raw)
        finally:
            raw.close()
        _pg_admin.execute(_truncate)
    from src.backend.crew_ai.optimization.postgres_execution_memory import PostgresExecutionMemory
    _pg_test_em._chroma_client = PostgresExecutionMemory._CHROMADB_INIT_FAILED
    saved_attrs = dict(_pg_test_em.__dict__)
    yield _pg_test_em
    _pg_test_em.__dict__.clear()
    _pg_test_em.__dict__.update(saved_attrs)


@pytest.fixture
def in_memory_db(in_memory_em):
    """Fresh database satisfying both sqlite3.Connection and ExecutionMemory interfaces.

    Returns an _EngineCompatConn wrapping in_memory_em so that:
    - conn.execute() / .commit() / .rollback() work (sqlite3.Connection API)
    - conn._writer_conn and conn.read_conn() work (ExecutionMemory API)
    """
    return _EngineCompatConn(in_memory_em._writer_conn, in_memory_em)


@pytest.fixture
def tmp_dir():
    """Create a temporary directory, removed after the test."""
    path = tempfile.mkdtemp(prefix="test_optimization_")
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def tmp_db_path(tmp_dir):
    """Return a path to a temporary SQLite database file."""
    return os.path.join(tmp_dir, "test.db")

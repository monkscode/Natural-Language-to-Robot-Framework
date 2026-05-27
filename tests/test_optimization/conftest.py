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

import pytest

from src.backend.crew_ai.optimization.learning_config import WRITER_THREAD_NAME
from src.backend.crew_ai.optimization.schema_manager import SchemaManager


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


@pytest.fixture
def in_memory_em(tmp_path):
    """File-backed ExecutionMemory for tests.

    Uses a real temp file (not :memory:) so read_conn() — which opens a
    separate connection — sees the same data that _writer_conn wrote.
    ChromaDB is disabled (sentinel set) to keep tests fast.
    """
    from src.backend.crew_ai.optimization.execution_memory import ExecutionMemory

    db_path = str(tmp_path / "test_execution_memory.db")
    em = ExecutionMemory(db_path=db_path)
    em._chroma_client = ExecutionMemory._CHROMADB_INIT_FAILED
    yield em
    em.close()


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

"""
Shared fixtures for optimization integration tests.

Provides reusable database connections, temporary directories,
and helper factories used across all test_day*.py files.
"""

import os
import sqlite3
import shutil
import tempfile

import pytest

from src.backend.crew_ai.optimization.schema_manager import SchemaManager


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


@pytest.fixture
def in_memory_db():
    """Create a fresh in-memory SQLite database with schema applied.

    Yields an open connection; closed automatically after the test.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    SchemaManager.ensure_current(conn)
    yield conn
    conn.close()


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

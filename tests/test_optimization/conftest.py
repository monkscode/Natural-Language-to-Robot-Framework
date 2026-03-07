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

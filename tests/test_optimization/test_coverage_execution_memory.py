"""
Coverage tests for execution_memory.py — targeting 22 missing lines.

Tests:
- _init_chromadb: cooldown not elapsed (returns early without retry)
- _init_chromadb: cooldown expired (clears sentinel, retries)
- _init_chromadb: wrong hnsw:space on learning_anchors → RuntimeError → FAILED
- _store_sqlite: IntegrityError on a non-passing re-run re-raises
- get(): missing row returns None
- close(): safe when _writer_conn is not set (edge-case init failure)
"""

import sqlite3
import time
from unittest.mock import MagicMock, patch

import pytest

from src.backend.crew_ai.optimization.execution_memory import ExecutionMemory, ExecutionRecord
from datetime import datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_em(tmp_path):
    db_path = str(tmp_path / "em.db")
    em = ExecutionMemory(db_path=db_path)
    # Disable ChromaDB so tests run offline
    em._chroma_client = ExecutionMemory._CHROMADB_INIT_FAILED
    em._chroma_failed_at = None   # permanent disable (no retry clock)
    return em


def _make_record(workflow_id: str = "wf-1", status: str = "passed") -> ExecutionRecord:
    return ExecutionRecord(
        workflow_id=workflow_id,
        timestamp=datetime.now(),
        user_query="test query",
        url="https://example.com",
        domain="example.com",
        robot_code="*** Test Cases ***\nT\n    Log    hi",
        test_status=status,
    )


# ---------------------------------------------------------------------------
# _init_chromadb — cooldown not elapsed
# ---------------------------------------------------------------------------

class TestInitChromaDbCooldown:
    def test_retry_suppressed_while_cooldown_active(self, tmp_path):
        """After a failed init, a second call within the cooldown window does
        not re-attempt the import and leaves the client as the FAILED sentinel."""
        em = _make_em(tmp_path)

        # Simulate a prior failure with a fresh cooldown clock
        em._chroma_client = ExecutionMemory._CHROMADB_INIT_FAILED
        em._chroma_failed_at = time.monotonic()   # failed "just now"
        em._CHROMA_RETRY_COOLDOWN_S = 9999        # very long cooldown

        with patch("chromadb.PersistentClient") as mock_client:
            em._init_chromadb()  # should return early, no import attempt

        mock_client.assert_not_called()
        assert em._chroma_client is ExecutionMemory._CHROMADB_INIT_FAILED

    def test_retry_attempted_after_cooldown_expires(self, tmp_path):
        """After cooldown expires, _init_chromadb clears the sentinel and tries again."""
        em = _make_em(tmp_path)
        em._chroma_client = ExecutionMemory._CHROMADB_INIT_FAILED
        em._chroma_failed_at = time.monotonic() - 9999  # failed a long time ago
        em._CHROMA_RETRY_COOLDOWN_S = 1               # 1-second cooldown (already expired)

        with patch("chromadb.PersistentClient") as mock_client:
            mock_client.side_effect = RuntimeError("chromadb unavailable")
            em._init_chromadb()  # should attempt and fail again

        mock_client.assert_called_once()
        # Still FAILED (retry also failed), but _chroma_failed_at was updated
        assert em._chroma_client is ExecutionMemory._CHROMADB_INIT_FAILED
        assert em._chroma_failed_at is not None


# ---------------------------------------------------------------------------
# _init_chromadb — wrong hnsw:space on learning_anchors
# ---------------------------------------------------------------------------

class TestInitChromaDbWrongSpace:
    def test_wrong_hnsw_space_sets_failed_sentinel(self, tmp_path):
        """If learning_anchors was created with a non-cosine space, RuntimeError
        is raised inside _init_chromadb and the client is set to FAILED."""
        em = _make_em(tmp_path)
        em._chroma_client = None   # not yet initialized

        mock_exec_coll = MagicMock()
        mock_anchors_coll = MagicMock()
        mock_anchors_coll.metadata = {"hnsw:space": "l2"}   # WRONG — must be cosine

        mock_chroma_client = MagicMock()
        mock_chroma_client.get_or_create_collection.side_effect = [
            mock_exec_coll,      # execution_embeddings
            mock_anchors_coll,   # learning_anchors
        ]

        with patch("chromadb.PersistentClient", return_value=mock_chroma_client):
            with patch("chromadb.config.Settings"):
                em._init_chromadb()

        assert em._chroma_client is ExecutionMemory._CHROMADB_INIT_FAILED
        assert em._chroma_last_error is not None
        assert "cosine" in em._chroma_last_error.lower() or "hnsw" in em._chroma_last_error.lower()


# ---------------------------------------------------------------------------
# _store_sqlite — IntegrityError on non-passing re-run re-raises
# ---------------------------------------------------------------------------

class TestStoreSqliteIntegrityError:
    def test_integrity_error_on_non_passing_rerun_reraises(self, tmp_path):
        """A UNIQUE constraint collision on a workflow_id that is NOT a passing
        re-run (i.e. status='failed') should re-raise, not silently swallow."""
        em = _make_em(tmp_path)

        # Insert the first record normally
        r1 = _make_record("wf-dupe", "failed")
        em._store_sqlite(r1)

        # A second record with the same workflow_id but also 'failed' should
        # hit the IntegrityError else-branch (not Case B recovery) and re-raise.
        r2 = _make_record("wf-dupe", "failed")
        with pytest.raises(sqlite3.IntegrityError):
            em._store_sqlite(r2)


# ---------------------------------------------------------------------------
# get() — missing row returns None
# ---------------------------------------------------------------------------

class TestGetMissingRow:
    def test_get_nonexistent_workflow_id_returns_none(self, tmp_path):
        em = _make_em(tmp_path)
        result = em.get("workflow-that-does-not-exist")
        assert result is None


# ---------------------------------------------------------------------------
# get() — existing row round-trips correctly
# ---------------------------------------------------------------------------

class TestGetExistingRow:
    def test_stored_record_retrieved_by_workflow_id(self, tmp_path):
        em = _make_em(tmp_path)
        r = _make_record("wf-store", "passed")
        em._store_sqlite(r)

        fetched = em.get("wf-store")
        assert fetched is not None
        assert fetched.workflow_id == "wf-store"
        assert fetched.test_status == "passed"


# ---------------------------------------------------------------------------
# close() — safe when _writer_conn is falsy
# ---------------------------------------------------------------------------

class TestCloseNoWriterConn:
    def test_close_when_writer_conn_is_none_does_not_raise(self, tmp_path):
        """close() should not raise if _writer_conn was never set
        (simulates an early-init failure scenario)."""
        em = _make_em(tmp_path)
        em._writer_conn = None   # simulate init failure

        # Should not raise AttributeError or any other exception
        em.close()


# ---------------------------------------------------------------------------
# _init_chromadb — permanent disable (failed_at=None, sentinel set)
# ---------------------------------------------------------------------------

class TestInitChromaDbPermanentDisable:
    def test_sentinel_with_no_failed_at_returns_early(self, tmp_path):
        """Sentinel set with failed_at=None means permanent test/manual disable.
        _init_chromadb must return without retrying."""
        em = _make_em(tmp_path)
        em._chroma_client = ExecutionMemory._CHROMADB_INIT_FAILED
        em._chroma_failed_at = None   # no timestamp → permanent disable

        with patch("chromadb.PersistentClient") as mock_client:
            em._init_chromadb()

        mock_client.assert_not_called()

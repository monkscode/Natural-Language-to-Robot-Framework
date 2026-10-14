"""Failure-path tests for PostgresExecutionMemory (Phase 4 store).

The happy paths are covered by test_pgvector_similarity.py and the engine
suites; this module targets the paths that only fire when something goes
wrong in production — duplicate workflow_id re-runs, a dying writer
connection, embedder init failures, malformed vectors — so those failures
surface in CI instead of in front of users. Runs on the isolated
learning_test schema via the in_memory_em fixture (embedder disabled there;
tests that need one inject a mock).
"""

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from src.backend.crew_ai.optimization.execution_memory import ExecutionRecord
from src.backend.crew_ai.optimization.postgres_execution_memory import (
    PostgresExecutionMemory,
)


def _record(wf_id="wf-gap-1", status="failed", query="log into the portal", **kw):
    defaults = dict(
        workflow_id=wf_id, timestamp=datetime.now(), user_query=query,
        url="https://example.com/login", domain="example.com",
        robot_code="*** Test Cases ***", code_structure=None, test_status=status,
    )
    defaults.update(kw)
    return ExecutionRecord(**defaults)


class _FailingWriter:
    """Writer-connection stand-in that fails on every statement."""

    closed = False
    broken = False

    def __init__(self, exc=RuntimeError("connection reset by server")):
        self.exc = exc
        self.rollbacks = 0

    def execute(self, *a, **kw):
        raise self.exc

    def executemany(self, *a, **kw):
        raise self.exc

    def commit(self):
        raise self.exc

    def rollback(self):
        self.rollbacks += 1


class _DoublyFailingWriter(_FailingWriter):
    """Worst case: the statement fails AND the rollback fails (connection is
    fully dead). Non-blocking writes must still swallow both."""

    def rollback(self):
        self.rollbacks += 1
        raise RuntimeError("rollback failed too")


# ---------------------------------------------------------------------------
# Relational writes: re-run handling + rollback on failure
# ---------------------------------------------------------------------------

class TestStoreFailurePaths:
    def test_store_execution_delegates_to_store(self, in_memory_em):
        in_memory_em.store_execution(_record("wf-se-1"))
        assert in_memory_em.get("wf-se-1") is not None

    def test_rerun_of_failed_workflow_that_passes_updates_row(self, in_memory_em):
        """Case B: same workflow_id, first failed then passed -> the unique
        violation must convert the row to passing, not crash the writer."""
        in_memory_em.store(_record("wf-rerun", status="failed"))
        in_memory_em.store(_record("wf-rerun", status="passed",
                                   robot_code="WORKING CODE"))
        row = in_memory_em.get("wf-rerun")
        assert row.test_status == "passed"
        assert row.working_code == "WORKING CODE"

    def test_rerun_that_fails_again_reraises_integrity_error(self, in_memory_em):
        in_memory_em.store(_record("wf-rerun-f", status="failed"))
        with pytest.raises(sqlite3.IntegrityError):
            # Different query text so dedup doesn't swallow it; status stays
            # failed, so the passed-rerun rescue must NOT kick in.
            in_memory_em.store(_record("wf-rerun-f", status="failed",
                                       query="a different query"))

    def test_store_rolls_back_and_reraises_on_generic_failure(self, in_memory_em):
        writer = _FailingWriter()
        in_memory_em._writer_conn = writer
        with pytest.raises(RuntimeError, match="connection reset"):
            in_memory_em.store(_record("wf-boom"))
        assert writer.rollbacks == 1


# ---------------------------------------------------------------------------
# Hint workflow trace: non-blocking guarantees
# ---------------------------------------------------------------------------

class TestHintTraceFailurePaths:
    def test_store_trace_failure_is_swallowed_and_rolled_back(self, in_memory_em):
        writer = _FailingWriter()
        in_memory_em._writer_conn = writer
        in_memory_em._last_trace_prune_ts = time.time()  # keep prune out of the way
        in_memory_em.store_hint_workflow_trace(
            "wf-t1", {"hint-1": {"scope": "domain", "injected": True}})
        assert writer.rollbacks == 1  # rolled back, never raised

    def test_prune_guard_swallows_prune_errors(self, in_memory_em):
        in_memory_em._last_trace_prune_ts = 0.0  # force the daily prune to run
        with patch.object(in_memory_em, "prune_hint_workflow_trace",
                          side_effect=RuntimeError("prune blew up")):
            in_memory_em._maybe_prune_hint_workflow_trace()  # must not raise
        assert in_memory_em._last_trace_prune_ts > 0  # guard still advanced

    def test_prune_failure_returns_zero_after_rollback(self, in_memory_em):
        writer = _FailingWriter()
        in_memory_em._writer_conn = writer
        assert in_memory_em.prune_hint_workflow_trace(30) == 0
        assert writer.rollbacks == 1

    def test_prune_disabled_retention_is_noop(self, in_memory_em):
        assert in_memory_em.prune_hint_workflow_trace(0) == 0
        assert in_memory_em.prune_hint_workflow_trace(-5) == 0


# ---------------------------------------------------------------------------
# Read queries (never exercised by the engine suites against Postgres)
# ---------------------------------------------------------------------------

class TestReadQueries:
    def test_query_by_domain_orders_recent_first(self, in_memory_em):
        in_memory_em.store(_record("wf-d1", query="older run",
                                   timestamp=datetime(2026, 1, 1)))
        in_memory_em.store(_record("wf-d2", query="newer run",
                                   timestamp=datetime(2026, 2, 1)))
        rows = in_memory_em.query_by_domain("example.com")
        assert [r.workflow_id for r in rows] == ["wf-d2", "wf-d1"]
        assert in_memory_em.query_by_domain("other.com") == []

    def test_query_failures_filters_by_category_and_domain(self, in_memory_em):
        in_memory_em.store(_record("wf-f1", status="failed",
                                   failure_category="locator_error"))
        in_memory_em.store(_record("wf-f2", status="failed", query="another",
                                   failure_category="timeout"))
        in_memory_em.store(_record("wf-p1", status="passed", query="green"))
        assert {r.workflow_id for r in in_memory_em.query_failures()} == {"wf-f1", "wf-f2"}
        assert [r.workflow_id for r in
                in_memory_em.query_failures(category="timeout")] == ["wf-f2"]
        assert in_memory_em.query_failures(domain="other.com") == []


# ---------------------------------------------------------------------------
# Embedder lifecycle: cooldown, init failure, embed failure
# ---------------------------------------------------------------------------

class TestEmbedderFailurePaths:
    def test_failed_init_within_cooldown_is_not_retried(self, in_memory_em):
        # Task 31: the store borrows embedding.get_embedder() — the cooldown
        # gate must still short-circuit before ever consulting the singleton.
        from src.backend.crew_ai.optimization import embedding as emb_mod
        in_memory_em._chroma_client = PostgresExecutionMemory._CHROMADB_INIT_FAILED
        in_memory_em._chroma_failed_at = time.monotonic()  # genuine recent failure
        with patch.object(emb_mod, "get_embedder", MagicMock()) as ge:
            in_memory_em._init_chromadb()
        ge.assert_not_called()
        assert in_memory_em._chroma_client is PostgresExecutionMemory._CHROMADB_INIT_FAILED

    def test_failed_init_retried_after_cooldown_and_failure_recached(self, in_memory_em):
        from src.backend.crew_ai.optimization import embedding as emb_mod
        in_memory_em._chroma_client = PostgresExecutionMemory._CHROMADB_INIT_FAILED
        in_memory_em._chroma_failed_at = (
            time.monotonic() - in_memory_em._CHROMA_RETRY_COOLDOWN_S - 1)
        with patch.object(emb_mod, "get_embedder", return_value=None) as ge:
            in_memory_em._init_chromadb()
        ge.assert_called_once()  # cooldown expired -> retried via the singleton
        assert in_memory_em._chroma_client is PostgresExecutionMemory._CHROMADB_INIT_FAILED
        assert in_memory_em._chroma_last_error == "shared fastembed embedder unavailable"
        assert in_memory_em._chroma_failed_at is not None  # fresh cooldown window

    def test_embed_returns_none_when_model_dies_mid_embed(self, in_memory_em):
        broken = MagicMock()
        broken.embed.side_effect = RuntimeError("onnx session died")
        in_memory_em._chroma_client = broken
        assert in_memory_em._embed("anything") is None

    def test_embedding_skipped_for_empty_query(self, in_memory_em):
        spy = MagicMock()
        in_memory_em._chroma_client = spy
        in_memory_em._store_execution_embedding(_record("wf-e0", query=""))
        spy.embed.assert_not_called()


# ---------------------------------------------------------------------------
# Vector writes/reads with a malformed or unavailable embedder
# ---------------------------------------------------------------------------

def _with_fake_vec(em, literal="[not-a-vector]"):
    """Make _embed return a literal pgvector will reject (dim/parse error)."""
    return patch.object(em, "_embed", return_value=literal)


class TestVectorFailurePaths:
    def test_store_execution_embedding_failure_never_raises(self, in_memory_em):
        with _with_fake_vec(in_memory_em):
            in_memory_em._store_execution_embedding(_record("wf-v1"))  # must not raise
        # The relational row was untouched by the failed vector write.
        assert in_memory_em.get("wf-v1") is None

    # Both of these pass an org DELIBERATELY. The T9 refusal is checked one
    # line before the embedder, so an org-less call returns [] at the guard and
    # never reaches the failure each test is named for — which is exactly what
    # these two did until the org was added.
    def test_find_similar_returns_empty_when_embedder_down(self, in_memory_em):
        assert in_memory_em.find_similar_executions("anything", org_id="org-1") == []

    def test_find_similar_returns_empty_on_query_failure(self, in_memory_em):
        with _with_fake_vec(in_memory_em):
            assert in_memory_em.find_similar_executions(
                "anything", org_id="org-1") == []

    def test_search_similar_delegates(self, in_memory_em):
        # org_id must reach find_similar_executions. Without it that method
        # fails closed (T9) and every search_similar caller silently gets [].
        with patch.object(in_memory_em, "find_similar_executions",
                          return_value=[{"workflow_id": "x"}]) as f:
            assert in_memory_em.search_similar(
                "q", top_k=2, org_id="org-1") == [{"workflow_id": "x"}]
        f.assert_called_once_with("q", 2, org_id="org-1")

    def test_search_similar_refuses_to_be_called_without_an_org(self, in_memory_em):
        # org_id is keyword-only with NO default, so forgetting it is a
        # TypeError on the first call rather than a silent []. Pins that a
        # default is never re-added out of misplaced symmetry with the sibling
        # reads, which default to None because None is a real state for them.
        with pytest.raises(TypeError):
            in_memory_em.search_similar("q", top_k=2)


    def test_store_embedding_skips_when_embedder_down(self, in_memory_em):
        in_memory_em.store_embedding("text", {"workflow_id": "wf-emb-skip"})
        with in_memory_em.read_conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM execution_embeddings").fetchone()["n"]
        assert n == 0

    def test_store_embedding_roundtrip_with_real_vector(self, in_memory_em):
        vec = "[" + ",".join(["0.1"] * 384) + "]"
        with _with_fake_vec(in_memory_em, literal=vec):
            in_memory_em.store_embedding(
                "click the login button",
                {"workflow_id": "wf-emb-rt", "test_status": "passed",
                 "domain": "example.com"})
        with in_memory_em.read_conn() as conn:
            row = conn.execute(
                "SELECT user_query, test_status FROM execution_embeddings "
                "WHERE workflow_id = ?", ("wf-emb-rt",)).fetchone()
        assert row["user_query"] == "click the login button"
        assert row["test_status"] == "passed"

    def test_store_embedding_failure_rolls_back_silently(self, in_memory_em):
        with _with_fake_vec(in_memory_em):
            in_memory_em.store_embedding("text", {"workflow_id": "wf-emb-bad"})
        # Writer must remain usable after the rollback.
        in_memory_em.store(_record("wf-after-emb-fail"))
        assert in_memory_em.get("wf-after-emb-fail") is not None

    def test_add_anchor_failure_rolls_back_silently(self, in_memory_em):
        with _with_fake_vec(in_memory_em):
            in_memory_em.add_anchor("nl", 1, "some anchor query")  # must not raise
        in_memory_em.store(_record("wf-after-anchor-fail"))
        assert in_memory_em.get("wf-after-anchor-fail") is not None

    def test_add_anchor_blank_query_or_no_embedder_is_noop(self, in_memory_em):
        in_memory_em.add_anchor("nl", 1, "   ")
        in_memory_em.add_anchor("nl", 1, "real query")  # embedder disabled -> None
        with in_memory_em.read_conn() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM learning_anchors").fetchone()["n"]
        assert n == 0

    def test_dead_connection_rollback_failures_are_swallowed(self, in_memory_em):
        writer = _DoublyFailingWriter()
        in_memory_em._writer_conn = writer
        with patch.object(in_memory_em, "_embed", return_value="[0.1]"):
            in_memory_em._store_execution_embedding(_record("wf-dead"))  # no raise
            in_memory_em.store_embedding("text", {"workflow_id": "wf-dead2"})
            in_memory_em.add_anchor("nl", 1, "anchor query")
        assert in_memory_em.prune_hint_workflow_trace(30) == 0
        assert writer.rollbacks == 4  # every path attempted the rollback

    def test_filter_by_query_similarity_fails_open_on_query_error(self, in_memory_em):
        sink = {}
        with _with_fake_vec(in_memory_em):
            survivors = in_memory_em.filter_by_query_similarity(
                "log in", [1, 2], "nl", score_sink=sink)
        assert survivors == {1, 2}  # bad vector -> SQL error -> fail-open
        assert sink[1]["outcome"] == "fail_open"


# ---------------------------------------------------------------------------
# reconcile_anchors: read failures mid-reconcile
# ---------------------------------------------------------------------------

class _FlakyReadConn:
    """read_conn() replacement failing on the Nth call (1-based)."""

    def __init__(self, em, fail_on: int):
        self._real = em.__class__.read_conn  # unbound, pre-patch
        self._em = em
        self.calls = 0
        self.fail_on = fail_on

    @contextmanager
    def __call__(self):
        self.calls += 1
        if self.calls == self.fail_on:
            raise RuntimeError("read pool exhausted")
        with self._real(self._em) as conn:
            yield conn


class TestReconcileAnchors:
    def _seed_nl_correction(self, em, anchor="log into the portal"):
        now = datetime.now().isoformat()
        em._writer_conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(feedback_text, anchor_query, created_at, last_seen) "
            "VALUES (?, ?, ?, ?)",
            ("use the burger menu on mobile", anchor, now, now),
        )
        em._writer_conn.commit()

    def test_reconcile_skips_when_embedder_unavailable(self, in_memory_em):
        out = in_memory_em.reconcile_anchors()
        assert out["checked"] == 0
        assert out["missing_count"] == 0

    def test_reconcile_reports_all_missing_when_anchor_lookup_fails(self, in_memory_em):
        self._seed_nl_correction(in_memory_em)
        in_memory_em._chroma_client = MagicMock()  # available
        in_memory_em.read_conn = _FlakyReadConn(in_memory_em, fail_on=2)
        out = in_memory_em.reconcile_anchors()
        assert out["checked"] == 1
        assert out["missing_count"] == 1  # honest: nothing verified as present

    def test_reconcile_reports_missing_when_recheck_fails(self, in_memory_em):
        self._seed_nl_correction(in_memory_em)
        in_memory_em._chroma_client = MagicMock()  # available
        # add_anchor would need a real embedder; keep it a no-op (it swallows
        # its own failures by contract) and fail the verification re-read.
        in_memory_em.add_anchor = MagicMock()
        in_memory_em.read_conn = _FlakyReadConn(in_memory_em, fail_on=3)
        out = in_memory_em.reconcile_anchors()
        assert out["checked"] == 1
        assert out["missing_count"] == 1


# ---------------------------------------------------------------------------
# close(): best-effort teardown
# ---------------------------------------------------------------------------

class TestClose:
    def test_close_swallows_pool_close_failure(self, in_memory_em):
        # Inject fakes for BOTH resources: the fixture restores the real ones.
        writer = MagicMock()
        pool = MagicMock()
        pool.close.side_effect = RuntimeError("pool already gone")
        in_memory_em._writer = writer
        in_memory_em._read_pool = pool
        in_memory_em.close()  # must not raise
        writer.close.assert_called_once()
        pool.close.assert_called_once()

"""Real pgvector + fastembed similarity tests (Phase 4 slice 6).

The rest of the optimization suite disables embeddings (fail-open path); this
module exercises the genuine pgvector path with the fastembed all-MiniLM-L6-v2
model enabled, validating add_anchor / filter_by_query_similarity / reconcile /
execution-embedding round-trips against a live Postgres+pgvector schema.
"""

from datetime import datetime

import pytest

from src.backend.crew_ai.optimization.execution_memory import ExecutionRecord


@pytest.fixture(scope="session")
def _shared_embedder():
    """Load the fastembed model once for the whole module (it is ~80MB ONNX)."""
    from fastembed import TextEmbedding
    from src.backend.crew_ai.optimization.postgres_execution_memory import EMBED_MODEL
    return TextEmbedding(model_name=EMBED_MODEL)


@pytest.fixture
def em_vec(in_memory_em, _shared_embedder):
    """in_memory_em with the embedder ENABLED (real pgvector similarity path)."""
    in_memory_em._chroma_client = _shared_embedder
    in_memory_em._chroma_failed_at = None
    in_memory_em._chroma_last_error = None
    return in_memory_em


class TestFilterByQuerySimilarityPgvector:
    def test_empty_inputs_return_empty(self, em_vec):
        assert em_vec.filter_by_query_similarity("", [1, 2], "nl") == set()
        assert em_vec.filter_by_query_similarity("  ", [1, 2], "nl") == set()
        assert em_vec.filter_by_query_similarity("a query", [], "nl") == set()

    def test_cold_start_fails_open(self, em_vec):
        # No anchors exist at all → don't filter (return every candidate).
        assert em_vec.filter_by_query_similarity("log in", [1, 2], "nl") == {1, 2}

    def test_keeps_relevant_drops_irrelevant(self, em_vec):
        em_vec.add_anchor("nl", 1, "log into my account")
        em_vec.add_anchor("nl", 2, "search for a laptop under 1000")
        survivors = em_vec.filter_by_query_similarity(
            "sign in to my profile", [1, 2], "nl", threshold=0.55)
        assert survivors == {1}

    def test_kind_isolation(self, em_vec):
        em_vec.add_anchor("nl", 5, "log into my account")
        em_vec.add_anchor("anti", 5, "verify every row shows a price")
        nl = em_vec.filter_by_query_similarity("sign in", [5], "nl", threshold=0.45)
        anti = em_vec.filter_by_query_similarity("sign in", [5], "anti", threshold=0.45)
        assert nl == {5}
        assert anti == set()

    def test_candidate_without_anchor_dropped(self, em_vec):
        # Anchors exist (so not cold-start), but candidate 2 has none → dropped.
        em_vec.add_anchor("nl", 1, "search for a laptop under 1000")
        survivors = em_vec.filter_by_query_similarity(
            "find me a cheap laptop", [1, 2], "nl", threshold=0.45)
        assert 2 not in survivors
        assert survivors == {1}

    def test_no_candidate_anchor_with_populated_collection_drops_all(self, em_vec):
        em_vec.add_anchor("nl", 99, "something unrelated entirely")
        # candidates 1,2 have no anchors; collection is non-empty → drop all.
        assert em_vec.filter_by_query_similarity("anything", [1, 2], "nl") == set()

    def test_score_sink_records_outcomes(self, em_vec):
        em_vec.add_anchor("nl", 1, "log into my account")
        em_vec.add_anchor("nl", 2, "search for a laptop under 1000")
        sink: dict = {}
        survivors = em_vec.filter_by_query_similarity(
            "sign in to my profile", [1, 2], "nl", threshold=0.55, score_sink=sink)
        assert survivors == {1}
        assert sink[1]["outcome"] == "survived"
        assert sink[2]["outcome"] == "similarity_below"
        assert 0.0 <= sink[2]["sim"] <= 1.0


class TestAddAnchorPgvector:
    def test_blank_anchor_is_skipped(self, em_vec):
        em_vec.add_anchor("nl", 1, "   ")
        n = em_vec._writer_conn.execute(
            "SELECT COUNT(*) AS n FROM learning_anchors").fetchone()["n"]
        assert n == 0

    def test_upsert_replaces_existing(self, em_vec):
        em_vec.add_anchor("nl", 1, "log into my account")
        em_vec.add_anchor("nl", 1, "search for a laptop")  # same key, new text
        rows = em_vec._writer_conn.execute(
            "SELECT anchor_query FROM learning_anchors WHERE anchor_key = 'nl:1'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["anchor_query"] == "search for a laptop"


class TestReconcileAnchorsPgvector:
    def test_reconcile_embeds_missing_anchors(self, em_vec):
        em_vec._writer_conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(feedback_text, category, scope, anchor_query, created_at, last_seen) "
            "VALUES ('hint', 'c', 'global', 'log into the portal', '2026-01-01', '2026-01-01')")
        em_vec._writer_conn.execute(
            "INSERT INTO anti_patterns (failure_category, query_pattern, last_seen) "
            "VALUES ('A1', 'verify all rows are active', '2026-01-01')")
        em_vec._writer_conn.commit()

        result = em_vec.reconcile_anchors()
        assert result["checked"] == 2
        assert result["missing_count"] == 0
        keys = {r["anchor_key"] for r in em_vec._writer_conn.execute(
            "SELECT anchor_key FROM learning_anchors").fetchall()}
        assert keys == {"nl:1", "anti:1"}

    def test_reconcile_noop_when_nothing_to_anchor(self, em_vec):
        result = em_vec.reconcile_anchors()
        assert result == {"ran_at": result["ran_at"], "checked": 0, "missing_count": 0}


class TestExecutionEmbeddingPgvector:
    def test_store_and_find_similar(self, em_vec):
        em_vec.store(ExecutionRecord(
            workflow_id="wf-emb-1", timestamp=datetime.now(),
            user_query="click the login button", url="u", domain="d",
            robot_code="code", code_structure=None, test_status="passed",
        ))
        em_vec.store(ExecutionRecord(
            workflow_id="wf-emb-2", timestamp=datetime.now(),
            user_query="add three laptops to the shopping cart", url="u", domain="d",
            robot_code="code", code_structure=None, test_status="passed",
        ))
        hits = em_vec.find_similar_executions("press the sign-in button", top_k=2)
        assert hits, "expected at least one similar execution"
        assert hits[0]["workflow_id"] == "wf-emb-1"  # login nearest to sign-in

"""Query-similarity hint filtering — ExecutionMemory layer.

Covers the learning_anchors collection, filter_by_query_similarity,
add_anchor and reconcile_anchors.
See docs/QUERY_SIMILARITY_HINT_INJECTION_PLAN.md.
"""

import sqlite3
import time
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from src.backend.crew_ai.optimization.execution_memory import (
    ExecutionMemory,
    ExecutionRecord,
)
from src.backend.crew_ai.optimization.nl_feedback_engine import NLFeedbackEngine
from src.backend.crew_ai.optimization.anti_pattern_engine import AntiPatternEngine
from src.backend.crew_ai.optimization.feedback_loop import (
    LearningMetricsTracker,
    FeedbackLoop,
)
from src.backend.crew_ai.optimization.smart_keyword_provider import (
    SmartKeywordProvider,
)


@pytest.fixture
def em_chroma(tmp_path):
    """ExecutionMemory backed by a real (temp) ChromaDB so similarity
    behaviour is exercised against genuine MiniLM embeddings."""
    em = ExecutionMemory(
        db_path=str(tmp_path / "qs.db"),
        chroma_dir=str(tmp_path / "chroma"),
    )
    yield em
    em.close()


@pytest.fixture
def fresh_em(tmp_path):
    """ExecutionMemory with ChromaDB NOT yet initialized (_chroma_client is
    None) — the state get_health_status must treat as transient, not FAILED."""
    em = ExecutionMemory(
        db_path=str(tmp_path / "fresh.db"),
        chroma_dir=str(tmp_path / "chroma"),
    )
    yield em
    em.close()


def _make_feedback_loop(em):
    """FeedbackLoop with a mock write_queue (so the init reconcile and every
    process_execution submit are captured, not executed) and a stub
    pattern_learner (so __init__ never touches ./chroma_db)."""
    return FeedbackLoop(
        execution_memory=em,
        write_queue=MagicMock(),
        pattern_learner=MagicMock(),
    )


def _make_provider(em, holdout=False):
    """SmartKeywordProvider with mock library/pattern/vector deps — only the
    learning-hint path (Tier 0) is exercised. holdout forces the R7 coin."""
    pattern_matcher = MagicMock()
    pattern_matcher.get_relevant_keywords.return_value = []
    lib = MagicMock()
    lib.library_name = "browser"
    lib.core_rules = "CORE RULES"
    provider = SmartKeywordProvider(
        library_context=lib,
        pattern_matcher=pattern_matcher,
        vector_store=MagicMock(),
        execution_memory=em,
    )
    provider._holdout_decision = holdout
    return provider


def _seed_metric(em, *, hints_injected, was_holdout, test_passed, n,
                 is_first_attempt=1):
    """Insert n learning_metrics rows with the given hint/holdout/outcome."""
    for i in range(n):
        em._writer_conn.execute(
            "INSERT INTO learning_metrics "
            "(workflow_id, user_query, is_first_attempt, hints_available, "
            " hints_injected, llm_calls, llm_cost, test_passed, was_holdout, "
            " timestamp) "
            "VALUES (?, 'q', ?, 0, ?, 1, 0.0, ?, ?, '2026-01-01')",
            (f"wf-{was_holdout}-{hints_injected}-{test_passed}-{i}",
             is_first_attempt, hints_injected, test_passed, was_holdout),
        )
    em._writer_conn.commit()


def _insert_nl_hint(em, hint_id, feedback_text, anchor_query,
                    scope="global", domain=None):
    em._writer_conn.execute(
        "INSERT INTO nl_feedback_corrections "
        "(id, feedback_text, anchor_query, scope, domain, created_at, last_seen) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (hint_id, feedback_text, anchor_query, scope, domain,
         "2026-01-01", "2026-01-01"),
    )
    em._writer_conn.commit()


def _insert_anti(em, anti_id, failure_category, query_pattern,
                 score=0.5, evidence_count=1):
    em._writer_conn.execute(
        "INSERT INTO anti_patterns "
        "(id, failure_category, query_pattern, score, evidence_count, last_seen) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (anti_id, failure_category, query_pattern, score, evidence_count,
         "2026-01-01"),
    )
    em._writer_conn.commit()


# ===================================================================
# model_version column round-trip (pure SQLite)
# ===================================================================


class TestModelVersionColumn:
    def test_model_version_round_trip(self, in_memory_em):
        rec = ExecutionRecord(
            workflow_id="wf-mv-1",
            timestamp=datetime(2026, 1, 1),
            user_query="do a thing",
            test_status="passed",
            model_version="gemini/gemini-2.5-flash",
        )
        in_memory_em.store(rec)
        got = in_memory_em.get("wf-mv-1")
        assert got is not None
        assert got.model_version == "gemini/gemini-2.5-flash"

    def test_model_version_defaults_none(self, in_memory_em):
        rec = ExecutionRecord(
            workflow_id="wf-mv-2",
            timestamp=datetime(2026, 1, 1),
            user_query="another thing",
            test_status="failed",
        )
        in_memory_em.store(rec)
        got = in_memory_em.get("wf-mv-2")
        assert got is not None and got.model_version is None

    def test_model_version_refreshed_on_dedup(self, in_memory_em):
        # DEDUPLICATION_THRESHOLD identical runs → the next store triggers the
        # dedup UPDATE, which must refresh model_version (a current-state
        # field, kept latest like robot_code) — not leave it stale. A domain
        # is required: the dedup match is keyed on (query, domain, status).
        thr = ExecutionMemory.DEDUPLICATION_THRESHOLD
        for i in range(thr):
            in_memory_em.store(ExecutionRecord(
                workflow_id=f"wf-dv-{i}",
                timestamp=datetime(2026, 1, 1, 0, 0, i),
                user_query="same query for dedup",
                domain="dedup.com",
                test_status="passed",
                model_version="gemini/old-model",
            ))
        in_memory_em.store(ExecutionRecord(
            workflow_id="wf-dv-new",
            timestamp=datetime(2026, 1, 2),
            user_query="same query for dedup",
            domain="dedup.com",
            test_status="passed",
            model_version="gemini/new-model",
        ))
        rows = in_memory_em._writer_conn.execute(
            "SELECT model_version FROM execution_records "
            "WHERE user_query = 'same query for dedup'"
        ).fetchall()
        assert len(rows) == thr                # last store deduped, not inserted
        latest = in_memory_em._writer_conn.execute(
            "SELECT model_version FROM execution_records "
            "WHERE user_query = 'same query for dedup' "
            "ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()[0]
        assert latest == "gemini/new-model"


class TestUrlLessDeduplication:
    """Regression: a workflow with no URL stores domain=NULL. The dedup match
    keys on domain; a plain `domain = ''` never matched NULL, so URL-less
    rows accumulated unbounded. COALESCE(domain,'') in the match fixes it."""

    def test_url_less_workflows_deduplicate(self, in_memory_em):
        thr = ExecutionMemory.DEDUPLICATION_THRESHOLD
        for i in range(thr + 1):          # thr inserts, then one more
            in_memory_em.store(ExecutionRecord(
                workflow_id=f"wf-nourl-{i}",
                timestamp=datetime(2026, 1, 1, 0, 0, i),
                user_query="a query with no url",
                domain=None,               # URL-less → stored as NULL
                test_status="passed",
            ))
        count = in_memory_em._writer_conn.execute(
            "SELECT COUNT(*) FROM execution_records "
            "WHERE user_query = 'a query with no url'"
        ).fetchone()[0]
        assert count == thr                # the last store deduped, not inserted


# ===================================================================
# learning_anchors collection
# ===================================================================


class TestLearningAnchorsCollection:
    def test_collection_is_cosine(self, em_chroma):
        em_chroma._init_chromadb()
        assert em_chroma._learning_anchors is not None
        assert em_chroma._learning_anchors.metadata.get("hnsw:space") == "cosine"


# ===================================================================
# filter_by_query_similarity
# ===================================================================


class TestFilterBySimilarity:
    def test_empty_query_returns_empty(self, in_memory_em):
        assert in_memory_em.filter_by_query_similarity("", [1, 2], "nl") == set()
        assert in_memory_em.filter_by_query_similarity("  ", [1, 2], "nl") == set()

    def test_empty_candidates_returns_empty(self, in_memory_em):
        assert in_memory_em.filter_by_query_similarity("a query", [], "nl") == set()

    def test_chroma_unavailable_fails_open(self, in_memory_em):
        # in_memory_em has the ChromaDB sentinel set — learning_anchors None.
        result = in_memory_em.filter_by_query_similarity(
            "a query", [1, 2, 3], "nl")
        assert result == {1, 2, 3}

    def test_count_zero_with_candidates_fails_open(self, em_chroma):
        # Fresh learning_anchors is empty → n==0 and count()==0 → fail-open.
        result = em_chroma.filter_by_query_similarity("some query", [1, 2], "nl")
        assert result == {1, 2}

    def test_keeps_relevant_drops_irrelevant(self, em_chroma):
        em_chroma.add_anchor("nl", 1, "log into my account")
        em_chroma.add_anchor("nl", 2, "search for a laptop under 1000")
        survivors = em_chroma.filter_by_query_similarity(
            "find me a laptop to buy", [1, 2], "nl")
        assert 2 in survivors          # laptop anchor — semantically close
        assert 1 not in survivors      # login anchor — unrelated, dropped

    def test_missing_anchor_doc_dropped(self, em_chroma):
        em_chroma.add_anchor("nl", 1, "search for a laptop under 1000")
        # Candidate 2 has no learning_anchors document.
        survivors = em_chroma.filter_by_query_similarity(
            "find me a laptop", [1, 2], "nl")
        assert 2 not in survivors      # fail-closed for the missing doc

    def test_n_results_sized_to_filtered_set(self, em_chroma):
        # C18: collection count must NOT drive n_results. Pad with anti-kind
        # docs so count() far exceeds the nl filtered set.
        em_chroma.add_anchor("nl", 1, "search for a laptop under 1000")
        em_chroma.add_anchor("nl", 2, "buy a cheap notebook computer")
        for i in range(1, 6):
            em_chroma.add_anchor("anti", i, "an unrelated anti pattern note")
        # count() is now 7; the nl filtered set for [1,2,3] is 2.
        survivors = em_chroma.filter_by_query_similarity(
            "find me a laptop", [1, 2, 3], "nl")
        assert 3 not in survivors      # no doc → dropped, NOT fail-open
        assert survivors <= {1, 2}

    def test_kind_isolation(self, em_chroma):
        # NL id 5 and anti id 5 are different rows; kind must select correctly.
        em_chroma.add_anchor("nl", 5, "log into my account")
        em_chroma.add_anchor("anti", 5, "verify every row has a price")
        nl_survivors = em_chroma.filter_by_query_similarity(
            "sign in to my account", [5], "nl")
        assert nl_survivors == {5}     # resolved the nl anchor (login)
        anti_survivors = em_chroma.filter_by_query_similarity(
            "sign in to my account", [5], "anti")
        assert anti_survivors == set()  # resolved the anti anchor (price) → drop

    def test_query_failure_fails_open(self, em_chroma):
        em_chroma.add_anchor("nl", 1, "search for a laptop")
        em_chroma._init_chromadb()
        with patch.object(em_chroma._learning_anchors, "query",
                           side_effect=RuntimeError("boom")):
            result = em_chroma.filter_by_query_similarity(
                "find a laptop", [1, 2], "nl")
        assert result == {1, 2}        # fail-open on query exception


# ===================================================================
# filter_by_query_similarity — opt-in score_sink (F1 / N3)
# ===================================================================


class TestFilterBySimilarityScoreSink:
    """F1 — the opt-in score_sink records each candidate's outcome for the N3
    trace. THE load-bearing invariant (FR4 guard #1): the survivor set must be
    byte-identical with vs without the sink, because both callers do
    `r["id"] in survivors`. Every test re-checks that equality.

    outcome enum: survived / similarity_below / no_anchor / fail_open.
    """

    def test_survivor_set_identical_with_and_without_sink(self, em_chroma):
        # FR4 guard #1 on a mixed survive/drop case.
        em_chroma.add_anchor("nl", 1, "log into my account")
        em_chroma.add_anchor("nl", 2, "search for a laptop under 1000")
        without = em_chroma.filter_by_query_similarity(
            "find me a laptop to buy", [1, 2], "nl")
        sink = {}
        with_sink = em_chroma.filter_by_query_similarity(
            "find me a laptop to buy", [1, 2], "nl", score_sink=sink)
        assert with_sink == without == {2}

    def test_survived_and_similarity_below_recorded(self, em_chroma):
        em_chroma.add_anchor("nl", 1, "log into my account")
        em_chroma.add_anchor("nl", 2, "search for a laptop under 1000")
        sink = {}
        survivors = em_chroma.filter_by_query_similarity(
            "find me a laptop to buy", [1, 2], "nl", score_sink=sink)
        assert survivors == {2}
        assert sink[2]["outcome"] == "survived"
        assert sink[2]["sim"] is not None and sink[2]["sim"] >= 0.55
        assert sink[1]["outcome"] == "similarity_below"
        assert sink[1]["sim"] is not None and sink[1]["sim"] < 0.55

    def test_no_anchor_recorded_for_missing_doc(self, em_chroma):
        # Candidate 1 has a doc and matches; candidate 2 has no doc → it never
        # appears in the query metas, so it keeps the pre-seeded no_anchor.
        em_chroma.add_anchor("nl", 1, "search for a laptop under 1000")
        sink = {}
        without = em_chroma.filter_by_query_similarity(
            "find me a laptop", [1, 2], "nl")
        survivors = em_chroma.filter_by_query_similarity(
            "find me a laptop", [1, 2], "nl", score_sink=sink)
        assert survivors == without
        assert 2 not in survivors
        assert sink[2] == {"sim": None, "outcome": "no_anchor"}

    def test_fail_open_chroma_unavailable(self, in_memory_em):
        # learning_anchors is None (sentinel) → fail-open: all survive.
        without = in_memory_em.filter_by_query_similarity(
            "a query", [1, 2, 3], "nl")
        sink = {}
        survivors = in_memory_em.filter_by_query_similarity(
            "a query", [1, 2, 3], "nl", score_sink=sink)
        assert survivors == without == {1, 2, 3}
        assert all(sink[i] == {"sim": None, "outcome": "fail_open"}
                   for i in (1, 2, 3))

    def test_fail_open_whole_store_empty(self, em_chroma):
        # n==0 AND count()==0 → fail-open (store empty), NOT no_anchor.
        sink = {}
        survivors = em_chroma.filter_by_query_similarity(
            "some query", [1, 2], "nl", score_sink=sink)
        assert survivors == {1, 2}
        assert all(sink[i] == {"sim": None, "outcome": "fail_open"}
                   for i in (1, 2))

    def test_un_reconciled_keeps_no_anchor(self, em_chroma):
        # n==0 but count()>0 (only other-kind docs exist) → fail-CLOSED drop;
        # candidates keep the pre-seeded no_anchor (must NOT become fail_open).
        for i in range(1, 6):
            em_chroma.add_anchor("anti", i, "an unrelated anti pattern note")
        sink = {}
        survivors = em_chroma.filter_by_query_similarity(
            "find a laptop", [1, 2], "nl", score_sink=sink)
        assert survivors == set()
        assert all(sink[i] == {"sim": None, "outcome": "no_anchor"}
                   for i in (1, 2))

    def test_fail_open_on_query_exception(self, em_chroma):
        em_chroma.add_anchor("nl", 1, "search for a laptop")
        em_chroma._init_chromadb()
        sink = {}
        with patch.object(em_chroma._learning_anchors, "query",
                          side_effect=RuntimeError("boom")):
            survivors = em_chroma.filter_by_query_similarity(
                "find a laptop", [1, 2], "nl", score_sink=sink)
        assert survivors == {1, 2}
        assert all(sink[i] == {"sim": None, "outcome": "fail_open"}
                   for i in (1, 2))

    def test_empty_query_leaves_sink_untouched(self, em_chroma):
        # Pre-seed happens AFTER the empty-guard → an empty/whitespace query
        # records nothing (nothing was evaluated).
        sink = {}
        assert em_chroma.filter_by_query_similarity(
            "", [1, 2], "nl", score_sink=sink) == set()
        assert em_chroma.filter_by_query_similarity(
            "   ", [1, 2], "nl", score_sink=sink) == set()
        assert sink == {}

    def test_empty_candidates_leaves_sink_untouched(self, em_chroma):
        sink = {}
        assert em_chroma.filter_by_query_similarity(
            "a query", [], "nl", score_sink=sink) == set()
        assert sink == {}

    def test_corrupt_anchor_record_id_skipped(self, em_chroma):
        # edge #3: a meta row missing record_id is skipped (meta.get + continue),
        # not a KeyError crash. The candidate keeps its pre-seeded no_anchor.
        em_chroma.add_anchor("nl", 1, "search for a laptop under 1000")
        em_chroma._init_chromadb()
        fake = {"metadatas": [[{"kind": "nl"}]], "distances": [[0.1]]}
        sink = {}
        with patch.object(em_chroma._learning_anchors, "query",
                          return_value=fake):
            survivors = em_chroma.filter_by_query_similarity(
                "find me a laptop", [1], "nl", score_sink=sink)
        assert survivors == set()        # no record_id → skipped, not crashed
        assert sink[1] == {"sim": None, "outcome": "no_anchor"}

    def test_duplicate_candidate_ids_fail_open(self, em_chroma):
        # corner #6 (CORRECTED vs the design doc, verified against live Chroma):
        # real ChromaDB rejects duplicate ids in get() ("Expected IDs to be
        # unique"), so a candidate list with dups hits the fail-open path — the
        # unique ids survive, marked fail_open (NOT "dict+set dedup → survived"
        # as the audit claimed). Pre-existing behavior; F1 only adds the
        # annotation. Production passes distinct SQL PKs, so this never fires.
        em_chroma.add_anchor("nl", 2, "search for a laptop under 1000")
        without = em_chroma.filter_by_query_similarity(
            "find me a laptop to buy", [2, 2], "nl")
        sink = {}
        survivors = em_chroma.filter_by_query_similarity(
            "find me a laptop to buy", [2, 2], "nl", score_sink=sink)
        assert survivors == without == {2}
        assert sink == {2: {"sim": None, "outcome": "fail_open"}}


# ===================================================================
# reconcile_anchors
# ===================================================================


class TestReconcileAnchors:
    def test_populates_on_first_run(self, em_chroma):
        _insert_nl_hint(em_chroma, 1, "use X not Y", "log into my account")
        _insert_nl_hint(em_chroma, 2, "wait for spinner", "add an item to cart")
        _insert_anti(em_chroma, 1, "C1", "verify all rows have prices")
        result = em_chroma.reconcile_anchors()
        assert result["checked"] == 3
        assert result["missing_count"] == 0
        ids = set(em_chroma._learning_anchors.get(include=[])["ids"])
        assert ids == {"nl:1", "nl:2", "anti:1"}

    def test_idempotent(self, em_chroma):
        _insert_nl_hint(em_chroma, 1, "h", "log into my account")
        em_chroma.reconcile_anchors()
        result2 = em_chroma.reconcile_anchors()
        assert result2["checked"] == 1
        assert result2["missing_count"] == 0
        assert em_chroma._learning_anchors.count() == 1

    def test_heals_missing_doc(self, em_chroma):
        _insert_nl_hint(em_chroma, 1, "h1", "log into my account")
        _insert_nl_hint(em_chroma, 2, "h2", "search for a laptop")
        em_chroma.reconcile_anchors()
        em_chroma._learning_anchors.delete(ids=["nl:1"])
        assert em_chroma._learning_anchors.count() == 1
        result = em_chroma.reconcile_anchors()
        assert result["missing_count"] == 0
        assert em_chroma._learning_anchors.count() == 2

    def test_skips_null_anchor_rows(self, em_chroma):
        _insert_nl_hint(em_chroma, 1, "good", "log into my account")
        em_chroma._writer_conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(id, feedback_text, anchor_query, created_at, last_seen) "
            "VALUES (2, 'bad', NULL, '2026-01-01', '2026-01-01')"
        )
        em_chroma._writer_conn.commit()
        result = em_chroma.reconcile_anchors()
        assert result["checked"] == 1   # only the row with a real anchor
        assert result["missing_count"] == 0
        ids = set(em_chroma._learning_anchors.get(include=[])["ids"])
        assert ids == {"nl:1"}

    def test_returns_ran_at_timestamp(self, em_chroma):
        result = em_chroma.reconcile_anchors()
        assert "ran_at" in result and isinstance(result["ran_at"], str)
        assert result["checked"] == 0   # empty DB
        assert result["missing_count"] == 0


# ===================================================================
# Bank 1 — NLFeedbackEngine.get_hints_with_ids similarity filter
# ===================================================================


class TestBank1Retrieval:
    def test_empty_query_returns_empty(self, em_chroma):
        eng = NLFeedbackEngine(em_chroma)
        _insert_nl_hint(em_chroma, 1, "a hint", "some anchor query")
        em_chroma.reconcile_anchors()
        assert eng.get_hints_with_ids("", "http://x.com", "planner") == ([], [])
        assert eng.get_hints_with_ids("  ", "http://x.com", "planner") == ([], [])

    def test_keeps_relevant_hint(self, em_chroma):
        eng = NLFeedbackEngine(em_chroma)
        _insert_nl_hint(em_chroma, 1, "use Get Element Count for counting",
                        "count the number of products on the page")
        em_chroma.reconcile_anchors()
        hints, ids = eng.get_hints_with_ids(
            "how many products are on the page", "http://x.com", "planner")
        assert ids == [1]
        assert len(hints) == 1

    def test_drops_irrelevant_hint(self, em_chroma):
        eng = NLFeedbackEngine(em_chroma)
        _insert_nl_hint(em_chroma, 1, "click Sign In not Sign Up",
                        "log into my account")
        em_chroma.reconcile_anchors()
        hints, ids = eng.get_hints_with_ids(
            "search for laptops and verify the count", "http://x.com", "planner")
        assert hints == [] and ids == []

    def test_filter_applies_to_global_scope(self, em_chroma):
        # A global-scope hint is NOT exempt from the similarity filter.
        eng = NLFeedbackEngine(em_chroma)
        _insert_nl_hint(em_chroma, 1, "click the cookie banner first",
                        "accept cookies on first visit", scope="global")
        em_chroma.reconcile_anchors()
        hints, ids = eng.get_hints_with_ids(
            "filter products by price and sort them", "http://x.com", "planner")
        assert hints == [] and ids == []

    def test_chroma_unavailable_fails_open_to_scope_only(self, in_memory_em):
        # ChromaDB down → similarity filter fails open → scope-only behaviour.
        eng = NLFeedbackEngine(in_memory_em)
        _insert_nl_hint(in_memory_em, 1, "a hint", "anchor text here")
        hints, ids = eng.get_hints_with_ids(
            "any query at all", "http://x.com", "planner")
        assert ids == [1]


# ===================================================================
# Bank 1 — get_hints_with_ids selection_trace (F2b stages 2-4)
# ===================================================================


class TestBank1SelectionTrace:
    """F2b — get_hints_with_ids records each NL candidate's stage 2-4 fate
    (similarity → dedup → internal [:5] cap) into an opt-in selection_trace
    dict. The returned (hints, ids) stay byte-identical with or without it.
    Stage 5 (holdout / outer cap / injected) is overlaid later by the provider.
    """

    def test_trace_none_is_noop(self, em_chroma):
        eng = NLFeedbackEngine(em_chroma)
        _insert_nl_hint(em_chroma, 1, "use Get Element Count for counting",
                        "count the number of products on the page")
        em_chroma.reconcile_anchors()
        hints, ids = eng.get_hints_with_ids(
            "how many products are on the page", "http://x.com", "planner")
        assert ids == [1]          # unchanged behaviour, no trace requested

    def test_returned_ids_identical_with_and_without_trace(self, em_chroma):
        # The load-bearing guard: a populated trace never changes (hints, ids).
        eng = NLFeedbackEngine(em_chroma)
        _insert_nl_hint(em_chroma, 1, "use Get Element Count for counting",
                        "count the number of products on the page")
        _insert_nl_hint(em_chroma, 2, "click Sign In not Sign Up",
                        "log into my account")
        em_chroma.reconcile_anchors()
        h0, i0 = eng.get_hints_with_ids(
            "how many products are on the page", "http://x.com", "planner")
        trace = {}
        h1, i1 = eng.get_hints_with_ids(
            "how many products are on the page", "http://x.com", "planner",
            selection_trace=trace)
        assert (h1, i1) == (h0, i0)

    def test_survived_hint_available_with_real_sim(self, em_chroma):
        eng = NLFeedbackEngine(em_chroma)
        _insert_nl_hint(em_chroma, 1, "use Get Element Count for counting",
                        "count the number of products on the page",
                        scope="global")
        em_chroma.reconcile_anchors()
        trace = {}
        eng.get_hints_with_ids(
            "how many products are on the page", "http://x.com", "planner",
            selection_trace=trace)
        assert trace[1]["available"] == 1
        assert trace[1]["injected"] == 0            # stage 5 not applied here
        assert trace[1]["drop_reason"] is None
        assert trace[1]["source"] == "nl"
        assert trace[1]["priority"] == "high"
        assert trace[1]["scope"] == "global"
        assert trace[1]["similarity_score"] is not None
        assert trace[1]["similarity_score"] >= 0.55

    def test_similarity_below_recorded(self, em_chroma):
        # Single irrelevant hint → dropped at similarity → no survivors path,
        # but the trace still records it (the "why wasn't it used" answer).
        eng = NLFeedbackEngine(em_chroma)
        _insert_nl_hint(em_chroma, 1, "click Sign In not Sign Up",
                        "log into my account")
        em_chroma.reconcile_anchors()
        trace = {}
        hints, ids = eng.get_hints_with_ids(
            "search for laptops and verify the count", "http://x.com",
            "planner", selection_trace=trace)
        assert ids == []
        assert trace[1]["available"] == 0
        assert trace[1]["drop_reason"] == "similarity_below"
        assert trace[1]["similarity_score"] is not None

    def test_no_anchor_recorded(self, em_chroma):
        # id 1 has a row but no learning_anchors doc; id 2 keeps its doc so the
        # store is non-empty (count>0) → id 1 is no_anchor, not fail-open.
        eng = NLFeedbackEngine(em_chroma)
        _insert_nl_hint(em_chroma, 1, "hint one needs no doc",
                        "count the products on the page")
        _insert_nl_hint(em_chroma, 2, "hint two keeps its doc",
                        "count the products on the page")
        em_chroma.reconcile_anchors()
        em_chroma._learning_anchors.delete(ids=["nl:1"])     # drop id 1's doc
        trace = {}
        eng.get_hints_with_ids(
            "how many products are on the page", "http://x.com", "planner",
            selection_trace=trace)
        assert trace[1]["drop_reason"] == "no_anchor"
        assert trace[1]["similarity_score"] is None
        assert trace[1]["available"] == 0

    def test_dedup_recorded(self, in_memory_em):
        # ChromaDB unavailable → similarity fails open (both survive); the two
        # near-identical feedback texts then collide at dedup (>0.6 overlap).
        eng = NLFeedbackEngine(in_memory_em)
        _insert_nl_hint(in_memory_em, 1,
                        "use Get Element Count for counting items", "anchor a")
        _insert_nl_hint(in_memory_em, 2,
                        "use Get Element Count for counting things", "anchor b")
        in_memory_em._writer_conn.execute(
            "UPDATE nl_feedback_corrections SET last_seen='2026-01-02' WHERE id=1")
        in_memory_em._writer_conn.commit()
        trace = {}
        hints, ids = eng.get_hints_with_ids(
            "any query", "http://x.com", "planner", selection_trace=trace)
        assert ids == [1]                            # id 1 (newer) wins
        assert trace[1]["available"] == 1
        assert trace[2]["available"] == 0
        assert trace[2]["drop_reason"] == "dedup"
        assert trace[1]["similarity_score"] is None  # fail-open → no score

    def test_internal_cap_recorded(self, in_memory_em):
        # 6 distinct hints, all fail-open survivors, none deduped → the NL
        # internal [:5] cap drops the 6th (lowest last_seen) as "cap".
        eng = NLFeedbackEngine(in_memory_em)
        texts = ["alpha advice one", "bravo advice two", "charlie advice three",
                 "delta advice four", "echo advice five", "foxtrot advice six"]
        for i, t in enumerate(texts, start=1):
            _insert_nl_hint(in_memory_em, i, t, f"anchor {i}")
            in_memory_em._writer_conn.execute(
                "UPDATE nl_feedback_corrections SET last_seen=? WHERE id=?",
                (f"2026-01-{10 - i:02d}", i))       # id1 newest … id6 oldest
        in_memory_em._writer_conn.commit()
        trace = {}
        hints, ids = eng.get_hints_with_ids(
            "any query", "http://x.com", "planner", selection_trace=trace)
        assert set(ids) == {1, 2, 3, 4, 5}
        assert trace[6]["drop_reason"] == "cap"
        assert trace[6]["available"] == 0
        for i in range(1, 6):
            assert trace[i]["available"] == 1

    def test_fail_open_survivor_has_null_sim(self, in_memory_em):
        eng = NLFeedbackEngine(in_memory_em)
        _insert_nl_hint(in_memory_em, 1, "a single distinct hint", "anchor a")
        trace = {}
        hints, ids = eng.get_hints_with_ids(
            "any query", "http://x.com", "planner", selection_trace=trace)
        assert ids == [1]
        assert trace[1]["available"] == 1
        assert trace[1]["drop_reason"] is None
        assert trace[1]["similarity_score"] is None  # fail-open

    def test_empty_query_leaves_trace_empty(self, em_chroma):
        eng = NLFeedbackEngine(em_chroma)
        _insert_nl_hint(em_chroma, 1, "h", "an anchor query")
        em_chroma.reconcile_anchors()
        trace = {}
        eng.get_hints_with_ids("", "http://x.com", "planner",
                               selection_trace=trace)
        assert trace == {}                           # nothing evaluated


# ===================================================================
# Bank 1 — learn_from_feedback anchor write
# ===================================================================


class TestLearnFromFeedbackAnchor:
    def test_new_hint_stores_anchor_and_doc(self, em_chroma):
        eng = NLFeedbackEngine(em_chroma)
        record = SimpleNamespace(
            workflow_id="wf-1",
            user_query="add a laptop to my cart",
            domain="amazon.com",
            url="https://amazon.com/x",
            failure_category=None,
        )
        eng.learn_from_feedback(
            record,
            {"feedback_text": "wait for the spinner before clicking",
             "category": "timing"},
        )
        row = em_chroma._writer_conn.execute(
            "SELECT id, anchor_query FROM nl_feedback_corrections "
            "WHERE feedback_text LIKE 'wait for the spinner%'"
        ).fetchone()
        assert row["anchor_query"] == "add a laptop to my cart"
        doc_ids = set(em_chroma._learning_anchors.get(include=[])["ids"])
        assert f"nl:{row['id']}" in doc_ids

    def test_reinforced_hint_keeps_original_anchor(self, em_chroma):
        eng = NLFeedbackEngine(em_chroma)
        insight = {"feedback_text": "some correction text", "category": "timing"}
        eng.learn_from_feedback(
            SimpleNamespace(workflow_id="wf-1", user_query="the first query",
                            domain="x.com", url="https://x.com",
                            failure_category=None),
            insight,
        )
        # Same feedback_text + domain + scope → UPSERT (reinforcement).
        eng.learn_from_feedback(
            SimpleNamespace(workflow_id="wf-2", user_query="a totally different query",
                            domain="x.com", url="https://x.com",
                            failure_category=None),
            insight,
        )
        rows = em_chroma._writer_conn.execute(
            "SELECT anchor_query, evidence_count FROM nl_feedback_corrections "
            "WHERE feedback_text LIKE 'some correction%'"
        ).fetchall()
        assert len(rows) == 1                         # deduped via UPSERT
        assert rows[0]["evidence_count"] == 2
        assert rows[0]["anchor_query"] == "the first query"   # anchor frozen


# ===================================================================
# Bank 2 — AntiPatternEngine._find_matching_anti_patterns similarity filter
# ===================================================================


class TestBank2Retrieval:
    def test_empty_query_returns_empty(self, em_chroma):
        eng = AntiPatternEngine(em_chroma)
        _insert_anti(em_chroma, 1, "C1", "verify products have prices",
                     score=0.6, evidence_count=5)
        em_chroma.reconcile_anchors()
        assert eng._find_matching_anti_patterns("") == []
        assert eng._find_matching_anti_patterns("   ") == []

    def test_semantic_match(self, em_chroma):
        eng = AntiPatternEngine(em_chroma)
        _insert_anti(em_chroma, 1, "C1",
                     "verify every product on the page has a price",
                     score=0.6, evidence_count=5)
        em_chroma.reconcile_anchors()
        matches = eng._find_matching_anti_patterns(
            "check that all products on the page show their prices")
        assert [m["id"] for m in matches] == [1]

    def test_drops_unrelated(self, em_chroma):
        eng = AntiPatternEngine(em_chroma)
        _insert_anti(em_chroma, 1, "C1",
                     "verify every product on the page has a price",
                     score=0.6, evidence_count=5)
        em_chroma.reconcile_anchors()
        matches = eng._find_matching_anti_patterns(
            "log in and open the account settings page")
        assert matches == []

    def test_score_evidence_gate_runs_before_filter(self, em_chroma):
        # evidence_count below the gate → row never reaches the filter.
        eng = AntiPatternEngine(em_chroma)
        _insert_anti(em_chroma, 1, "C1",
                     "verify every product on the page has a price",
                     score=0.6, evidence_count=2)
        em_chroma.reconcile_anchors()
        matches = eng._find_matching_anti_patterns(
            "check that all products on the page show their prices")
        assert matches == []


# ===================================================================
# Bank 2 — learn() anti-pattern anchor write
# ===================================================================


class TestBank2LearnAnchor:
    def test_learn_creates_anti_pattern_anchor(self, em_chroma):
        eng = AntiPatternEngine(em_chroma)
        failing = SimpleNamespace(
            test_status="failed",
            failure_category="C1",
            error_message="element not found: .price",
            user_query="get the price of every product",
            robot_code="Get Text    css=.price",
            failed_keyword="Get Text",
            domain="shop.com",
        )
        eng.learn(failing)
        row = em_chroma._writer_conn.execute(
            "SELECT id, query_pattern FROM anti_patterns "
            "WHERE failure_category = 'C1'"
        ).fetchone()
        assert row["query_pattern"] == "get the price of every product"
        doc_ids = set(em_chroma._learning_anchors.get(include=[])["ids"])
        assert f"anti:{row['id']}" in doc_ids

    def test_learn_reinforce_keeps_single_anchor(self, em_chroma):
        eng = AntiPatternEngine(em_chroma)
        common = dict(test_status="failed", failure_category="C1",
                      error_message="err", robot_code="X",
                      failed_keyword=None, domain=None)
        eng.learn(SimpleNamespace(
            user_query="get the price of every single product item", **common))
        eng.learn(SimpleNamespace(
            user_query="get the price of every single product now", **common))
        rows = em_chroma._writer_conn.execute(
            "SELECT id, evidence_count FROM anti_patterns"
        ).fetchall()
        assert len(rows) == 1                       # merged, not duplicated
        assert rows[0]["evidence_count"] == 2
        # The merge branch must not add a second anchor doc.
        doc_ids = set(em_chroma._learning_anchors.get(include=[])["ids"])
        assert doc_ids == {f"anti:{rows[0]['id']}"}

    def test_check_for_correct_alternative_semantic(self, em_chroma):
        # PASS-path still backfills correct_alternative under semantic matching.
        eng = AntiPatternEngine(em_chroma)
        em_chroma._writer_conn.execute(
            "INSERT INTO anti_patterns "
            "(id, failure_category, query_pattern, bad_code_snippet, "
            " error_message, score, evidence_count, last_seen) "
            "VALUES (1, 'C1', 'verify every product on the page has a price', "
            "'Get Text    css=.bad', 'element not found', 0.6, 5, '2026-01-01')"
        )
        em_chroma._writer_conn.commit()
        em_chroma.reconcile_anchors()
        passing = SimpleNamespace(
            test_status="passed",
            user_query="check that all products on the page show their prices",
            robot_code="Get Element Count    css=.good",
            domain=None, failure_category=None, failed_keyword=None,
        )
        eng.learn(passing)
        row = em_chroma._writer_conn.execute(
            "SELECT correct_alternative FROM anti_patterns WHERE id = 1"
        ).fetchone()
        assert row["correct_alternative"] is not None
        assert "Get Element Count" in row["correct_alternative"]


# ===================================================================
# LearningMetricsTracker.record_execution — was_holdout persistence
# ===================================================================


class TestRecordExecutionHoldout:
    def test_was_holdout_persisted(self, in_memory_em):
        tracker = LearningMetricsTracker(in_memory_em)
        tracker.record_execution(
            workflow_id="wf-h1", user_query="q", is_first_attempt=True,
            hints_available=3, hints_injected=0, hint_sources=[],
            llm_calls=1, llm_cost=0.0, test_passed=True, was_holdout=True,
        )
        row = in_memory_em._writer_conn.execute(
            "SELECT was_holdout FROM learning_metrics WHERE workflow_id = 'wf-h1'"
        ).fetchone()
        assert row["was_holdout"] == 1

    def test_was_holdout_defaults_false(self, in_memory_em):
        tracker = LearningMetricsTracker(in_memory_em)
        tracker.record_execution(
            workflow_id="wf-h2", user_query="q", is_first_attempt=True,
            hints_available=0, hints_injected=0, hint_sources=[],
            llm_calls=1, llm_cost=0.0, test_passed=False,
        )
        row = in_memory_em._writer_conn.execute(
            "SELECT was_holdout FROM learning_metrics WHERE workflow_id = 'wf-h2'"
        ).fetchone()
        assert row["was_holdout"] == 0


# ===================================================================
# get_effectiveness_report — Cat C / honest_lift (R7)
# ===================================================================


class TestEffectivenessReport:
    def test_cat_a_excludes_holdout_runs(self, in_memory_em):
        tracker = LearningMetricsTracker(in_memory_em)
        tracker._min_sample_size = 2
        # 3 genuine Cat A: no hints, not holdout.
        _seed_metric(in_memory_em, hints_injected=0, was_holdout=0,
                     test_passed=1, n=3)
        # 2 holdout runs: hints_injected=0 but was_holdout=1 — must NOT be Cat A.
        _seed_metric(in_memory_em, hints_injected=0, was_holdout=1,
                     test_passed=0, n=2)
        nc = tracker.get_effectiveness_report()["natural_comparison"]
        assert nc["no_hints_available"]["total"] == 3
        assert nc["holdout_suppressed"]["total"] == 2

    def test_honest_lift_is_cat_b_minus_cat_c(self, in_memory_em):
        tracker = LearningMetricsTracker(in_memory_em)
        tracker._min_sample_size = 2
        # Cat B: 4 hint-injected runs, 3 passed → 0.75.
        _seed_metric(in_memory_em, hints_injected=2, was_holdout=0,
                     test_passed=1, n=3)
        _seed_metric(in_memory_em, hints_injected=2, was_holdout=0,
                     test_passed=0, n=1)
        # Cat C: 4 holdout runs, 2 passed → 0.50.
        _seed_metric(in_memory_em, hints_injected=0, was_holdout=1,
                     test_passed=1, n=2)
        _seed_metric(in_memory_em, hints_injected=0, was_holdout=1,
                     test_passed=0, n=2)
        nc = tracker.get_effectiveness_report()["natural_comparison"]
        assert nc["honest_lift"] == 0.25

    def test_honest_lift_null_when_cat_c_insufficient(self, in_memory_em):
        tracker = LearningMetricsTracker(in_memory_em)
        tracker._min_sample_size = 5
        _seed_metric(in_memory_em, hints_injected=2, was_holdout=0,
                     test_passed=1, n=6)
        _seed_metric(in_memory_em, hints_injected=0, was_holdout=1,
                     test_passed=1, n=2)        # only 2 Cat C — below 5
        nc = tracker.get_effectiveness_report()["natural_comparison"]
        assert nc["honest_lift"] is None
        assert nc["lift_is_biased"] is True       # legacy lift still labelled


# ===================================================================
# FeedbackLoop — init reconcile, model_version stamp, health status
# ===================================================================


class TestFeedbackLoopInit:
    def test_reconcile_submitted_on_init(self, in_memory_em):
        fl = _make_feedback_loop(in_memory_em)
        submitted = [c.args[0] for c in fl.write_queue.submit.call_args_list]
        assert fl._run_anchor_reconcile in submitted

    def test_model_version_stamped_on_record(self, in_memory_em):
        from src.backend.core.config import settings
        fl = _make_feedback_loop(in_memory_em)
        fl.write_queue.submit.reset_mock()
        fl.process_execution(
            workflow_id="wf-mv", user_query="do a thing", url="http://x.com",
            robot_code="X", test_status="passed",
        )
        # First submit in process_execution is execution_memory.store(record).
        record = fl.write_queue.submit.call_args_list[0].args[1]
        from src.backend.crew_ai.llm_provider_routing import resolve_model_string
        raw_model = (
            settings.LOCAL_MODEL if settings.MODEL_PROVIDER == "local"
            else settings.ONLINE_MODEL
        )
        expected = resolve_model_string(settings.MODEL_PROVIDER, raw_model)
        assert record.model_version == expected

    def test_was_holdout_forwarded_to_record_execution(self, in_memory_em):
        fl = _make_feedback_loop(in_memory_em)
        fl.write_queue.submit.reset_mock()
        fl.process_execution(
            workflow_id="wf-wh", user_query="q", url="http://x.com",
            robot_code="X", test_status="passed", was_holdout=True,
        )
        # Find the record_execution submit and check its was_holdout kwarg.
        rec_calls = [
            c for c in fl.write_queue.submit.call_args_list
            if getattr(c.args[0], "__name__", "") == "record_execution"
        ]
        assert rec_calls and rec_calls[0].kwargs["was_holdout"] is True

    def test_health_check_exception_does_not_trip_breaker(self, in_memory_em):
        fl = _make_feedback_loop(in_memory_em)
        with patch.object(fl, "get_health_status",
                          side_effect=RuntimeError("boom")), \
             patch.object(fl.circuit_breaker, "record_error") as mock_err:
            fl.process_execution(
                workflow_id="wf-hc", user_query="q", url="http://x.com",
                robot_code="X", test_status="passed",
            )
        mock_err.assert_not_called()


class TestHealthStatus:
    def test_ok(self, fresh_em):
        fl = _make_feedback_loop(fresh_em)
        assert fl.get_health_status() == "OK"

    def test_chromadb_init_failed_is_failed(self, fresh_em):
        fl = _make_feedback_loop(fresh_em)
        fl.execution_memory._chroma_client = (
            ExecutionMemory._CHROMADB_INIT_FAILED
        )
        assert fl.get_health_status() == "FAILED"

    def test_chromadb_not_yet_initialized_is_not_failed(self, fresh_em):
        fl = _make_feedback_loop(fresh_em)
        fl.execution_memory._chroma_client = None    # never inited (C19)
        assert fl.get_health_status() == "OK"

    def test_health_does_not_trigger_chromadb_init(self, fresh_em):
        fl = _make_feedback_loop(fresh_em)
        fl.get_health_status()
        # A health check must not cause an 80 MB ONNX load.
        assert fl.execution_memory._chroma_client is None

    def test_detects_null_anchor(self, fresh_em):
        fl = _make_feedback_loop(fresh_em)
        fresh_em._writer_conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(feedback_text, anchor_query, is_active, created_at, last_seen) "
            "VALUES ('h', NULL, 1, '2026-01-01', '2026-01-01')"
        )
        fresh_em._writer_conn.commit()
        assert fl.get_health_status() == "DEGRADED"

    def test_optimization_init_ok_flag_drives_status(self, fresh_em):
        fl = _make_feedback_loop(fresh_em)
        assert fl.get_health_status() == "OK"
        fl._optimization_init_ok = False
        assert fl.get_health_status() == "DEGRADED"

    def test_lifetime_counter_does_not_drive_status(self, fresh_em):
        # C20: a monotonic lifetime counter must not pin the status.
        fl = _make_feedback_loop(fresh_em)
        fl._optimization_init_failures = 99
        fl._optimization_init_ok = True
        assert fl.get_health_status() == "OK"

    def test_reconcile_missing_count_is_degraded(self, fresh_em):
        fl = _make_feedback_loop(fresh_em)
        fl.last_reconcile = {"ran_at": "x", "checked": 5, "missing_count": 2}
        assert fl.get_health_status() == "DEGRADED"
        fl.last_reconcile = {"ran_at": "x", "checked": 5, "missing_count": 0}
        assert fl.get_health_status() == "OK"

    def test_circuit_breaker_tripped_is_failed(self, fresh_em):
        fl = _make_feedback_loop(fresh_em)
        fl.circuit_breaker.is_enabled = lambda: False
        assert fl.get_health_status() == "FAILED"

    def test_status_recovers_without_manual_reset(self, fresh_em):
        fl = _make_feedback_loop(fresh_em)
        fl._optimization_init_ok = False
        assert fl.get_health_status() == "DEGRADED"
        fl._optimization_init_ok = True             # fix the root cause
        assert fl.get_health_status() == "OK"        # self-clears

    def test_disabled_when_optimization_off(self, fresh_em):
        fl = _make_feedback_loop(fresh_em)
        from src.backend.core.config import settings
        original = settings.OPTIMIZATION_ENABLED
        settings.OPTIMIZATION_ENABLED = False
        try:
            assert fl.get_health_status() == "DISABLED"
        finally:
            settings.OPTIMIZATION_ENABLED = original


# ===================================================================
# SmartKeywordProvider — Bank 1 per-workflow cache + R7 holdout
# ===================================================================


class TestSmartKeywordProvider:
    def test_per_workflow_cache(self, in_memory_em):
        # Bank 1 retrieval runs once across the 2 per-workflow role calls.
        provider = _make_provider(in_memory_em)
        mock_nl = MagicMock()
        mock_nl.get_hints_with_ids.return_value = ([], [])
        provider._get_nl_feedback_engine = lambda: mock_nl
        for role in ("planner", "assembler"):
            provider._get_learning_hints(role, "the same query", "http://x.com")
        assert mock_nl.get_hints_with_ids.call_count == 1

    def test_cache_misses_on_different_query(self, in_memory_em):
        provider = _make_provider(in_memory_em)
        mock_nl = MagicMock()
        mock_nl.get_hints_with_ids.return_value = ([], [])
        provider._get_nl_feedback_engine = lambda: mock_nl
        provider._get_learning_hints("planner", "query one", "http://x.com")
        provider._get_learning_hints("planner", "query two", "http://x.com")
        assert mock_nl.get_hints_with_ids.call_count == 2

    def test_holdout_suppresses_available_hints(self, in_memory_em):
        provider = _make_provider(in_memory_em, holdout=True)
        mock_nl = MagicMock()
        mock_nl.get_hints_with_ids.return_value = (["a useful hint"], [1])
        provider._get_nl_feedback_engine = lambda: mock_nl
        result = provider._get_learning_hints("planner", "q", "http://x.com")
        assert result["text"] is None          # suppressed
        assert result["count"] == 0
        assert result["available"] == 1        # real count preserved
        assert provider.was_holdout is True

    def test_holdout_on_hintless_workflow_is_not_holdout(self, in_memory_em):
        # Coin up but no hints existed → still Cat A, not Cat C.
        provider = _make_provider(in_memory_em, holdout=True)
        mock_nl = MagicMock()
        mock_nl.get_hints_with_ids.return_value = ([], [])
        provider._get_nl_feedback_engine = lambda: mock_nl
        result = provider._get_learning_hints("planner", "q", "http://x.com")
        assert result["available"] == 0
        assert provider.was_holdout is False

    def test_no_holdout_injects_hints_normally(self, in_memory_em):
        provider = _make_provider(in_memory_em, holdout=False)
        mock_nl = MagicMock()
        mock_nl.get_hints_with_ids.return_value = (["a useful hint"], [1])
        provider._get_nl_feedback_engine = lambda: mock_nl
        result = provider._get_learning_hints("planner", "q", "http://x.com")
        assert result["text"] is not None
        assert result["count"] == 1
        assert result["available"] == 1
        assert provider.was_holdout is False

    def test_get_agent_context_surfaces_holdout_available(self, in_memory_em):
        # On a holdout run, hints_available reports the real count even
        # though no hint text is injected.
        provider = _make_provider(in_memory_em, holdout=True)
        mock_nl = MagicMock()
        mock_nl.get_hints_with_ids.return_value = (["a useful hint"], [1])
        provider._get_nl_feedback_engine = lambda: mock_nl
        result = provider.get_agent_context("q", "planner", "http://x.com")
        assert result.hints_available == 1
        assert result.hints_count == 0
        assert result.hint_text == ""
        assert provider.was_holdout is True


# ===================================================================
# SmartKeywordProvider — F2b stage-5 overlay + selection_trace surfacing
# ===================================================================


class TestSelectionTraceOverlay:
    """F2b — the provider overlays stage 5 (holdout / outer cap / injected)
    onto the cached stage 2-4 fragment on a COPY, and surfaces the result on
    AgentContextResult.selection_trace. Uses a real provider + in_memory_em
    (similarity fails open → deterministic, no embedding flakiness)."""

    def test_injected_hints_marked(self, in_memory_em):
        _insert_nl_hint(in_memory_em, 1, "first distinct hint about carts", "anchor a")
        _insert_nl_hint(in_memory_em, 2, "second distinct hint about search", "anchor b")
        provider = _make_provider(in_memory_em, holdout=False)
        result = provider.get_agent_context("any query", "planner", "http://x.com")
        trace = result.selection_trace
        assert trace is not None
        assert set(result.nl_injected_ids) == {1, 2}
        for i in (1, 2):
            assert trace[i]["available"] == 1
            assert trace[i]["injected"] == 1
            assert trace[i]["drop_reason"] is None

    def test_holdout_marks_available_as_holdout(self, in_memory_em):
        _insert_nl_hint(in_memory_em, 1, "a distinct cart hint", "anchor a")
        provider = _make_provider(in_memory_em, holdout=True)
        result = provider.get_agent_context("any query", "planner", "http://x.com")
        trace = result.selection_trace
        assert trace is not None
        assert result.nl_injected_ids == ()
        assert trace[1]["available"] == 1
        assert trace[1]["injected"] == 0
        assert trace[1]["drop_reason"] == "holdout"

    def test_outer_cap_marks_cap(self, in_memory_em):
        # 5 structural hints (high priority, added first) fill the tier-simple
        # [:5] outer cap → the 3 available NL hints are pushed out → "cap".
        # NL texts must be word-disjoint or _deduplicate_hints would drop them
        # as "dedup" before the outer cap is ever reached.
        nl_texts = ["click the add to cart button firmly",
                    "wait for spinner before submitting form",
                    "select size dropdown then confirm choice"]
        for i, t in enumerate(nl_texts, start=1):
            _insert_nl_hint(in_memory_em, i, t, f"anchor {i}")
        provider = _make_provider(in_memory_em, holdout=False)
        mock_struct = MagicMock()
        mock_struct.get_hints.return_value = [f"structural hint {j}" for j in range(5)]
        provider._get_structural_engine = lambda: mock_struct
        result = provider.get_agent_context("any query", "planner", "http://x.com")
        trace = result.selection_trace
        assert result.nl_injected_ids == ()
        for i in range(1, 4):
            assert trace[i]["available"] == 1
            assert trace[i]["injected"] == 0
            assert trace[i]["drop_reason"] == "cap"

    def test_cache_not_mutated_across_agents(self, in_memory_em):
        # The cached stage 2-4 fragment is shared across agents; the per-agent
        # overlay must run on a COPY (the robustness fix). The cached fragment
        # stays raw (injected=0, drop_reason None for survivors).
        _insert_nl_hint(in_memory_em, 1, "a distinct cart hint", "anchor a")
        provider = _make_provider(in_memory_em, holdout=False)
        r1 = provider.get_agent_context("any query", "planner", "http://x.com")
        _, _, cached_trace = provider._nl_hints_cache[("any query", "http://x.com")]
        assert cached_trace[1]["available"] == 1
        assert cached_trace[1]["injected"] == 0          # cache NOT mutated
        assert cached_trace[1]["drop_reason"] is None
        assert r1.selection_trace[1]["injected"] == 1    # planner's copy overlaid
        r2 = provider.get_agent_context("any query", "assembler", "http://x.com")
        assert r2.selection_trace[1]["injected"] == 1    # cache-hit agent correct
        assert r1.selection_trace is not r2.selection_trace

    def test_trace_off_returns_none_and_keeps_injection(self, in_memory_em):
        from src.backend.core.config import settings
        _insert_nl_hint(in_memory_em, 1, "a distinct cart hint", "anchor a")
        provider = _make_provider(in_memory_em, holdout=False)
        original = settings.HINT_TRACE_ENABLED
        settings.HINT_TRACE_ENABLED = False
        try:
            result = provider.get_agent_context("any query", "planner", "http://x.com")
        finally:
            settings.HINT_TRACE_ENABLED = original
        assert result.selection_trace is None            # capture off
        assert result.nl_injected_ids == (1,)            # selection unaffected


# ===================================================================
# F2c — reconcile_selection_traces (crew.py union-seam aggregation)
# ===================================================================


class TestReconcileSelectionTraces:
    """F2c — fold per-agent NL selection traces into one record keyed by
    hint_id with injected-wins reconciliation against the union that
    attribution credits. Pure/observability — never mutates the union."""

    @staticmethod
    def _entry(**kw):
        base = {"scope": "global", "source": "nl", "priority": "high",
                "similarity_score": None, "available": 0, "injected": 0,
                "drop_reason": None}
        base.update(kw)
        return base

    def _fn(self):
        from src.backend.crew_ai.optimization.smart_keyword_provider import (
            reconcile_selection_traces,
        )
        return reconcile_selection_traces

    def test_both_none_returns_none(self):
        assert self._fn()([None, None], []) is None

    def test_one_none_uses_the_other(self):
        planner = {1: self._entry(available=1)}
        out = self._fn()([planner, None], [1])
        assert out[1]["injected"] == 1 and out[1]["drop_reason"] is None

    def test_injected_iff_in_union_and_union_not_mutated(self):
        # FR4 guard #2: injected == (id in union); the union is untouched.
        planner = {1: self._entry(available=1),
                   2: self._entry(available=1, drop_reason="cap")}
        assembler = {1: self._entry(available=1, drop_reason="cap"),
                     2: self._entry(available=1, drop_reason="cap")}
        union = [1]
        snapshot = list(union)
        out = self._fn()([planner, assembler], union)
        assert union == snapshot                          # not mutated
        for hid, e in out.items():
            assert (e["injected"] == 1) == (hid in union)
        assert out[1]["drop_reason"] is None
        assert out[2]["injected"] == 0 and out[2]["drop_reason"] == "cap"

    def test_injected_wins_over_per_agent_cap(self):
        # The real divergence: injected for the planner, capped for the
        # assembler → union has it → injected (matches what attribution credits).
        planner = {1: self._entry(available=1, injected=1)}
        assembler = {1: self._entry(available=1, injected=0, drop_reason="cap")}
        out = self._fn()([planner, assembler], [1])
        assert out[1]["injected"] == 1 and out[1]["drop_reason"] is None

    def test_drop_reason_precedence(self):
        # not injected, agents disagree → highest precedence wins.
        a = {1: self._entry(drop_reason="cap")}
        b = {1: self._entry(drop_reason="holdout")}
        assert self._fn()([a, b], [])[1]["drop_reason"] == "holdout"
        c = {1: self._entry(drop_reason="dedup")}
        d = {1: self._entry(drop_reason="similarity_below")}
        assert self._fn()([c, d], [])[1]["drop_reason"] == "similarity_below"

    def test_similarity_score_first_non_null(self):
        a = {1: self._entry(available=1, similarity_score=None)}
        b = {1: self._entry(available=1, similarity_score=0.82)}
        out = self._fn()([a, b], [1])
        assert abs(out[1]["similarity_score"] - 0.82) < 1e-9


# ===================================================================
# F2d — store_hint_workflow_trace (writer-thread INSERT)
# ===================================================================


class TestHintWorkflowTraceStore:
    """F2d — store_hint_workflow_trace persists the reconciled trace on the
    writer thread (ON CONFLICT DO NOTHING; attribution_* left NULL for F2e)."""

    @staticmethod
    def _trace():
        return {
            5: {"scope": "global", "source": "nl", "priority": "high",
                "similarity_score": 0.82, "available": 1, "injected": 1,
                "drop_reason": None},
            9: {"scope": "domain", "source": "nl", "priority": "high",
                "similarity_score": 0.40, "available": 0, "injected": 0,
                "drop_reason": "similarity_below"},
        }

    def test_inserts_rows(self, in_memory_em):
        in_memory_em.store_hint_workflow_trace("wf-1", self._trace())
        with in_memory_em.read_conn() as conn:
            rows = conn.execute(
                "SELECT hint_id, scope, source, priority, similarity_score, "
                "       available, injected, drop_reason "
                "FROM hint_workflow_trace WHERE workflow_id='wf-1' "
                "ORDER BY hint_id"
            ).fetchall()
        assert len(rows) == 2
        assert rows[0]["hint_id"] == 5 and rows[0]["injected"] == 1
        assert rows[0]["drop_reason"] is None
        assert rows[0]["source"] == "nl" and rows[0]["scope"] == "global"
        assert abs(rows[0]["similarity_score"] - 0.82) < 1e-9
        assert rows[1]["hint_id"] == 9 and rows[1]["available"] == 0
        assert rows[1]["drop_reason"] == "similarity_below"

    def test_attribution_columns_null_on_insert(self, in_memory_em):
        in_memory_em.store_hint_workflow_trace("wf-2", self._trace())
        with in_memory_em.read_conn() as conn:
            row = conn.execute(
                "SELECT attribution_bucket, attribution_reason "
                "FROM hint_workflow_trace WHERE workflow_id='wf-2' AND hint_id=5"
            ).fetchone()
        assert row["attribution_bucket"] is None
        assert row["attribution_reason"] is None      # F2e fills these later

    def test_on_conflict_do_nothing_keeps_first(self, in_memory_em):
        in_memory_em.store_hint_workflow_trace(
            "wf-3", {5: {"drop_reason": "cap", "available": 1, "injected": 0}})
        in_memory_em.store_hint_workflow_trace(
            "wf-3", {5: {"drop_reason": "holdout", "available": 1, "injected": 0}})
        with in_memory_em.read_conn() as conn:
            rows = conn.execute(
                "SELECT drop_reason FROM hint_workflow_trace "
                "WHERE workflow_id='wf-3' AND hint_id=5"
            ).fetchall()
        assert len(rows) == 1 and rows[0]["drop_reason"] == "cap"

    def test_empty_trace_is_noop(self, in_memory_em):
        in_memory_em.store_hint_workflow_trace("wf-4", {})
        with in_memory_em.read_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM hint_workflow_trace WHERE workflow_id='wf-4'"
            ).fetchone()[0]
        assert count == 0

    def test_bool_flags_coerced_to_int(self, in_memory_em):
        in_memory_em.store_hint_workflow_trace(
            "wf-5", {7: {"available": True, "injected": False,
                         "drop_reason": "cap"}})
        with in_memory_em.read_conn() as conn:
            row = conn.execute(
                "SELECT available, injected FROM hint_workflow_trace "
                "WHERE workflow_id='wf-5' AND hint_id=7"
            ).fetchone()
        assert row["available"] == 1 and row["injected"] == 0


# ===================================================================
# F2f / G3 — hint_workflow_trace retention prune
# ===================================================================


class TestHintWorkflowTracePrune:
    """G3 — opportunistic, daily-guarded retention prune of the N3 trace.
    Pruning is observability-only: counters live on the hint row, never derived
    from the trace, so a prune can never change a count."""

    @staticmethod
    def _insert(em, workflow_id, hint_id, created_at):
        em._writer_conn.execute(
            "INSERT INTO hint_workflow_trace "
            "(workflow_id, hint_id, source, available, injected, created_at) "
            "VALUES (?, ?, 'nl', 1, 1, ?)",
            (workflow_id, hint_id, created_at),
        )
        em._writer_conn.commit()

    def test_prune_deletes_old_keeps_recent(self, in_memory_em):
        old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self._insert(in_memory_em, "wf-old", 1, old)
        self._insert(in_memory_em, "wf-new", 2, recent)
        deleted = in_memory_em.prune_hint_workflow_trace(90)
        assert deleted == 1
        with in_memory_em.read_conn() as conn:
            ids = {r["hint_id"] for r in conn.execute(
                "SELECT hint_id FROM hint_workflow_trace").fetchall()}
        assert ids == {2}                       # only the recent row survives

    def test_prune_zero_keeps_all(self, in_memory_em):
        old = (datetime.now(timezone.utc) - timedelta(days=1000)).isoformat()
        self._insert(in_memory_em, "wf-old", 1, old)
        deleted = in_memory_em.prune_hint_workflow_trace(0)     # 0 = keep all
        assert deleted == 0
        with in_memory_em.read_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM hint_workflow_trace").fetchone()[0]
        assert count == 1

    def test_maybe_prune_respects_daily_guard(self, in_memory_em):
        old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        self._insert(in_memory_em, "wf-old", 1, old)
        # Guard armed (pruned just now) → _maybe_prune is a no-op.
        in_memory_em._last_trace_prune_ts = time.time()
        in_memory_em._maybe_prune_hint_workflow_trace()
        with in_memory_em.read_conn() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM hint_workflow_trace").fetchone()[0] == 1
        # Guard expired → it runs (HINT_TRACE_RETENTION_DAYS=90 default).
        in_memory_em._last_trace_prune_ts = 0.0
        in_memory_em._maybe_prune_hint_workflow_trace()
        with in_memory_em.read_conn() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM hint_workflow_trace").fetchone()[0] == 0


# ===================================================================
# learning_endpoints — anchor_query field, /health, effectiveness in stats
# ===================================================================


class TestLearningEndpoints:
    def test_create_hint_rejects_missing_anchor_query(self):
        from pydantic import ValidationError
        from src.backend.api.learning_endpoints import HintCreateRequest
        with pytest.raises(ValidationError):
            HintCreateRequest(
                feedback_text="a hint", scope="global", actor="admin",
            )

    def test_create_hint_rejects_short_anchor(self):
        from src.backend.api.learning_endpoints import (
            HintCreateRequest, create_hint,
        )
        from fastapi import HTTPException
        req = HintCreateRequest(
            feedback_text="a hint", anchor_query="ab",   # < 3 chars
            scope="global", actor="admin",
        )
        with pytest.raises(HTTPException) as exc:
            create_hint(req, fb=MagicMock())
        assert exc.value.status_code == 400

    def test_create_hint_stores_anchor_and_enqueues(self, tmp_path):
        from src.backend.api.learning_endpoints import (
            HintCreateRequest, create_hint,
        )
        db_path = str(tmp_path / "admin.db")
        em = ExecutionMemory(db_path=db_path)          # migrates to v11
        fb = MagicMock()
        fb.execution_memory = em
        req = HintCreateRequest(
            feedback_text="use Wait For Elements State, not Sleep",
            anchor_query="verify the product list loads after filtering",
            scope="global", run_triage=False, actor="admin",
        )
        test_conn = sqlite3.connect(db_path)
        test_conn.row_factory = sqlite3.Row
        with patch("src.backend.api.learning_endpoints._admin_conn",
                   return_value=test_conn):
            result = create_hint(req, fb=fb)
        assert result["created"] is True
        assert result["hint"]["anchor_query"] == (
            "verify the product list loads after filtering"
        )
        # The learning_anchors add was enqueued on the write queue.
        assert any(
            c.args and c.args[0] == em.add_anchor
            for c in fb.write_queue.submit.call_args_list
        )
        em.close()

    def test_health_endpoint_none_fb_optimization_on_is_failed(self):
        from src.backend.api import learning_endpoints
        from src.backend.core.config import settings
        original = settings.OPTIMIZATION_ENABLED
        settings.OPTIMIZATION_ENABLED = True
        try:
            with patch.object(learning_endpoints, "get_feedback_loop",
                              return_value=None):
                assert learning_endpoints.get_learning_health() == {
                    "status": "FAILED"
                }
        finally:
            settings.OPTIMIZATION_ENABLED = original

    def test_health_endpoint_none_fb_optimization_off_is_disabled(self):
        from src.backend.api import learning_endpoints
        from src.backend.core.config import settings
        original = settings.OPTIMIZATION_ENABLED
        settings.OPTIMIZATION_ENABLED = False
        try:
            with patch.object(learning_endpoints, "get_feedback_loop",
                              return_value=None):
                assert learning_endpoints.get_learning_health() == {
                    "status": "DISABLED"
                }
        finally:
            settings.OPTIMIZATION_ENABLED = original

    def test_health_endpoint_returns_feedback_loop_status(self):
        from src.backend.api import learning_endpoints
        fake_fb = MagicMock()
        fake_fb.get_health_status.return_value = "DEGRADED"
        with patch.object(learning_endpoints, "get_feedback_loop",
                          return_value=fake_fb):
            assert learning_endpoints.get_learning_health() == {
                "status": "DEGRADED"
            }

    def test_effectiveness_report_in_stats_payload(self, in_memory_em):
        from src.backend.api.learning_endpoints import get_dashboard_stats
        fl = _make_feedback_loop(in_memory_em)
        result = get_dashboard_stats(fb=fl)
        assert "learning_effectiveness" in result
        assert "natural_comparison" in result["learning_effectiveness"]

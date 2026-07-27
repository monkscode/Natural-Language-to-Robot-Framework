"""Task 31 — hot-path latency patch: per-workflow retrieval caches, similarity
filter round-trip reduction, embed memoization, shared embedder instance, and
exact-name keyword-doc lookup.

Contracts locked here (each was measured as a hot-path cost on 2026-07-18):
- A: anti-pattern + structural retrieval run ONCE per workflow across the two
     role calls (mirrors the existing NL per-workflow cache); formatting stays
     role-specific and byte-identical with the pre-cache behavior.
- B: filter_by_query_similarity runs the COUNT(*) round-trip only on the
     empty-result branch (cold-start disambiguation), never when rows matched.
- C: identical text is embedded once per embedder instance (memo invalidated
     when the client identity changes; failures never cached).
- D: PostgresExecutionMemory obtains its embedder from the shared
     embedding.get_embedder() singleton instead of loading a second ONNX model.
- E: keyword docs for predicted keywords come from an exact-name SQL lookup,
     not an embed + ANN search that is then filtered to exact equality anyway.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.backend.crew_ai.optimization import embedding
from src.backend.crew_ai.optimization.anti_pattern_engine import AntiPatternEngine
# Bound at import (collection) time, BEFORE the autouse _isolate_keyword_store
# fixture patches the class attribute on its module — this is the real class.
from src.backend.crew_ai.optimization.keyword_vector_store import (
    KeywordVectorStore as _RealKeywordVectorStore,
)
from src.backend.crew_ai.optimization.smart_keyword_provider import SmartKeywordProvider
from src.backend.crew_ai.optimization.structural_rule_engine import StructuralRuleEngine


VEC_384 = [0.1] * 384
VEC_LIT = "[" + ",".join("%.7g" % v for v in VEC_384) + "]"


def _fake_embedder():
    m = MagicMock(name="fake_embedder")
    m.embed.return_value = iter([VEC_384])
    # .embed is consumed via next(iter(...)); return a fresh iterator per call
    m.embed.side_effect = lambda texts: iter([VEC_384])
    return m


def _make_provider(em, holdout=False):
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


# ===================================================================
# A — per-workflow retrieval caches (anti-pattern + structural)
# ===================================================================


class TestAntiPatternPerWorkflowCache:
    def test_retrieval_runs_once_across_roles(self, in_memory_em):
        provider = _make_provider(in_memory_em)
        mock_anti = MagicMock()
        mock_anti.get_warnings.return_value = []
        mock_anti.format_hints.return_value = None
        provider._get_anti_pattern_engine = lambda: mock_anti
        for role in ("planner", "assembler"):
            provider._get_learning_hints(role, "the same query", "http://x.com")
        assert mock_anti.get_warnings.call_count == 1
        assert mock_anti.format_hints.call_count == 2

    def test_cache_misses_on_different_query(self, in_memory_em):
        provider = _make_provider(in_memory_em)
        mock_anti = MagicMock()
        mock_anti.get_warnings.return_value = []
        mock_anti.format_hints.return_value = None
        provider._get_anti_pattern_engine = lambda: mock_anti
        provider._get_learning_hints("planner", "query one", "http://x.com")
        provider._get_learning_hints("planner", "query two", "http://x.com")
        assert mock_anti.get_warnings.call_count == 2

    def test_role_formatting_parity(self, in_memory_em):
        """get_hints() == format_hints(get_warnings()) for both roles, and the
        formatted output matches the pre-refactor shape exactly."""
        engine = AntiPatternEngine(in_memory_em)
        warnings = [{
            "failure_category": "A2",
            "error_message": "Element not found: xpath=//div[1]",
            "bad_code_snippet": "Get Text    xpath=//div[1]",
            "correct_alternative": "Get Text    css=.result",
        }]
        planner = engine.format_hints(warnings, "planner")
        assembler = engine.format_hints(warnings, "assembler")
        assert planner == [
            "⚠️ AVOID: Previously failed with category A2: "
            "Element not found: xpath=//div[1]"
        ]
        assert assembler == [
            "⚠️ AVOID: Don't use:\nGet Text    xpath=//div[1]\n"
            "Instead use:\nGet Text    css=.result"
        ]
        assert engine.format_hints(warnings, "identifier") is None
        assert engine.format_hints([], "planner") is None


class TestStructuralPerWorkflowCache:
    def test_retrieval_runs_once_across_roles(self, in_memory_em):
        provider = _make_provider(in_memory_em)
        mock_se = MagicMock()
        mock_se.get_intent_rules.return_value = []
        mock_se.format_hints.return_value = None
        provider._get_structural_engine = lambda: mock_se
        for role in ("planner", "assembler"):
            provider._get_learning_hints(role, "the same query", "http://x.com")
        assert mock_se.get_intent_rules.call_count == 1
        assert mock_se.format_hints.call_count == 2

    def test_role_formatting_parity(self, in_memory_em):
        engine = StructuralRuleEngine(in_memory_em, MagicMock())
        rules = [{
            "required_structure": "loop",
            "required_keywords_json": '["FOR", "END"]',
            "code_template": "FOR    ${row}    IN    @{rows}",
        }]
        planner = engine.format_hints(rules, "planner")
        assembler = engine.format_hints(rules, "assembler")
        assert planner == [
            "⚠️ STRUCTURAL: This query requires loop structure. Use FOR, END"
        ]
        assert assembler == ["📋 TEMPLATE: FOR    ${row}    IN    @{rows}"]
        assert engine.format_hints([], "planner") is None


# ===================================================================
# B — COUNT(*) only on the empty similarity branch
# ===================================================================


class _RecordingConn:
    """Wraps a CompatConnection, recording every SQL string executed."""

    def __init__(self, inner, log):
        self._inner = inner
        self._log = log

    def execute(self, sql, params=None):
        self._log.append(sql)
        if params is None:
            return self._inner.execute(sql)
        return self._inner.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.fixture
def recording_em(in_memory_em):
    """in_memory_em with a fake (deterministic) embedder and SQL recording on
    the read path. Yields (em, executed_sql_list)."""
    from contextlib import contextmanager

    in_memory_em._chroma_client = _fake_embedder()
    in_memory_em._chroma_failed_at = None
    executed = []
    orig_read_conn = in_memory_em.read_conn

    @contextmanager
    def recording_read_conn():
        with orig_read_conn() as conn:
            yield _RecordingConn(conn, executed)

    in_memory_em.read_conn = recording_read_conn
    return in_memory_em, executed


def _insert_anchor(em, record_id, kind="nl", org_id=None, vec_lit=VEC_LIT):
    em._writer_conn.execute(
        "INSERT INTO learning_anchors (anchor_key, kind, record_id, "
        "anchor_query, embedding, org_id) VALUES (?, ?, ?, ?, ?::vector, ?)",
        (f"{kind}:{record_id}", kind, record_id, "anchor query", vec_lit, org_id),
    )
    em._writer_conn.commit()


class TestSimilarityFilterRoundTrips:
    def test_count_skipped_when_rows_found(self, recording_em):
        em, executed = recording_em
        _insert_anchor(em, 1)
        survivors = em.filter_by_query_similarity("a query", [1], "nl")
        assert survivors == {1}          # identical vectors → sim 1.0
        assert not any("COUNT(*)" in s for s in executed), \
            f"COUNT ran on the non-empty branch: {executed}"

    def test_unanchored_candidates_closed_when_anchors_exist(self, recording_em):
        em, executed = recording_em
        _insert_anchor(em, 99)           # anchor exists, but not for candidate 1
        survivors = em.filter_by_query_similarity("a query", [1], "nl")
        assert survivors == set()
        assert any("COUNT(*)" in s for s in executed)

    def test_cold_start_fails_open(self, recording_em):
        em, executed = recording_em      # no anchors at all
        sink = {}
        survivors = em.filter_by_query_similarity(
            "a query", [1, 2], "nl", score_sink=sink)
        assert survivors == {1, 2}
        assert all(sink[i]["outcome"] == "fail_open" for i in (1, 2))

    def test_similarity_below_threshold_drops(self, recording_em):
        em, _ = recording_em
        _insert_anchor(em, 1, vec_lit="[" + ",".join(["1"] + ["0"] * 383) + "]")
        sink = {}
        survivors = em.filter_by_query_similarity(
            "a query", [1], "nl", score_sink=sink)
        assert survivors == set()
        assert sink[1]["outcome"] == "similarity_below"


# ===================================================================
# C — embed memoization (store level and module level)
# ===================================================================


class TestStoreEmbedMemo:
    def test_same_text_embedded_once(self, in_memory_em):
        fake = _fake_embedder()
        in_memory_em._chroma_client = fake
        in_memory_em._chroma_failed_at = None
        lit1 = in_memory_em._embed("the same text")
        lit2 = in_memory_em._embed("the same text")
        assert lit1 == lit2 == VEC_LIT
        assert fake.embed.call_count == 1

    def test_memo_reset_when_client_changes(self, in_memory_em):
        fake1, fake2 = _fake_embedder(), _fake_embedder()
        in_memory_em._chroma_client = fake1
        in_memory_em._chroma_failed_at = None
        in_memory_em._embed("text")
        in_memory_em._chroma_client = fake2
        in_memory_em._embed("text")
        assert fake1.embed.call_count == 1
        assert fake2.embed.call_count == 1

    def test_failure_not_cached(self, in_memory_em):
        fake = MagicMock()
        fake.embed.side_effect = [RuntimeError("onnx died"), iter([VEC_384])]
        in_memory_em._chroma_client = fake
        in_memory_em._chroma_failed_at = None
        assert in_memory_em._embed("text") is None
        assert in_memory_em._embed("text") == VEC_LIT
        assert fake.embed.call_count == 2


@pytest.fixture
def _clean_embedder_state():
    saved = (embedding._embedder, embedding._failed_at)
    embedding._embedder = None
    embedding._failed_at = None
    yield
    embedding._embedder, embedding._failed_at = saved


class TestModuleEmbedMemo:
    def test_same_text_embedded_once(self, _clean_embedder_state):
        fake = _fake_embedder()
        embedding._embedder = fake
        lit1 = embedding.embed_to_literal("memo text")
        lit2 = embedding.embed_to_literal("memo text")
        assert lit1 == lit2 == VEC_LIT
        assert fake.embed.call_count == 1

    def test_failure_then_success_not_cached(self, _clean_embedder_state):
        fake = MagicMock()
        fake.embed.side_effect = [RuntimeError("boom"), iter([VEC_384])]
        embedding._embedder = fake
        assert embedding.embed_to_literal("flaky text") is None
        assert embedding.embed_to_literal("flaky text") == VEC_LIT

    def test_memo_reset_when_embedder_changes(self, _clean_embedder_state):
        fake1, fake2 = _fake_embedder(), _fake_embedder()
        embedding._embedder = fake1
        embedding.embed_to_literal("text")
        embedding._embedder = fake2
        embedding.embed_to_literal("text")
        assert fake1.embed.call_count == 1
        assert fake2.embed.call_count == 1


# ===================================================================
# D — one embedder instance process-wide
# ===================================================================


class TestSharedEmbedderInstance:
    def test_init_uses_shared_singleton(self, in_memory_em):
        shared = _fake_embedder()
        in_memory_em._chroma_client = None
        in_memory_em._chroma_failed_at = None
        with patch.object(embedding, "get_embedder", return_value=shared):
            in_memory_em._init_chromadb()
        assert in_memory_em._chroma_client is shared

    def test_shared_unavailable_sets_failure_state(self, in_memory_em):
        from src.backend.crew_ai.optimization.postgres_execution_memory import (
            PostgresExecutionMemory,
        )
        in_memory_em._chroma_client = None
        in_memory_em._chroma_failed_at = None
        with patch.object(embedding, "get_embedder", return_value=None):
            in_memory_em._init_chromadb()
        assert in_memory_em._chroma_client is \
            PostgresExecutionMemory._CHROMADB_INIT_FAILED
        assert in_memory_em._chroma_failed_at is not None

    def test_explicit_disable_never_loads(self, in_memory_em):
        from src.backend.crew_ai.optimization.postgres_execution_memory import (
            PostgresExecutionMemory,
        )
        in_memory_em._chroma_client = PostgresExecutionMemory._CHROMADB_INIT_FAILED
        in_memory_em._chroma_failed_at = None    # explicit-disable tri-state
        with patch.object(embedding, "get_embedder") as ge:
            in_memory_em._init_chromadb()
            ge.assert_not_called()
        assert in_memory_em._chroma_client is \
            PostgresExecutionMemory._CHROMADB_INIT_FAILED


# ===================================================================
# E — exact-name keyword-doc lookup (no embed, no ANN)
# ===================================================================


class TestExactNameKeywordLookup:
    def test_provider_uses_get_keyword_doc_not_search(self, in_memory_em):
        provider = _make_provider(in_memory_em)
        provider.pruning_enabled = False
        provider.vector_store.get_keyword_doc.return_value = {
            "name": "Click",
            "args": ["selector", "button"],
            "description": "Clicks the element matching selector.",
        }
        ctx = provider._format_predicted_context(["Click"], "assembler", "q")
        provider.vector_store.search.assert_not_called()
        provider.vector_store.get_keyword_doc.assert_called_once_with(
            "browser", "Click")
        assert "• Click(selector, button)" in ctx

    def test_provider_skips_unknown_keyword(self, in_memory_em):
        provider = _make_provider(in_memory_em)
        provider.pruning_enabled = False
        provider.vector_store.get_keyword_doc.return_value = None
        ctx = provider._format_predicted_context(["Nope"], "assembler", "q")
        assert "No predicted keywords available" in ctx

    def test_store_get_keyword_doc_exact_match(self, in_memory_em):
        store = _RealKeywordVectorStore(dsn=in_memory_em.dsn)
        try:
            with store._pool.connection() as conn:
                conn.execute(
                    "INSERT INTO kw_keywords (library, name, args, doc, embedding) "
                    "VALUES (%s, %s, %s, %s, %s::vector) "
                    "ON CONFLICT (library, name) DO NOTHING",
                    ("browser", "Click", '["selector", "button"]',
                     "Clicks the element.", "[" + ",".join(["0"] * 384) + "]"),
                )
                conn.commit()
            kw = store.get_keyword_doc("browser", "Click")
            assert kw == {
                "name": "Click",
                "args": ["selector", "button"],
                "description": "Clicks the element.",
            }
            assert store.get_keyword_doc("browser", "click") is None  # case-sensitive
            assert store.get_keyword_doc("browser", "Missing") is None
        finally:
            store.close()

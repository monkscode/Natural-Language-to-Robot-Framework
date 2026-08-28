"""
T9 — org-scoped learning reads and writes fail CLOSED when the org is unknown.

Before this change every org filter was written as::

    org_filter = " AND org_id = ?" if org_id is not None else ""

so an org-less caller had the predicate dropped entirely and read EVERY org's
rows.  Tenancy was advisory: it held only for callers who happened to carry an
org.  After this change a missing org matches no org-scoped row, and the
refusal is logged once so an empty result can be told apart from "there were
none".

Six reads are covered.  The plan named four; ``anti_pattern_engine``'s two were
miscounted as "already unconditional" (they are ``if/else`` blocks, not the
ternary the original scan matched) and feed the same agent prompt through
``SmartKeywordProvider``.

Two writes are covered as well.  Fail-closed reads would otherwise orphan every
org-less row forever, and the write paths still fire: ``kw_query_patterns``
carries NULL-org rows created seven weeks AFTER the one-shot org backfill
consumed its marker, so no backfill will ever reclaim them.

``AntiPatternEngine.learn``'s guard also closes a cross-org MUTATION, not just
a leak: ``_find_similar_anti_pattern`` failing open let an org-less run match
another org's row and reinforce it (score + evidence_count UPDATE).

Placement of the ``learn_from_feedback`` guard is load-bearing: it must run
before T5's claim INSERT.  A ``return`` after the claim would leave that INSERT
uncommitted on the long-lived writer connection, where the NEXT job's commit
makes it durable — a claim for a correction that was refused, which then gates
the user's next legitimate submission from that run.

Real Postgres via the in_memory_em fixture (never mocked — optimization rule).
"""

import logging
import uuid
from datetime import datetime, timezone

import psycopg
import pytest

from src.backend.crew_ai.optimization.anti_pattern_engine import AntiPatternEngine
from src.backend.crew_ai.optimization.execution_memory import ExecutionRecord
from src.backend.crew_ai.optimization.nl_feedback_engine import NLFeedbackEngine

pytestmark = pytest.mark.integration

_QUERY = "iterate over all rows in the table"
_URL = "https://a.test/list"

# Fixed 384-dim vector: the in_memory_em fixture disables the embedder, so
# monkeypatching _embed to a constant makes org_id the sole discriminator.
_FAKE_VEC = "[" + ",".join(["0.01"] * 384) + "]"


def _record(*, org_id, workflow_id=None, status="failed"):
    return ExecutionRecord(
        workflow_id=workflow_id or str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc),
        user_query=_QUERY, url=_URL, domain="a.test",
        robot_code="Get Text    css=td", code_structure="linear",
        test_status=status, failure_category="A1",
        error_message="only the first row was read", org_id=org_id,
    )


# ---------------------------------------------------------------------------
# seeds
# ---------------------------------------------------------------------------

def _seed_anti(em, org_id):
    """One anti-pattern above the injection gate (score >= 0.4, evidence >= 3)."""
    aid = em._writer_conn.execute(
        "INSERT INTO anti_patterns "
        "(failure_category, query_pattern, bad_code_snippet, error_message, "
        " domain, org_id, score, evidence_count, last_seen) "
        "VALUES ('A1', ?, 'Get Text', 'only first row', 'a.test', ?, 0.9, 5, "
        " datetime('now')) RETURNING id",
        (_QUERY, org_id),
    ).fetchone()["id"]
    em._writer_conn.commit()
    em.add_anchor("anti", aid, _QUERY, org_id=org_id)
    return aid


# ---------------------------------------------------------------------------
# READS — nl_feedback_engine
# ---------------------------------------------------------------------------

class TestHintReadsFailClosed:
    def test_hint_injection_returns_nothing_without_an_org(self, em_vec, seed_hint):
        seed_hint(text="wait for the spinner", domain="a.test",
                  anchor=_QUERY, org_id="org-A")
        engine = NLFeedbackEngine(execution_memory=em_vec)
        hints, ids = engine.get_hints_with_ids(_QUERY, _URL, "assembler", org_id=None)
        assert (hints, ids) == ([], []), "org-less caller read another org's hints"

    def test_hint_injection_still_serves_the_owning_org(self, em_vec, seed_hint):
        seed_hint(text="wait for the spinner", domain="a.test",
                  anchor=_QUERY, org_id="org-A")
        engine = NLFeedbackEngine(execution_memory=em_vec)
        _, ids = engine.get_hints_with_ids(_QUERY, _URL, "assembler", org_id="org-A")
        assert ids, "fail-closed must not break the org that owns the hint"

    def test_trigger_read_returns_nothing_without_an_org(self, em_vec, seed_hint):
        seed_hint(text="wait for the spinner", domain="a.test",
                  anchor=_QUERY, org_id="org-A")
        engine = NLFeedbackEngine(execution_memory=em_vec)
        assert engine.get_active_hints_raw(domain="a.test", url=_URL,
                                           org_id=None) == []

    def test_trigger_read_still_serves_the_owning_org(self, em_vec, seed_hint):
        seed_hint(text="wait for the spinner", domain="a.test",
                  anchor=_QUERY, org_id="org-A")
        engine = NLFeedbackEngine(execution_memory=em_vec)
        assert engine.get_active_hints_raw(domain="a.test", url=_URL,
                                           org_id="org-A")

    def test_the_refusal_is_logged(self, em_vec, seed_hint, caplog):
        seed_hint(text="wait for the spinner", domain="a.test",
                  anchor=_QUERY, org_id="org-A")
        engine = NLFeedbackEngine(execution_memory=em_vec)
        with caplog.at_level(logging.WARNING):
            engine.get_hints_with_ids(_QUERY, _URL, "assembler", org_id=None)
        assert any("refused (no org)" in r.message for r in caplog.records
                   if r.levelno >= logging.WARNING), (
            "a fail-closed read that logs nothing is indistinguishable from "
            "'there were no hints'; every tenancy refusal carries the same "
            "'refused (no org)' token so one Loki query finds them all")


# ---------------------------------------------------------------------------
# READS — anti_pattern_engine (the two the plan miscounted)
# ---------------------------------------------------------------------------

class TestAntiPatternReadsFailClosed:
    def test_warnings_return_nothing_without_an_org(self, em_vec):
        _seed_anti(em_vec, "org-A")
        engine = AntiPatternEngine(execution_memory=em_vec)
        assert engine.get_warnings(_QUERY, _URL, org_id=None) == [], (
            "org-less caller read another org's anti-patterns into the prompt")

    def test_warnings_still_serve_the_owning_org(self, em_vec):
        _seed_anti(em_vec, "org-A")
        engine = AntiPatternEngine(execution_memory=em_vec)
        assert engine.get_warnings(_QUERY, _URL, org_id="org-A")

    def test_the_dedup_lookup_matches_nothing_without_an_org(self, em_vec):
        """_find_similar_anti_pattern failing open let an org-less run REINFORCE
        another org's row — a cross-org mutation, not only a read leak.

        There is no branch here to test: the predicate is unconditional and
        `org_id = NULL` is never true in SQL, so this pins that the predicate
        stays unconditional. Re-adding `if org_id is not None` fails it.
        """
        _seed_anti(em_vec, "org-A")
        engine = AntiPatternEngine(execution_memory=em_vec)
        assert engine._find_similar_anti_pattern("A1", _QUERY, org_id=None) is None

    def test_the_dedup_lookup_still_matches_within_the_org(self, em_vec):
        aid = _seed_anti(em_vec, "org-A")
        engine = AntiPatternEngine(execution_memory=em_vec)
        found = engine._find_similar_anti_pattern("A1", _QUERY, org_id="org-A")
        assert found is not None and found["id"] == aid


# ---------------------------------------------------------------------------
# READS — keyword patterns and execution embeddings
# ---------------------------------------------------------------------------

class TestVectorReadsFailClosed:
    def test_find_similar_executions_returns_nothing_without_an_org(
            self, in_memory_em, monkeypatch):
        monkeypatch.setattr(in_memory_em, "_embed", lambda text: _FAKE_VEC)
        in_memory_em.store(_record(org_id="org-A", status="passed"))
        assert in_memory_em.find_similar_executions(_QUERY, org_id=None) == []

    def test_find_similar_executions_still_serves_the_owning_org(
            self, in_memory_em, monkeypatch):
        monkeypatch.setattr(in_memory_em, "_embed", lambda text: _FAKE_VEC)
        in_memory_em.store(_record(org_id="org-A", status="passed"))
        assert in_memory_em.find_similar_executions(_QUERY, org_id="org-A")


# ---------------------------------------------------------------------------
# The org must SURVIVE the round trip, or the write guard refuses everything
# ---------------------------------------------------------------------------

class TestTheOrgSurvivesTheReadBack:
    """The guard is only correct if the org survives `em.get`.

    `_row_to_record` dropped org_id (fixed separately, pinned by
    test_execution_record_org.py), and that is the exact record
    `process_user_feedback` hands to `learn_from_feedback` — so the guard would
    have refused EVERY production correction: failing closed always, not only
    when the org is unknown.
    """

    def test_a_stored_run_can_still_have_its_correction_learned(self, in_memory_db):
        """End to end over the round trip: store with an org, read back, learn."""
        rec = _record(org_id="org-A")
        in_memory_db._em.store(rec)
        engine = NLFeedbackEngine(execution_memory=in_memory_db)
        engine.learn_from_feedback(
            in_memory_db._em.get(rec.workflow_id),
            {"category": "keyword", "feedback_text": "wait for the spinner",
             "actor": "tester"})
        row = in_memory_db.execute(
            "SELECT org_id FROM nl_feedback_corrections").fetchone()
        assert row is not None and row["org_id"] == "org-A"


# ---------------------------------------------------------------------------
# WRITES — fail closed where the reads now fail closed
# ---------------------------------------------------------------------------

class TestHintWriteFailsClosed:
    def _hints(self, conn):
        return conn.execute(
            "SELECT id FROM nl_feedback_corrections").fetchall()

    def test_an_org_less_record_creates_no_hint(self, in_memory_db, caplog):
        engine = NLFeedbackEngine(execution_memory=in_memory_db)
        with caplog.at_level(logging.WARNING):
            engine.learn_from_feedback(
                _record(org_id=None),
                {"category": "keyword", "feedback_text": "wait for the spinner",
                 "actor": "tester"})
        assert self._hints(in_memory_db) == []
        assert any("refused (no org)" in r.message for r in caplog.records
                   if r.levelno >= logging.WARNING)

    def test_an_org_bearing_record_still_creates_its_hint(self, in_memory_db):
        engine = NLFeedbackEngine(execution_memory=in_memory_db)
        engine.learn_from_feedback(
            _record(org_id="org-A"),
            {"category": "keyword", "feedback_text": "wait for the spinner",
             "actor": "tester"})
        assert len(self._hints(in_memory_db)) == 1

    def test_the_refusal_leaves_no_claim_row(self, in_memory_db):
        """The guard must precede T5's claim INSERT.

        Placed after it, the refused submission leaves an uncommitted claim on
        the long-lived writer connection which the NEXT job's commit makes
        durable — gating the user's next legitimate correction from that run.
        """
        engine = NLFeedbackEngine(execution_memory=in_memory_db)
        wid = str(uuid.uuid4())
        engine.learn_from_feedback(
            _record(org_id=None, workflow_id=wid),
            {"category": "keyword", "feedback_text": "wait for the spinner",
             "actor": "tester"})
        # Force the writer connection to commit whatever it is holding, exactly
        # as the next queued job would.
        in_memory_db._writer_conn.commit()
        rows = in_memory_db.execute(
            "SELECT id FROM hint_evidence").fetchall()
        assert rows == [], "a refused correction left a claim behind"


class TestAntiPatternWriteFailsClosed:
    def _anti(self, conn):
        return conn.execute("SELECT id FROM anti_patterns").fetchall()

    def test_an_org_less_record_creates_no_anti_pattern(self, in_memory_db, caplog):
        engine = AntiPatternEngine(execution_memory=in_memory_db)
        with caplog.at_level(logging.WARNING):
            engine.learn(_record(org_id=None))
        assert self._anti(in_memory_db) == []
        assert any("refused (no org)" in r.message for r in caplog.records
                   if r.levelno >= logging.WARNING)

    def test_an_org_bearing_record_still_creates_its_anti_pattern(self, in_memory_db):
        engine = AntiPatternEngine(execution_memory=in_memory_db)
        engine.learn(_record(org_id="org-A"))
        assert len(self._anti(in_memory_db)) == 1


class TestPatternWriteFailsClosed:
    """add_pattern uses raw psycopg (%s placeholders) against its own pool."""

    @pytest.fixture(autouse=True)
    def _isolate_keyword_store(self):
        """Shadow the package autouse fixture by NAME so this class gets the
        real KeywordVectorStore, not the MagicMock the suite installs."""
        yield

    @pytest.fixture
    def kw_store(self):
        from src.backend.core.config import PG_CONNECT_TIMEOUT_S, settings
        from src.backend.crew_ai.optimization.keyword_vector_store import (
            KeywordVectorStore,
        )

        sep = "&" if "?" in settings.DATABASE_URL else "?"
        dsn = settings.DATABASE_URL + (
            f"{sep}options=-c%20search_path%3Dlearning_test,public")
        admin = psycopg.connect(settings.DATABASE_URL, autocommit=True,
                                connect_timeout=PG_CONNECT_TIMEOUT_S)
        try:
            admin.execute("CREATE SCHEMA IF NOT EXISTS learning_test")
        finally:
            admin.close()
        store = KeywordVectorStore(dsn=dsn)
        yield store
        cleanup = psycopg.connect(dsn, autocommit=True,
                                  connect_timeout=PG_CONNECT_TIMEOUT_S)
        try:
            cleanup.execute(
                "DELETE FROM kw_query_patterns WHERE org_id = 'org-A' "
                "OR user_query = %s", (_QUERY,))
        finally:
            cleanup.close()
        store.close()

    def _count(self, store, org_clause, params=()):
        with store._pool.connection() as conn:
            return conn.execute(
                f"SELECT count(*) FROM kw_query_patterns WHERE {org_clause}",
                params).fetchone()[0]

    def test_add_pattern_without_an_org_stores_nothing(self, kw_store, caplog):
        with caplog.at_level(logging.WARNING):
            assert kw_store.add_pattern(_QUERY, ["Get Text"], org_id=None) is None
        assert self._count(kw_store, "org_id IS NULL AND user_query = %s",
                           (_QUERY,)) == 0
        assert any("refused (no org)" in r.message for r in caplog.records
                   if r.levelno >= logging.WARNING)

    def test_add_pattern_with_an_org_still_stores(self, kw_store):
        assert kw_store.add_pattern(_QUERY, ["Get Text"], org_id="org-A")
        assert self._count(kw_store, "org_id = 'org-A'") == 1

    def test_search_patterns_returns_nothing_without_an_org(self, kw_store, caplog):
        kw_store.add_pattern(_QUERY, ["Get Text"], org_id="org-A")
        with caplog.at_level(logging.WARNING):
            assert kw_store.search_patterns(_QUERY, org_id=None) == []
        assert any("refused (no org)" in r.message for r in caplog.records
                   if r.levelno >= logging.WARNING)

    def test_search_patterns_still_serves_the_owning_org(self, kw_store):
        kw_store.add_pattern(_QUERY, ["Get Text"], org_id="org-A")
        assert kw_store.search_patterns(_QUERY, org_id="org-A")

"""kw_query_patterns: org attribution on write and org isolation on read.

Two coupled production fixes (must land together):
  Fix 1 — WRITE: feedback_loop passes record.org_id to learn_from_execution so
           patterns are stored with the caller's org rather than org_id=NULL.
  Fix 2 — READ:  smart_keyword_provider passes self._org_id to
           get_relevant_keywords so the pattern search is scoped to the
           provider's org rather than running unscoped.

Test A (write-wiring):   proves Fix 1 — FeedbackLoop submits record.org_id.
Test B (read-isolation): proves Fix 2 — org-B provider cannot retrieve
                         org-A's keyword patterns.
"""

import psycopg
import pytest
from unittest.mock import MagicMock

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Override the package autouse fixture so THIS module gets a real
# KeywordVectorStore rather than the suite-wide MagicMock.
# (Same pattern as test_kw_patterns_org.py.)
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolate_keyword_store():
    yield


# ---------------------------------------------------------------------------
# SynchronousWriteQueue — executes submit() calls immediately in the test
# thread (which conftest renames to WRITER_THREAD_NAME for every test).
# ---------------------------------------------------------------------------
class SynchronousWriteQueue:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


# ---------------------------------------------------------------------------
# Real KeywordVectorStore pointing at the isolated learning_test schema.
# (Same construction as test_kw_patterns_org.kw_store.)
# ---------------------------------------------------------------------------
@pytest.fixture
def kw_store():
    from src.backend.crew_ai.optimization.keyword_vector_store import KeywordVectorStore
    from src.backend.core.config import PG_CONNECT_TIMEOUT_S, settings

    sep = "&" if "?" in settings.DATABASE_URL else "?"
    dsn = settings.DATABASE_URL + f"{sep}options=-c%20search_path%3Dlearning_test,public"

    admin = psycopg.connect(
        settings.DATABASE_URL, autocommit=True,
        connect_timeout=PG_CONNECT_TIMEOUT_S,
    )
    try:
        admin.execute("CREATE SCHEMA IF NOT EXISTS learning_test")
    finally:
        admin.close()

    store = KeywordVectorStore(dsn=dsn)
    yield store
    store.close()


# ===========================================================================
# Test A — WRITE attribution
# ===========================================================================

def test_feedback_loop_passes_org_id_to_pattern_learner(in_memory_db):
    """FeedbackLoop.process_execution must submit record.org_id as the third
    positional argument to learn_from_execution.

    RED (pre-fix): write_queue.submit receives only (user_query, robot_code) —
    learn_from_execution.call_args.args has 2 elements, not 3.
    GREEN (post-fix): args = (user_query, robot_code, "org-A").
    """
    from src.backend.crew_ai.optimization.feedback_loop import (
        FeedbackLoop,
        LearningMetricsTracker,
        ContradictionDetector,
    )
    from src.backend.crew_ai.optimization.learning_config import LearningCircuitBreaker

    mock_pattern_learner = MagicMock()

    em = in_memory_db._em
    fl = FeedbackLoop(
        execution_memory=em,
        failure_analyzer=MagicMock(),
        structural_engine=MagicMock(),
        keyword_engine=MagicMock(),
        anti_pattern_engine=MagicMock(),
        pattern_learner=mock_pattern_learner,
        metrics_tracker=LearningMetricsTracker(in_memory_db),
        contradiction_detector=ContradictionDetector(in_memory_db),
        write_queue=SynchronousWriteQueue(),
        circuit_breaker=LearningCircuitBreaker(),
    )

    robot_code = "*** Test Cases ***\nLogin Test\n    Click    id=submit-btn\n"
    fl.process_execution(
        workflow_id="wf-kw-write-attr",
        user_query="login as admin",
        url="https://test.example.com/login",
        robot_code=robot_code,
        test_status="passed",
        org_id="org-A",
    )

    mock_pattern_learner.learn_from_execution.assert_called_once()
    called_args = mock_pattern_learner.learn_from_execution.call_args.args
    assert len(called_args) >= 3, (
        f"Expected 3 positional args (user_query, robot_code, org_id), "
        f"got {len(called_args)}: {called_args!r} — org_id was not passed to "
        f"learn_from_execution"
    )
    assert called_args[2] == "org-A", (
        f"Expected org_id='org-A' as 3rd positional arg, got {called_args[2]!r}"
    )


# ===========================================================================
# Test B — READ isolation
# ===========================================================================

def _stub_library_context():
    ctx = MagicMock()
    ctx.library_name = "SeleniumLibrary"
    ctx.core_rules = "# Core rules stub"
    ctx.planning_context = "# Planning context stub"
    ctx.code_assembly_context = "# Assembly context stub"
    return ctx


def test_provider_does_not_surface_other_org_keyword_patterns(kw_store):
    """SmartKeywordProvider built for org-B must not receive org-A's patterns.

    org-A's 'OrgASeleniumKw' is seeded against the exact query "login as admin"
    so it WOULD be retrieved by an unscoped search.  The mock vector_store
    echoes any predicted keyword back by name, so if 'OrgASeleniumKw' leaks
    into predicted_keywords it will appear in bundle.context.

    RED (pre-fix): line 727 calls get_relevant_keywords(user_query) with no
    org_id → unscoped search returns org-A pattern → 'OrgASeleniumKw' in
    bundle.context.
    GREEN (post-fix): get_relevant_keywords(user_query, org_id='org-B') →
    scoped search returns [] (no org-B patterns) → 'OrgASeleniumKw' absent.
    """
    from src.backend.crew_ai.optimization.pattern_learning import QueryPatternMatcher
    from src.backend.crew_ai.optimization.smart_keyword_provider import SmartKeywordProvider

    # Seed an org-A pattern whose query matches the provider query exactly.
    kw_store.add_pattern("login as admin", ["OrgASeleniumKw"], org_id="org-A")

    # The vector_store mock echoes each queried name back so that predicted
    # keywords surface in the formatted context (avoiding false-green from
    # empty keyword docs).
    vs = MagicMock()
    vs.search.side_effect = lambda library_name, query, top_k: [
        {
            "name": query,
            "args": [],
            "description": "stub-doc",
            "distance": 0.1,
            "similarity": 0.9,
        }
    ]

    provider = SmartKeywordProvider(
        library_context=_stub_library_context(),
        pattern_matcher=QueryPatternMatcher(kw_store),
        vector_store=vs,
        execution_memory=None,  # skip hint engines; test targets Tier-2 only
        org_id="org-B",
    )

    bundle = provider.get_agent_context("login as admin", "assembler")
    combined = bundle.hint_text + bundle.context

    assert "OrgASeleniumKw" not in combined, (
        f"org-B provider leaked org-A keyword pattern. "
        f"context={bundle.context!r}"
    )

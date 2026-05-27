"""
DAY_04 Tests -- Anti-Pattern Engine.

Pytest conversion of scripts/verify_day04.py.
Uses in-memory SQLite database -- does NOT modify real data.
"""

import contextlib
import sqlite3

from src.backend.crew_ai.optimization.learning_config import (
    LearningEngine,
    EffectivenessScore,
)
from src.backend.crew_ai.optimization.anti_pattern_engine import AntiPatternEngine


# ---------------------------------------------------------------------------
# Test Helpers
# ---------------------------------------------------------------------------

class MockRecord:
    """Minimal mock of ExecutionRecord for testing."""

    def __init__(self, user_query="test query", robot_code=None,
                 test_status="failed", failure_category=None,
                 failed_keyword=None, error_message=None,
                 domain=None):
        self.user_query = user_query
        self.robot_code = robot_code
        self.test_status = test_status
        self.failure_category = failure_category
        self.failed_keyword = failed_keyword
        self.error_message = error_message
        self.domain = domain


class _EmCompat:
    """Minimal ExecutionMemory stand-in for AntiPatternEngine unit tests.

    Exposes ._writer_conn and .read_conn() so the engine's thread-checked
    write paths and read_conn() context manager both work. Also proxies
    .execute() and .commit() so helper functions like _seed_anti_pattern
    can use the returned object directly.
    """

    def __init__(self, writer_conn):
        self._writer_conn = writer_conn

    def execute(self, *args, **kwargs):
        return self._writer_conn.execute(*args, **kwargs)

    def commit(self):
        return self._writer_conn.commit()

    def rollback(self):
        return self._writer_conn.rollback()

    def close(self):
        return self._writer_conn.close()

    @contextlib.contextmanager
    def read_conn(self):
        yield self._writer_conn

    def filter_by_query_similarity(self, user_query, candidate_ids,
                                   kind, threshold=0.55):
        # This unit-test shim has no ChromaDB — mirror the real
        # ExecutionMemory's documented "ChromaDB unavailable" degradation:
        # fail open (score/evidence gating only, no semantic narrowing).
        return set(candidate_ids)

    def add_anchor(self, *args, **kwargs):
        # No ChromaDB in this shim — anchor embedding is a no-op.
        pass


def _create_test_db() -> "_EmCompat":
    """Create in-memory SQLite DB with anti_patterns schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE anti_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            failure_category TEXT NOT NULL,
            query_pattern TEXT,
            bad_code_snippet TEXT,
            error_message TEXT,
            correct_alternative TEXT,
            domain TEXT,
            score REAL DEFAULT 0.5,
            evidence_count INTEGER DEFAULT 1,
            last_seen TEXT
        )
    """)
    conn.commit()
    return _EmCompat(conn)


def _seed_anti_pattern(conn, **overrides):
    """Insert a test anti-pattern with sensible defaults."""
    defaults = {
        "failure_category": "A1",
        "query_pattern": "verify all rows in the table",
        "bad_code_snippet": "    Get Text    css=table tr",
        "error_message": "Keyword 'Get Text' expected 1 element but found 5",
        "correct_alternative": None,
        "domain": None,
        "score": 0.5,
        "evidence_count": 3,
        "last_seen": "2026-02-19 12:00:00",
    }
    defaults.update(overrides)
    conn.execute("""
        INSERT INTO anti_patterns
        (failure_category, query_pattern, bad_code_snippet, error_message,
         correct_alternative, domain, score, evidence_count, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        defaults["failure_category"], defaults["query_pattern"],
        defaults["bad_code_snippet"], defaults["error_message"],
        defaults["correct_alternative"], defaults["domain"],
        defaults["score"], defaults["evidence_count"],
        defaults["last_seen"],
    ))
    conn.commit()


# ---------------------------------------------------------------------------
# Tests: ABC Implementation
# ---------------------------------------------------------------------------

class TestABCImplementation:
    """Verify AntiPatternEngine implements LearningEngine ABC."""

    def test_is_subclass_of_learning_engine(self):
        assert issubclass(AntiPatternEngine, LearningEngine), \
            "AntiPatternEngine is subclass of LearningEngine"

    def test_has_required_methods(self):
        required_methods = ["learn", "get_hints", "get_stats"]
        for method_name in required_methods:
            assert hasattr(AntiPatternEngine, method_name) \
                and callable(getattr(AntiPatternEngine, method_name)), \
                f"AntiPatternEngine has '{method_name}' method"


# ---------------------------------------------------------------------------
# Tests: learn()
# ---------------------------------------------------------------------------

class TestLearn:
    """Tests for AntiPatternEngine.learn() method."""

    def test_creates_anti_pattern_for_each_failure_category(self):
        """Verify learn() creates anti-patterns for each failure category."""
        categories = [
            ("A1", "Missing FOR loop for multi-element operation"),
            ("B1", "No keyword with name 'Input Text' found"),
            ("C1", "Element with locator 'id=submit' not found"),
            ("D1", "TimeoutError: waiting for element"),
            ("E1", "Values are not equal: expected 'Active' got 'Inactive'"),
        ]

        for category, error_msg in categories:
            conn = _create_test_db()
            engine = AntiPatternEngine(conn)

            record = MockRecord(
                user_query=f"test query for {category}",
                robot_code="    Click    id=submit",
                test_status="failed",
                failure_category=category,
                error_message=error_msg,
                domain="demoqa.com",
            )
            engine.learn(record)

            count = conn.execute(
                "SELECT COUNT(*) FROM anti_patterns"
            ).fetchone()[0]
            assert count == 1, \
                f"learn() creates anti-pattern for category {category}: count={count}"

            row = conn.execute("SELECT * FROM anti_patterns").fetchone()
            assert row["failure_category"] == category, \
                f"learn() stores correct failure_category {category}"
            conn.close()

    def test_ignores_passed_tests(self):
        """Verify learn() skips passed tests."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)
        record = MockRecord(
            test_status="passed",
            user_query="click submit button",
            failure_category="A1",
            error_message="some error",
        )
        engine.learn(record)
        count = conn.execute(
            "SELECT COUNT(*) FROM anti_patterns"
        ).fetchone()[0]
        assert count == 0, f"learn() skips passed tests: count={count}"
        conn.close()

    def test_ignores_unknown_category(self):
        """Verify learn() skips unknown category."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)
        record = MockRecord(
            test_status="failed",
            failure_category="unknown",
            error_message="some error",
        )
        engine.learn(record)
        count = conn.execute(
            "SELECT COUNT(*) FROM anti_patterns"
        ).fetchone()[0]
        assert count == 0, "learn() skips unknown category"
        conn.close()

    def test_ignores_none_category(self):
        """Verify learn() skips None category."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)
        record = MockRecord(
            test_status="failed",
            failure_category=None,
            error_message="some error",
        )
        engine.learn(record)
        count = conn.execute(
            "SELECT COUNT(*) FROM anti_patterns"
        ).fetchone()[0]
        assert count == 0, "learn() skips None category"
        conn.close()

    def test_ignores_missing_error_message(self):
        """Verify learn() skips missing error_message."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)
        record = MockRecord(
            test_status="failed",
            failure_category="A1",
            error_message=None,
        )
        engine.learn(record)
        count = conn.execute(
            "SELECT COUNT(*) FROM anti_patterns"
        ).fetchone()[0]
        assert count == 0, "learn() skips missing error_message"
        conn.close()

    def test_reinforces_existing_no_new_row(self):
        """Verify learn() reinforces existing anti-patterns when similar query."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        _seed_anti_pattern(conn,
            failure_category="A1",
            query_pattern="verify all rows in the table",
            evidence_count=1,
            score=EffectivenessScore.calculate(1, 0),
        )

        record = MockRecord(
            user_query="verify all rows in the grid",
            test_status="failed",
            failure_category="A1",
            error_message="Missing FOR loop",
            robot_code="    Get Text    css=table tr",
        )
        engine.learn(record)

        count = conn.execute(
            "SELECT COUNT(*) FROM anti_patterns"
        ).fetchone()[0]
        assert count == 1, \
            f"learn() reinforces existing (no new row): count={count}"

        row = conn.execute("SELECT * FROM anti_patterns").fetchone()
        assert row["evidence_count"] == 2, \
            f"learn() increments evidence_count: evidence={row['evidence_count']}"

        expected_score = EffectivenessScore.calculate(2, 0)
        assert abs(row["score"] - expected_score) < 0.001, \
            f"learn() recalculates score from raw counts: score={row['score']}, expected={expected_score}"

        conn.close()

    def test_creates_new_for_different_category(self):
        """Verify learn() creates new anti-pattern for different category even with same query."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        _seed_anti_pattern(conn,
            failure_category="A1",
            query_pattern="verify all rows in the table",
            evidence_count=1,
            score=EffectivenessScore.calculate(1, 0),
        )

        record2 = MockRecord(
            user_query="verify all rows in the table",
            test_status="failed",
            failure_category="B1",
            error_message="Different error",
            robot_code="    Input Text    id=name    value",
        )
        engine.learn(record2)

        count_after = conn.execute(
            "SELECT COUNT(*) FROM anti_patterns"
        ).fetchone()[0]
        assert count_after == 2, \
            f"learn() creates new for different category: count={count_after}"

        conn.close()

    def test_correct_alternative_filled_by_passing(self):
        """Verify passing executions can fill in correct_alternative."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        _seed_anti_pattern(conn,
            failure_category="A1",
            query_pattern="verify all rows in the table",
            evidence_count=3,
            score=EffectivenessScore.calculate(3, 0),
            correct_alternative=None,
        )

        record = MockRecord(
            user_query="verify all rows from the table",
            test_status="passed",
            robot_code="*** Test Cases ***\nVerify Rows\n    FOR    ${row}    IN    @{rows}\n        Should Contain    ${row}    Active\n    END",
        )
        engine.learn(record)

        row = conn.execute("SELECT * FROM anti_patterns").fetchone()
        assert row["correct_alternative"] is not None, \
            f"Passing execution fills correct_alternative: alt={'set' if row['correct_alternative'] else 'None'}"

        conn.close()

    def test_already_resolved_not_overwritten(self):
        """Verify already resolved anti-pattern is NOT overwritten."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)
        _seed_anti_pattern(conn,
            failure_category="A1",
            query_pattern="verify all rows in the table",
            evidence_count=3,
            score=EffectivenessScore.calculate(3, 0),
            correct_alternative="already set code",
        )

        record = MockRecord(
            user_query="verify all rows from the table",
            test_status="passed",
            robot_code="new passing code",
        )
        engine.learn(record)

        row = conn.execute("SELECT * FROM anti_patterns").fetchone()
        assert row["correct_alternative"] == "already set code", \
            "Already-resolved anti-pattern not overwritten"

        conn.close()

    def test_below_threshold_not_resolved_by_passing(self):
        """Verify below threshold anti-pattern is NOT resolved by passing."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)
        _seed_anti_pattern(conn,
            failure_category="A1",
            query_pattern="verify all rows in the table",
            evidence_count=1,
            score=EffectivenessScore.calculate(1, 0),
            correct_alternative=None,
        )

        record = MockRecord(
            user_query="verify all rows from the table",
            test_status="passed",
            robot_code="some code",
        )
        engine.learn(record)

        row = conn.execute("SELECT * FROM anti_patterns").fetchone()
        assert row["correct_alternative"] is None, \
            "Below-threshold anti-pattern NOT resolved by passing"

        conn.close()

    def test_correct_alternative_truncated_at_500_chars(self):
        """Verify correct_alternative is truncated at 500 chars."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)
        _seed_anti_pattern(conn,
            failure_category="A1",
            query_pattern="verify all rows in the table",
            evidence_count=3,
            score=EffectivenessScore.calculate(3, 0),
            correct_alternative=None,
        )

        long_code = "x" * 700
        record = MockRecord(
            user_query="verify all rows from the table",
            test_status="passed",
            robot_code=long_code,
        )
        engine.learn(record)

        row = conn.execute("SELECT * FROM anti_patterns").fetchone()
        assert row["correct_alternative"] is not None \
            and len(row["correct_alternative"]) == 500, \
            f"correct_alternative truncated at 500 chars: len={len(row['correct_alternative']) if row['correct_alternative'] else 0}"

        conn.close()

    def test_passing_with_no_robot_code_does_not_resolve(self):
        """Verify passing with no robot_code does not resolve."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)
        _seed_anti_pattern(conn,
            failure_category="A1",
            query_pattern="verify all rows in the table",
            evidence_count=3,
            score=EffectivenessScore.calculate(3, 0),
            correct_alternative=None,
        )

        record = MockRecord(
            user_query="verify all rows from the table",
            test_status="passed",
            robot_code=None,
        )
        engine.learn(record)

        row = conn.execute("SELECT * FROM anti_patterns").fetchone()
        assert row["correct_alternative"] is None, \
            "Passing with no robot_code does not resolve"

        conn.close()

    def test_score_consistency(self):
        """Verify scores are always recalculated from raw counts, never incremental."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        record = MockRecord(
            user_query="verify all rows in the table data",
            test_status="failed",
            failure_category="A1",
            error_message="Missing FOR loop",
            robot_code="    Get Text    css=tr",
        )
        engine.learn(record)

        row = conn.execute("SELECT * FROM anti_patterns").fetchone()
        expected_initial = EffectivenessScore.calculate(1, 0)
        assert abs(row["score"] - expected_initial) < 0.001, \
            f"Initial score matches EffectivenessScore.calculate(1, 0): score={row['score']}, expected={expected_initial}"

        record2 = MockRecord(
            user_query="verify all rows in the table columns",
            test_status="failed",
            failure_category="A1",
            error_message="Missing FOR loop again",
            robot_code="    Get Text    css=td",
        )
        engine.learn(record2)

        row = conn.execute("SELECT * FROM anti_patterns").fetchone()
        expected_after2 = EffectivenessScore.calculate(2, 0)
        assert abs(row["score"] - expected_after2) < 0.001, \
            f"Score after 2nd evidence matches EffectivenessScore.calculate(2, 0): score={row['score']}, expected={expected_after2}"

        record3 = MockRecord(
            user_query="verify all rows in the table headers",
            test_status="failed",
            failure_category="A1",
            error_message="Still missing FOR loop",
            robot_code="    Get Text    css=th",
        )
        engine.learn(record3)

        row = conn.execute("SELECT * FROM anti_patterns").fetchone()
        expected_after3 = EffectivenessScore.calculate(3, 0)
        assert abs(row["score"] - expected_after3) < 0.001, \
            f"Score after 3rd evidence matches EffectivenessScore.calculate(3, 0): score={row['score']}, expected={expected_after3}"

        conn.close()

    def test_no_counter_evidence(self):
        """Verify anti-pattern scores only increase, never decrease."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        _seed_anti_pattern(conn,
            failure_category="A1",
            query_pattern="verify all rows in the table",
            evidence_count=5,
            score=EffectivenessScore.calculate(5, 0),
        )

        initial_score = conn.execute(
            "SELECT score FROM anti_patterns"
        ).fetchone()[0]

        record = MockRecord(
            user_query="verify all rows from the table",
            test_status="passed",
            robot_code="correct code here",
        )
        engine.learn(record)

        after_score = conn.execute(
            "SELECT score FROM anti_patterns"
        ).fetchone()[0]

        assert after_score >= initial_score, \
            f"Passing execution does not decrease score: before={initial_score}, after={after_score}"

        evidence = conn.execute(
            "SELECT evidence_count FROM anti_patterns"
        ).fetchone()[0]
        assert evidence == 5, \
            f"Evidence count unchanged by passing execution: evidence={evidence}"

        conn.close()


# ---------------------------------------------------------------------------
# Tests: get_hints()
# ---------------------------------------------------------------------------

class TestGetHints:
    """Tests for AntiPatternEngine.get_hints() method."""

    def test_role_filtering_planner(self):
        """Verify get_hints() returns hints for planner."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)
        _seed_anti_pattern(conn,
            query_pattern="verify all rows in the table",
            evidence_count=5,
            score=0.7,
        )

        hints = engine.get_hints("verify all rows in the table",
                                 "https://demoqa.com", "planner")
        assert hints is not None and len(hints) > 0, \
            f"get_hints() returns hints for planner: count={len(hints) if hints else 0}"

        conn.close()

    def test_role_filtering_assembler(self):
        """Verify get_hints() returns hints for assembler."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)
        _seed_anti_pattern(conn,
            query_pattern="verify all rows in the table",
            evidence_count=5,
            score=0.7,
        )

        hints = engine.get_hints("verify all rows in the table",
                                 "https://demoqa.com", "assembler")
        assert hints is not None and len(hints) > 0, \
            "get_hints() returns hints for assembler"

        conn.close()

    def test_role_filtering_validator_returns_none(self):
        """Verify get_hints() returns None for validator."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)
        _seed_anti_pattern(conn,
            query_pattern="verify all rows in the table",
            evidence_count=5,
            score=0.7,
        )

        hints = engine.get_hints("verify all rows in the table",
                                 "https://demoqa.com", "validator")
        assert hints is None, \
            "get_hints() returns None for validator"

        conn.close()

    def test_role_filtering_identifier_returns_none(self):
        """Verify get_hints() returns None for identifier."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)
        _seed_anti_pattern(conn,
            query_pattern="verify all rows in the table",
            evidence_count=5,
            score=0.7,
        )

        hints = engine.get_hints("verify all rows in the table",
                                 "https://demoqa.com", "identifier")
        assert hints is None, \
            "get_hints() returns None for identifier"

        conn.close()

    def test_content_quality_planner_contains_category(self):
        """Verify planner hint contains failure_category."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        _seed_anti_pattern(conn,
            failure_category="A1",
            query_pattern="verify all rows in the table",
            bad_code_snippet="    Get Text    css=table tr",
            error_message="Expected 1 element but found 5",
            correct_alternative=None,
            evidence_count=5,
            score=0.7,
        )

        hints = engine.get_hints("verify all rows in the table",
                                 "https://demoqa.com", "planner")
        assert hints is not None and any("A1" in h for h in hints), \
            "Planner hint contains failure_category"

        conn.close()

    def test_content_quality_assembler_without_alternative(self):
        """Verify assembler hint without correct_alternative contains error info."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        _seed_anti_pattern(conn,
            failure_category="A1",
            query_pattern="verify all rows in the table",
            bad_code_snippet="    Get Text    css=table tr",
            error_message="Expected 1 element but found 5",
            correct_alternative=None,
            evidence_count=5,
            score=0.7,
        )

        hints = engine.get_hints("verify all rows in the table",
                                 "https://demoqa.com", "assembler")
        assert hints is not None and any("A1" in h for h in hints), \
            "Assembler hint (no alt) contains error message"

        conn.close()

    def test_content_quality_assembler_with_alternative(self):
        """Verify assembler hint with correct_alternative shows 'Instead use'."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        _seed_anti_pattern(conn,
            failure_category="A1",
            query_pattern="verify all rows in the table",
            bad_code_snippet="    Get Text    css=table tr",
            error_message="Expected 1 element but found 5",
            correct_alternative=None,
            evidence_count=5,
            score=0.7,
        )

        conn.execute(
            "UPDATE anti_patterns SET correct_alternative = ? WHERE id = 1",
            ("    FOR    ${row}    IN    @{rows}\n        Get Text    ${row}",)
        )
        conn.commit()

        hints = engine.get_hints("verify all rows in the table",
                                 "https://demoqa.com", "assembler")
        assert hints is not None and any("Instead use" in h for h in hints), \
            "Assembler hint WITH correct_alternative shows 'Instead use'"

        conn.close()

    def test_threshold_below_evidence(self):
        """Verify below evidence threshold returns no hints."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        _seed_anti_pattern(conn,
            query_pattern="verify all rows in the table",
            evidence_count=2,
            score=0.7,
        )

        hints = engine.get_hints("verify all rows in the table",
                                 "https://demoqa.com", "planner")
        assert hints is None, \
            "Below evidence threshold: no hints returned"

        conn.close()

    def test_threshold_below_score(self):
        """Verify below score threshold returns no hints."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        _seed_anti_pattern(conn,
            query_pattern="verify all rows in the table",
            evidence_count=5,
            score=0.3,
        )

        hints = engine.get_hints("verify all rows in the table",
                                 "https://demoqa.com", "planner")
        assert hints is None, \
            "Below score threshold: no hints returned"

        conn.close()

    def test_threshold_exactly_at(self):
        """Verify exactly at threshold returns hints."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        _seed_anti_pattern(conn,
            query_pattern="verify all rows in the table",
            evidence_count=3,
            score=0.4,
        )

        hints = engine.get_hints("verify all rows in the table",
                                 "https://demoqa.com", "planner")
        assert hints is not None and len(hints) > 0, \
            "Exactly at threshold: hints returned"

        conn.close()

    def test_threshold_above(self):
        """Verify above threshold returns hints."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        _seed_anti_pattern(conn,
            query_pattern="verify all rows in the table",
            evidence_count=10,
            score=0.9,
        )

        hints = engine.get_hints("verify all rows in the table",
                                 "https://demoqa.com", "planner")
        assert hints is not None and len(hints) > 0, \
            "Above threshold: hints returned"

        conn.close()

    def test_domain_matching(self):
        """Verify matching domain returns hints."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        _seed_anti_pattern(conn,
            query_pattern="verify all rows in the table",
            domain="demoqa.com",
            evidence_count=5,
            score=0.7,
        )

        hints = engine.get_hints("verify all rows in the table",
                                 "https://demoqa.com/test", "planner")
        assert hints is not None and len(hints) > 0, \
            "Domain match: hints returned"

        conn.close()

    def test_domain_wrong(self):
        """Verify wrong domain returns no hints."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        _seed_anti_pattern(conn,
            query_pattern="verify all rows in the table",
            domain="demoqa.com",
            evidence_count=5,
            score=0.7,
        )

        hints = engine.get_hints("verify all rows in the table",
                                 "https://example.com/test", "planner")
        assert hints is None, \
            "Wrong domain: no hints returned"

        conn.close()

    def test_domain_null_matches_any(self):
        """Verify NULL domain anti-pattern matches any domain query."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        _seed_anti_pattern(conn,
            query_pattern="verify all rows in the table",
            domain=None,
            evidence_count=5,
            score=0.7,
        )

        hints = engine.get_hints("verify all rows in the table",
                                 "https://any-site.com", "planner")
        assert hints is not None and len(hints) > 0, \
            "NULL domain anti-pattern matches any domain"

        conn.close()

    def test_domain_null_matches_no_url(self):
        """Verify NULL domain anti-pattern still matches when no URL provided."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        _seed_anti_pattern(conn,
            query_pattern="verify all rows in the table",
            domain=None,
            evidence_count=5,
            score=0.7,
        )

        hints = engine.get_hints("verify all rows in the table",
                                 None, "planner")
        assert hints is not None and len(hints) > 0, \
            "No URL: NULL domain anti-pattern still matches"

        conn.close()


# ---------------------------------------------------------------------------
# Tests: get_stats()
# ---------------------------------------------------------------------------

class TestGetStats:
    """Tests for AntiPatternEngine.get_stats() method."""

    def test_empty_db(self):
        """Verify get_stats() on empty DB."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)
        stats = engine.get_stats()
        assert stats["total_anti_patterns"] == 0, \
            "get_stats() empty DB: total=0"
        assert stats["active_anti_patterns"] == 0, \
            "get_stats() empty DB: active=0"
        assert stats["engine"] == "anti_pattern", \
            "get_stats() has engine name"
        conn.close()

    def test_mixed_active_inactive(self):
        """Verify get_stats() with mixed active/inactive anti-patterns."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)
        # Active: score >= 0.4 AND evidence >= 3
        _seed_anti_pattern(conn, failure_category="A1", evidence_count=5, score=0.7)
        _seed_anti_pattern(conn, failure_category="B1", evidence_count=3, score=0.5)
        # Inactive: below thresholds
        _seed_anti_pattern(conn, failure_category="C1", evidence_count=1, score=0.3)

        stats = engine.get_stats()
        assert stats["total_anti_patterns"] == 3, \
            "get_stats() total=3"
        assert stats["active_anti_patterns"] == 2, \
            f"get_stats() active=2 (above thresholds): active={stats['active_anti_patterns']}"
        assert stats["by_category"].get("A1") == 1 \
            and stats["by_category"].get("B1") == 1 \
            and stats["by_category"].get("C1") == 1, \
            f"get_stats() category breakdown correct: categories={stats['by_category']}"

        conn.close()


# ---------------------------------------------------------------------------
# Tests: Internal Methods
# ---------------------------------------------------------------------------

class TestInternals:
    """Tests for AntiPatternEngine internal methods."""

    def test_find_similar_exact_overlap(self):
        """Verify exact overlap matches."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        _seed_anti_pattern(conn,
            failure_category="A1",
            query_pattern="verify all rows in the table",
        )

        result = engine._find_similar_anti_pattern("A1", "verify all rows in the table")
        assert result is not None, \
            "Exact overlap: matches"

        conn.close()

    def test_find_similar_four_word_overlap(self):
        """Verify 4-word overlap matches (>= 3)."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        _seed_anti_pattern(conn,
            failure_category="A1",
            query_pattern="verify all rows in the table",
        )

        result = engine._find_similar_anti_pattern("A1", "verify all rows from the grid")
        assert result is not None, \
            "4-word overlap: matches"

        conn.close()

    def test_find_similar_two_word_overlap_no_match(self):
        """Verify 2-word overlap does NOT match (< 3)."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        _seed_anti_pattern(conn,
            failure_category="A1",
            query_pattern="verify all rows in the table",
        )

        result = engine._find_similar_anti_pattern("A1", "check the buttons")
        assert result is None, \
            "2-word overlap: no match"

        conn.close()

    def test_find_similar_wrong_category_no_match(self):
        """Verify wrong category does NOT match despite word overlap."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        _seed_anti_pattern(conn,
            failure_category="A1",
            query_pattern="verify all rows in the table",
        )

        result = engine._find_similar_anti_pattern("B1", "verify all rows in the table")
        assert result is None, \
            "Wrong category: no match despite word overlap"

        conn.close()

    def test_find_matching_returns_score_gated_rows(self):
        """_find_matching_anti_patterns returns score/evidence-gated rows.
        Query-relevance narrowing is the query-similarity filter's job
        (tested against real ChromaDB in test_query_similarity.py); with no
        ChromaDB the filter fails open, so every gated row is returned."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        _seed_anti_pattern(conn,
            failure_category="A1",
            query_pattern="verify all rows in the table",
            evidence_count=5,
            score=0.8,
        )
        _seed_anti_pattern(conn,
            failure_category="B1",
            query_pattern="click the submit button",
            evidence_count=5,
            score=0.6,
        )

        result = engine._find_matching_anti_patterns("verify the table")
        assert {r["failure_category"] for r in result} == {"A1", "B1"}

        conn.close()

    def test_find_matching_sorted_by_score_desc(self):
        """Verify results sorted by score DESC."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        _seed_anti_pattern(conn,
            failure_category="A1",
            query_pattern="verify all rows in the table",
            evidence_count=5,
            score=0.8,
        )
        _seed_anti_pattern(conn,
            failure_category="C1",
            query_pattern="verify all rows in the table",
            evidence_count=5,
            score=0.9,
        )

        result = engine._find_matching_anti_patterns("verify all rows in the table")
        assert len(result) >= 2 and result[0]["score"] >= result[1]["score"], \
            f"Results sorted by score DESC: scores={[r['score'] for r in result]}"

        conn.close()

    def test_find_matching_empty_query_returns_empty(self):
        """C5: an empty user_query short-circuits to [] — it carries no
        relevance signal and cannot be embedded. (An unrelated non-empty
        query being dropped is semantic behaviour, tested with real ChromaDB
        in test_query_similarity.py.)"""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        _seed_anti_pattern(conn,
            failure_category="A1",
            query_pattern="verify all rows in the table",
            evidence_count=5,
            score=0.8,
        )

        assert engine._find_matching_anti_patterns("") == []
        assert engine._find_matching_anti_patterns("   ") == []

        conn.close()

    def test_extract_relevant_code_finds_keyword(self):
        """Verify _extract_relevant_code finds failed_keyword."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        robot_code = (
            "*** Test Cases ***\n"
            "Verify Table\n"
            "    ${rows}=    Get Elements    css=table tr\n"
            "    FOR    ${row}    IN    @{rows}\n"
            "        Get Text    ${row}\n"
            "        Should Contain    ${text}    Active\n"
            "    END\n"
        )
        snippet = engine._extract_relevant_code(robot_code, "Get Text")
        assert "Get Text" in snippet, \
            "extract_relevant_code: finds failed_keyword"

        conn.close()

    def test_extract_relevant_code_includes_context(self):
        """Verify _extract_relevant_code includes context lines."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        robot_code = (
            "*** Test Cases ***\n"
            "Verify Table\n"
            "    ${rows}=    Get Elements    css=table tr\n"
            "    FOR    ${row}    IN    @{rows}\n"
            "        Get Text    ${row}\n"
            "        Should Contain    ${text}    Active\n"
            "    END\n"
        )
        snippet = engine._extract_relevant_code(robot_code, "Get Text")
        assert "FOR" in snippet or "Get Elements" in snippet, \
            "extract_relevant_code: includes context lines"

        conn.close()

    def test_extract_relevant_code_capped_at_5_lines(self):
        """Verify _extract_relevant_code does not exceed 5 meaningful lines."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        robot_code = (
            "*** Test Cases ***\n"
            "Verify Table\n"
            "    ${rows}=    Get Elements    css=table tr\n"
            "    FOR    ${row}    IN    @{rows}\n"
            "        Get Text    ${row}\n"
            "        Should Contain    ${text}    Active\n"
            "    END\n"
        )
        snippet = engine._extract_relevant_code(robot_code, "Get Text")
        non_empty_lines = [l for l in snippet.split("\n") if l.strip()]
        assert len(non_empty_lines) <= 5, \
            f"extract_relevant_code: capped at 5 lines: lines={len(non_empty_lines)}"

        conn.close()

    def test_extract_relevant_code_fallback_without_keyword(self):
        """Verify _extract_relevant_code fallback without keyword."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        robot_code = (
            "*** Test Cases ***\n"
            "Verify Table\n"
            "    ${rows}=    Get Elements    css=table tr\n"
            "    FOR    ${row}    IN    @{rows}\n"
            "        Get Text    ${row}\n"
            "        Should Contain    ${text}    Active\n"
            "    END\n"
        )
        snippet = engine._extract_relevant_code(robot_code, None)
        assert len(snippet) > 0 and "*** Test Cases ***" not in snippet, \
            "extract_relevant_code: fallback without keyword"

        conn.close()

    def test_extract_relevant_code_none_input(self):
        """Verify _extract_relevant_code returns empty for None input."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        snippet = engine._extract_relevant_code(None, "Get Text")
        assert snippet == "", \
            "extract_relevant_code: None input returns empty"

        conn.close()

    def test_extract_relevant_code_empty_string(self):
        """Verify _extract_relevant_code returns empty for empty string."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        snippet = engine._extract_relevant_code("", "Get Text")
        assert snippet == "", \
            "extract_relevant_code: empty string returns empty"

        conn.close()

    def test_extract_relevant_code_keyword_not_found_fallback(self):
        """Verify _extract_relevant_code uses fallback when keyword not found."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        robot_code = (
            "*** Test Cases ***\n"
            "Verify Table\n"
            "    ${rows}=    Get Elements    css=table tr\n"
            "    FOR    ${row}    IN    @{rows}\n"
            "        Get Text    ${row}\n"
            "        Should Contain    ${text}    Active\n"
            "    END\n"
        )
        snippet = engine._extract_relevant_code(robot_code, "NonExistentKeyword")
        assert len(snippet) > 0, \
            "extract_relevant_code: keyword not found uses fallback: Should use first-5-lines fallback"

        conn.close()

    def test_bad_snippet_present_line_level_match(self):
        """Verify _bad_snippet_present uses line-level comparison."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        # Multi-line snippet: all lines present -> True
        snippet = "    Get Text    css=table tr\n    Should Contain    ${text}    Active"
        code = (
            "*** Test Cases ***\n"
            "Verify Table\n"
            "    Get Text    css=table tr\n"
            "    Should Contain    ${text}    Active\n"
        )
        assert engine._bad_snippet_present(snippet, code) is True, \
            "All snippet lines present: returns True"
        conn.close()

    def test_bad_snippet_present_partial_no_match(self):
        """Verify _bad_snippet_present returns False when only some lines match."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        snippet = "    Get Text    css=table tr\n    Should Contain    ${text}    Active"
        code = (
            "*** Test Cases ***\n"
            "Verify Table\n"
            "    Get Text    css=table tr\n"
            "    Log    done\n"
        )
        assert engine._bad_snippet_present(snippet, code) is False, \
            "Only some snippet lines present: returns False"
        conn.close()

    def test_bad_snippet_present_short_keyword_no_false_positive(self):
        """Verify short snippet like 'Click' doesn't false-positive match 'Click Element'."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        # Snippet is "Click    id=submit", code has "Click Element    id=submit"
        snippet = "    Click    id=submit"
        code = "    Click Element    id=submit\n    Log    done"
        assert engine._bad_snippet_present(snippet, code) is False, \
            "Short snippet 'Click' does not match 'Click Element'"
        conn.close()

    def test_bad_snippet_present_empty_inputs(self):
        """Verify _bad_snippet_present handles empty/None inputs."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        assert engine._bad_snippet_present("", "some code") is False
        assert engine._bad_snippet_present(None, "some code") is False
        assert engine._bad_snippet_present("some snippet", "") is False
        assert engine._bad_snippet_present("some snippet", None) is False
        assert engine._bad_snippet_present("   \n  \n  ", "some code") is False
        conn.close()

    def test_bad_snippet_present_whitespace_insensitive(self):
        """Verify _bad_snippet_present is whitespace-insensitive per line."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        snippet = "  Get Text    css=table tr  "
        code = "    Get Text    css=table tr\n    Log    done"
        assert engine._bad_snippet_present(snippet, code) is True, \
            "Whitespace-insensitive: leading/trailing spaces ignored"
        conn.close()


# ---------------------------------------------------------------------------
# Tests: Edge Cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge case and interaction tests."""

    def test_long_error_message_truncated_in_hints(self):
        """Verify very long error message is truncated in hints."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)
        long_error = "X" * 500
        _seed_anti_pattern(conn,
            query_pattern="verify all rows in the table",
            error_message=long_error,
            evidence_count=5,
            score=0.7,
        )

        hints = engine.get_hints("verify all rows in the table",
                                 "https://demoqa.com", "planner")
        assert hints is not None and all(len(h) < 500 for h in hints), \
            "Long error message truncated in hints"
        conn.close()

    def test_special_characters_in_query(self):
        """Verify special characters in query are handled."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)
        record = MockRecord(
            user_query="verify row's 'status' & count > 0",
            test_status="failed",
            failure_category="A1",
            error_message="Error with special chars",
            robot_code="    Get Text    id=status",
        )
        try:
            engine.learn(record)
            count = conn.execute(
                "SELECT COUNT(*) FROM anti_patterns"
            ).fetchone()[0]
            assert count == 1, "Special characters in query handled"
        except Exception as e:
            assert False, f"Special characters in query handled: {e}"
        conn.close()

    def test_empty_db_get_hints_returns_none(self):
        """Verify empty DB get_hints returns None."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)
        hints = engine.get_hints("any query", "https://any.com", "planner")
        assert hints is None, \
            "Empty DB: get_hints returns None"
        conn.close()

    def test_learn_stores_domain_from_record(self):
        """Verify learn() stores domain from record."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)
        record = MockRecord(
            user_query="click submit button on login",
            test_status="failed",
            failure_category="C1",
            error_message="Element not found",
            robot_code="    Click    id=submit",
            domain="example.com",
        )
        engine.learn(record)
        row = conn.execute("SELECT * FROM anti_patterns").fetchone()
        assert row["domain"] == "example.com", \
            "learn() stores domain from record"
        conn.close()

    def test_multiple_anti_patterns_each_yields_hint(self):
        """get_hints emits one planner hint per anti-pattern returned by
        _find_matching_anti_patterns. Relevance narrowing is the
        query-similarity filter's job (tested with real ChromaDB in
        test_query_similarity.py); with no ChromaDB the filter fails open,
        so all three gated anti-patterns yield hints."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        _seed_anti_pattern(conn,
            failure_category="A1",
            query_pattern="verify all rows in the table",
            evidence_count=5,
            score=0.8,
        )
        _seed_anti_pattern(conn,
            failure_category="B1",
            query_pattern="fill in the login form fields",
            evidence_count=4,
            score=0.6,
        )
        _seed_anti_pattern(conn,
            failure_category="C1",
            query_pattern="click the submit button element",
            evidence_count=3,
            score=0.5,
        )

        hints = engine.get_hints("verify the table rows",
                                 "https://demoqa.com", "planner")
        assert hints is not None and len(hints) == 3

        conn.close()

    def test_multiple_anti_patterns_stats_reflects_all(self):
        """Verify stats reflect all anti-patterns."""
        conn = _create_test_db()
        engine = AntiPatternEngine(conn)

        _seed_anti_pattern(conn,
            failure_category="A1",
            query_pattern="verify all rows in the table",
            evidence_count=5,
            score=0.8,
        )
        _seed_anti_pattern(conn,
            failure_category="B1",
            query_pattern="fill in the login form fields",
            evidence_count=4,
            score=0.6,
        )
        _seed_anti_pattern(conn,
            failure_category="C1",
            query_pattern="click the submit button element",
            evidence_count=3,
            score=0.5,
        )

        stats = engine.get_stats()
        assert stats["total_anti_patterns"] == 3, \
            "Stats total reflects all anti-patterns"

        conn.close()

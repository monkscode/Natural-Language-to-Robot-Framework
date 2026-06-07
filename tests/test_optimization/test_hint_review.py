"""
Tests for LLM hint review — Step 7 of the learning-prompt hardening initiative.

Covers:
  - _build_review_prompt: prompt structure for globals chunk, domain chunk with
    global context, and empty context_global_hints (Tests 1–3).
  - _run_hint_review: chunk-loop partial-failure path — globals chunk succeeds,
    domain chunk fails on both retry attempts → session ends as 'pending_review'
    (Test 4).
  - TestGetHintTimelineLabels: get_hint timeline SQL correctly labels trigger_events
    rows as 'flagged' vs 'flag_recommended_suppressed' based on actually_flagged_hint_ids
    (M1 acceptance criteria — schema v12 split).

Referenced by: docs/LEARNING_PROMPTS_ANALYSIS.md §7 (Step 7)
Depends on: learning_endpoints._build_review_prompt, _run_hint_review
"""

import json
import sqlite3
import types
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.backend.api.learning_endpoints import _build_review_prompt, _run_hint_review
from src.backend.crew_ai.optimization.schema_manager import SchemaManager


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _h(
    hint_id: int,
    *,
    scope: str = "global",
    domain: str | None = None,
    is_active: int = 1,
    feedback_text: str | None = None,
    applied_count: int = 5,
    success_count: int = 4,
    failure_count: int = 1,
    disabled_at: str | None = None,
    conflict_flagged: int = 0,
    conflict_flag_reason: str | None = None,
    conflict_flagged_at: str | None = None,
) -> dict:
    """Return a minimal hint dict compatible with _build_review_prompt."""
    return {
        "id": hint_id,
        "is_active": is_active,
        "disabled_at": disabled_at,
        "applied_count": applied_count,
        "success_count": success_count,
        "failure_count": failure_count,
        "feedback_text": feedback_text or f"Hint text for hint {hint_id}",
        "scope": scope,
        "domain": domain,
        "original_failure_category": None,
        "conflict_flagged": conflict_flagged,
        "conflict_flag_reason": conflict_flag_reason,
        "conflict_flagged_at": conflict_flagged_at,
    }


# ---------------------------------------------------------------------------
# Tests 1–3: _build_review_prompt
# ---------------------------------------------------------------------------

class TestBuildReviewPrompt:
    """Snapshot tests for the three chunk configurations."""

    def test_globals_chunk_no_context_block(self):
        """Globals chunk: decision_hints only, no context_global_hints.

        Expects: HINTS TO REVIEW header present; GLOBAL HINTS block absent;
        both hint IDs in the prompt.
        """
        hints = [_h(10, scope="global"), _h(11, scope="global")]
        prompt = _build_review_prompt(
            hints,
            exoneration_counts={},
            flag_counts={},
            context_global_hints=None,
        )

        assert "HINTS TO REVIEW (decisions required):" in prompt
        assert "GLOBAL HINTS (context-only" not in prompt
        assert "ID: 10" in prompt
        assert "ID: 11" in prompt

    def test_domain_chunk_with_global_context(self):
        """Domain chunk: decision_hints + context_global_hints.

        Expects: HINTS TO REVIEW header; GLOBAL HINTS context block;
        domain IDs precede the context block; global IDs in [G{id}] format;
        context instruction text present.
        """
        decision_hints = [
            _h(20, scope="domain", domain="example.com"),
            _h(21, scope="domain", domain="example.com"),
        ]
        context_hints = [
            _h(30, scope="global", feedback_text="Global hint alpha"),
            _h(31, scope="global", feedback_text="Global hint beta"),
        ]
        prompt = _build_review_prompt(
            decision_hints,
            exoneration_counts={},
            flag_counts={},
            context_global_hints=context_hints,
        )

        assert "HINTS TO REVIEW (decisions required):" in prompt
        assert "GLOBAL HINTS (context-only" in prompt

        # Domain IDs must appear before the context block.
        hints_pos = prompt.find("HINTS TO REVIEW (decisions required):")
        context_pos = prompt.find("GLOBAL HINTS (context-only")
        id20_pos = prompt.find("ID: 20")
        id21_pos = prompt.find("ID: 21")
        assert hints_pos < id20_pos < context_pos
        assert hints_pos < id21_pos < context_pos

        # Global IDs appear in [G{id}] format inside the context block.
        assert "[G30]" in prompt
        assert "[G31]" in prompt

        # Context instruction is present.
        assert "do not produce decisions" in prompt

    def test_empty_context_global_hints_no_context_block(self):
        """Empty list for context_global_hints → no GLOBAL HINTS block rendered."""
        hints = [_h(40, scope="domain", domain="shop.example.com")]
        prompt = _build_review_prompt(
            hints,
            exoneration_counts={},
            flag_counts={},
            context_global_hints=[],
        )

        assert "GLOBAL HINTS (context-only" not in prompt


# ---------------------------------------------------------------------------
# Test 4: _run_hint_review chunk-loop partial failure
# ---------------------------------------------------------------------------

class TestRunHintReview:
    """_run_hint_review: globals chunk succeeds, domain chunk fails → pending_review."""

    @staticmethod
    def _insert_test_data(conn: sqlite3.Connection) -> int:
        """Populate the test DB and return the session_id (always 1)."""
        now = datetime.now(timezone.utc).isoformat()

        conn.execute(
            "INSERT INTO hint_review_sessions (id, status, hint_count, created_at) "
            "VALUES (1, 'pending_llm', 0, ?)",
            (now,),
        )

        rows = [
            # (id, text,                   scope,    domain,        is_active, applied, success, failure, disabled_at)
            (1, "Global hint one",          "global", None,          1,         5,       4,       1,       None),
            (2, "Domain hint one",          "domain", "example.com", 1,         3,       2,       1,       None),
            (3, "Domain hint two",          "domain", "example.com", 1,         2,       2,       0,       None),
            (4, "Recently disabled global", "global", None,          0,         10,      5,       5,       now),
        ]
        for (hid, text, scope, domain, active, applied, success, failure, dis_at) in rows:
            conn.execute(
                "INSERT INTO nl_feedback_corrections "
                "(id, feedback_text, scope, domain, is_active, applied_count, "
                " success_count, failure_count, disabled_at, created_at, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (hid, text, scope, domain, active, applied, success, failure,
                 dis_at, now, now),
            )

        conn.commit()
        return 1

    @staticmethod
    def _make_llm_response(hint_ids: list[int]) -> object:
        """Return a SimpleNamespace mimicking litellm ModelResponse for given hint IDs."""
        content = json.dumps({
            "decisions": [
                {"id": hid, "recommendation": "keep", "reason": "looks fine"}
                for hid in hint_ids
            ],
            "summary": "reviewed",
        })
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))]
        )

    def test_chunk_loop_partial_failure(self, tmp_db_path):
        """Globals chunk succeeds; domain chunk fails on both attempts.

        Resulting session status must be 'pending_review' (not 'failed') because
        at least one chunk succeeded. The domain page must record status='failed'
        and the global page status='succeeded'. Recommendations are persisted only
        for the successful chunk.
        """
        # --- Setup DB ---
        setup_conn = sqlite3.connect(tmp_db_path)
        setup_conn.row_factory = sqlite3.Row
        setup_conn.execute("PRAGMA journal_mode=WAL")
        SchemaManager.ensure_current(setup_conn)
        session_id = self._insert_test_data(setup_conn)
        setup_conn.close()

        # --- Mock LLM response for globals chunk (hints 1 and 4) ---
        globals_response = self._make_llm_response([1, 4])
        # Global chunk: 1 success call. Domain chunk: 2 failing calls (attempt 0 + retry).
        llm_side_effect = [
            globals_response,
            RuntimeError("LLM network error"),
            RuntimeError("LLM network error retry"),
        ]

        # --- Mock _admin_conn to open fresh connections to tmp_db_path ---
        def admin_conn_factory():
            c = sqlite3.connect(tmp_db_path, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=5000")
            return c

        mock_fb = MagicMock()

        with (
            patch("src.backend.api.learning_endpoints._admin_conn",
                  side_effect=admin_conn_factory),
            patch("src.backend.api.learning_endpoints._call_conflict_detection_llm",
                  side_effect=llm_side_effect),
            patch("src.backend.api.learning_endpoints._get_conflict_detection_model",
                  return_value="test-model"),
            patch("src.backend.api.learning_endpoints._get_conflict_detection_completion_kwargs",
                  return_value={}),
        ):
            _run_hint_review(session_id, mock_fb)

        # --- Assert final DB state ---
        check_conn = sqlite3.connect(tmp_db_path)
        check_conn.row_factory = sqlite3.Row
        try:
            session = check_conn.execute(
                "SELECT status FROM hint_review_sessions WHERE id=?", (session_id,)
            ).fetchone()
            assert session["status"] == "pending_review", (
                f"Session status should be 'pending_review', got {session['status']!r}"
            )

            pages = check_conn.execute(
                "SELECT scope_type, status FROM hint_review_pages "
                "WHERE session_id=? ORDER BY scope_type",
                (session_id,),
            ).fetchall()
            assert len(pages) == 2, f"Expected 2 page rows, got {len(pages)}"

            pages_by_scope = {p["scope_type"]: p["status"] for p in pages}
            assert pages_by_scope["global"] == "succeeded", (
                f"Global page should be 'succeeded', got {pages_by_scope.get('global')!r}"
            )
            assert pages_by_scope["domain"] == "failed", (
                f"Domain page should be 'failed', got {pages_by_scope.get('domain')!r}"
            )

            recs = check_conn.execute(
                "SELECT hint_id FROM hint_review_recommendations WHERE session_id=?",
                (session_id,),
            ).fetchall()
            rec_ids = {r["hint_id"] for r in recs}
            assert len(recs) == 2, f"Expected 2 recommendations, got {len(recs)}"
            assert rec_ids == {1, 4}, f"Expected recommendations for hints {{1, 4}}, got {rec_ids}"
        finally:
            check_conn.close()


# ---------------------------------------------------------------------------
# Test 5: apply_review_session — reactivate audit includes all cleared columns
# ---------------------------------------------------------------------------

class TestApplyReviewSessionReactivateAudit:
    """apply_review_session reactivate branch writes a complete after_value.

    Finding 8: the reactivate branch previously omitted conflict_flagged_at
    and conflict_flag_reason from the after_value JSON in hint_audit, making
    the audit log inconsistent with the unflag branch.
    """

    @staticmethod
    def _setup_db(conn: sqlite3.Connection) -> tuple[int, int]:
        """Insert a conflicted disabled hint + approved reactivate recommendation.

        Returns (session_id, hint_id).
        """
        now = datetime.now(timezone.utc).isoformat()
        SchemaManager.ensure_current(conn)

        conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(id, feedback_text, scope, domain, is_active, applied_count, "
            " success_count, failure_count, conflict_flagged, "
            " conflict_flagged_at, conflict_flag_reason, disabled_at, "
            " created_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1, "Use explicit waits", "global", None,
                0, 10, 5, 5,       # disabled, was active
                1,                  # conflict_flagged
                now,                # conflict_flagged_at
                "Conflicts with hint 2",  # conflict_flag_reason
                now,                # disabled_at
                now, now,
            ),
        )

        conn.execute(
            "INSERT INTO hint_review_sessions "
            "(id, status, hint_count, created_at) "
            "VALUES (1, 'pending_review', 1, ?)",
            (now,),
        )

        conn.execute(
            "INSERT INTO hint_review_recommendations "
            "(id, session_id, hint_id, recommendation, reason, admin_decision, applied, created_at) "
            "VALUES (1, 1, 1, 'reactivate', 'Conflict resolved, safe to reactivate', 'approved', 0, ?)",
            (now,),
        )

        conn.commit()
        return 1, 1

    def test_reactivate_after_value_includes_conflict_columns(self, tmp_db_path):
        """after_value in hint_audit must include conflict_flagged_at and conflict_flag_reason."""
        from src.backend.api.learning_endpoints import apply_review_session
        from unittest.mock import MagicMock, patch

        setup_conn = sqlite3.connect(tmp_db_path)
        setup_conn.row_factory = sqlite3.Row
        setup_conn.execute("PRAGMA journal_mode=WAL")
        session_id, hint_id = self._setup_db(setup_conn)
        setup_conn.close()

        def admin_conn_factory():
            c = sqlite3.connect(tmp_db_path, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=5000")
            return c

        mock_fb = MagicMock()

        with patch("src.backend.api.learning_endpoints._admin_conn",
                   side_effect=admin_conn_factory):
            apply_review_session(session_id, fb=mock_fb)

        check_conn = sqlite3.connect(tmp_db_path)
        check_conn.row_factory = sqlite3.Row
        try:
            audit_row = check_conn.execute(
                "SELECT action, after_value FROM hint_audit WHERE hint_id=?",
                (hint_id,),
            ).fetchone()

            assert audit_row is not None, "Expected a hint_audit row for the reactivated hint"
            assert audit_row["action"] == "llm_review_reactivate"

            after = json.loads(audit_row["after_value"])
            assert after["is_active"] == 1, f"is_active should be 1, got {after.get('is_active')}"
            assert after["conflict_flagged"] == 0, (
                f"conflict_flagged should be 0, got {after.get('conflict_flagged')}"
            )
            assert after["conflict_flagged_at"] is None, (
                f"conflict_flagged_at should be None, got {after.get('conflict_flagged_at')!r}"
            )
            assert after["conflict_flag_reason"] is None, (
                f"conflict_flag_reason should be None, got {after.get('conflict_flag_reason')!r}"
            )
            assert after["disabled_at"] is None, (
                f"disabled_at should be None, got {after.get('disabled_at')!r}"
            )
        finally:
            check_conn.close()


# ---------------------------------------------------------------------------
# M1 acceptance criteria — get_hint timeline SQL labels (schema v12 split)
# ---------------------------------------------------------------------------

_TIMELINE_SQL = (
    "SELECT CASE "
    "  WHEN EXISTS ("
    "    SELECT 1 FROM json_each(COALESCE(actually_flagged_hint_ids, flagged_hint_ids)) "
    "    WHERE CAST(value AS INTEGER) = ?"
    "  ) THEN 'flagged' "
    "  ELSE 'flag_recommended_suppressed' "
    "END AS action "
    "FROM trigger_events "
    "WHERE flagged_hint_ids IS NOT NULL "
    "  AND flagged_hint_ids != '[]' "
    "  AND EXISTS ("
    "    SELECT 1 FROM json_each(flagged_hint_ids) "
    "    WHERE CAST(value AS INTEGER) = ?"
    "  )"
)


def _make_timeline_db() -> tuple:
    """Return (conn, hint_id) with a fully-migrated in-memory DB and one hint."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    SchemaManager.ensure_current(conn)
    now = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO nl_feedback_corrections "
        "(feedback_text, created_at, last_seen) VALUES (?, ?, ?)",
        ("use Browser Library Click", now, now),
    )
    hint_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    return conn, hint_id


class TestGetHintTimelineLabels:
    """SQL CASE expression in get_hint correctly labels suppressed vs actual flags."""

    def test_suppressed_flag_labelled_flag_recommended_suppressed(self):
        """Hint in flagged_hint_ids but not in actually_flagged_hint_ids → suppressed."""
        conn, hint_id = _make_timeline_db()
        now = "2026-01-01T00:00:00+00:00"
        conn.execute(
            "INSERT INTO trigger_events "
            "(trigger_type, flagged_hint_ids, actually_flagged_hint_ids, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("trigger_2", json.dumps([hint_id]), json.dumps([]), "succeeded", now),
        )
        conn.commit()
        rows = conn.execute(_TIMELINE_SQL, (hint_id, hint_id)).fetchall()
        assert len(rows) == 1
        assert rows[0]["action"] == "flag_recommended_suppressed", rows[0]["action"]
        conn.close()

    def test_actually_flagged_labelled_flagged(self):
        """Hint in both flagged_hint_ids and actually_flagged_hint_ids → flagged."""
        conn, hint_id = _make_timeline_db()
        now = "2026-01-01T00:00:00+00:00"
        conn.execute(
            "INSERT INTO trigger_events "
            "(trigger_type, flagged_hint_ids, actually_flagged_hint_ids, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("trigger_1", json.dumps([hint_id]), json.dumps([hint_id]), "succeeded", now),
        )
        conn.commit()
        rows = conn.execute(_TIMELINE_SQL, (hint_id, hint_id)).fetchall()
        assert len(rows) == 1
        assert rows[0]["action"] == "flagged", rows[0]["action"]
        conn.close()

    def test_legacy_null_actually_flagged_falls_back_to_flagged(self):
        """Legacy row: actually_flagged_hint_ids NULL → COALESCE to flagged_hint_ids → flagged."""
        conn, hint_id = _make_timeline_db()
        now = "2026-01-01T00:00:00+00:00"
        conn.execute(
            "INSERT INTO trigger_events "
            "(trigger_type, flagged_hint_ids, actually_flagged_hint_ids, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("trigger_1", json.dumps([hint_id]), None, "succeeded", now),
        )
        conn.commit()
        rows = conn.execute(_TIMELINE_SQL, (hint_id, hint_id)).fetchall()
        assert len(rows) == 1
        assert rows[0]["action"] == "flagged", rows[0]["action"]
        conn.close()

    def test_hint_not_in_flagged_hint_ids_excluded_from_results(self):
        """Trigger event for a different hint does not appear in this hint's timeline."""
        conn, hint_id = _make_timeline_db()
        now = "2026-01-01T00:00:00+00:00"
        other_id = hint_id + 99
        conn.execute(
            "INSERT INTO trigger_events "
            "(trigger_type, flagged_hint_ids, actually_flagged_hint_ids, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("trigger_2", json.dumps([other_id]), json.dumps([]), "succeeded", now),
        )
        conn.commit()
        rows = conn.execute(_TIMELINE_SQL, (hint_id, hint_id)).fetchall()
        assert len(rows) == 0
        conn.close()

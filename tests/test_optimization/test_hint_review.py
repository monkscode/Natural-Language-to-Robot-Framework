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
import re
import types
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.backend.api.learning_endpoints import _build_review_prompt, _run_hint_review
from src.backend.crew_ai.optimization import pg_compat


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
    def _insert_test_data(conn) -> int:
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

    def test_chunk_loop_partial_failure(self, in_memory_em):
        """Globals chunk succeeds; domain chunk fails on both attempts.

        Resulting session status must be 'pending_review' (not 'failed') because
        at least one chunk succeeded. The domain page must record status='failed'
        and the global page status='succeeded'. Recommendations are persisted only
        for the successful chunk.
        """
        # --- Setup DB (the in_memory_em fixture provides a clean schema) ---
        dsn = in_memory_em.dsn
        setup_conn = pg_compat.connect(dsn)
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

        # --- Mock _admin_conn to open fresh connections to the test schema ---
        def admin_conn_factory():
            # Mirrors production _admin_conn (pg_compat, default transactions).
            return pg_compat.connect(dsn)

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
        check_conn = pg_compat.connect(dsn)
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
    def _setup_db(conn) -> tuple[int, int]:
        """Insert a conflicted disabled hint + approved reactivate recommendation.

        Returns (session_id, hint_id).
        """
        now = datetime.now(timezone.utc).isoformat()

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

    def test_reactivate_after_value_includes_conflict_columns(self, in_memory_em):
        """after_value in hint_audit must include conflict_flagged_at and conflict_flag_reason."""
        from src.backend.api.learning_endpoints import apply_review_session
        from unittest.mock import MagicMock, patch

        dsn = in_memory_em.dsn
        setup_conn = pg_compat.connect(dsn)
        session_id, hint_id = self._setup_db(setup_conn)
        setup_conn.close()

        def admin_conn_factory():
            # Mirrors production _admin_conn (pg_compat, default transactions).
            return pg_compat.connect(dsn)

        mock_fb = MagicMock()

        with patch("src.backend.api.learning_endpoints._admin_conn",
                   side_effect=admin_conn_factory):
            apply_review_session(session_id, fb=mock_fb)

        check_conn = pg_compat.connect(dsn)
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
    "  WHEN COALESCE(actually_flagged_hint_ids, flagged_hint_ids) "
    "       @> to_jsonb(?::int) THEN 'flagged' "
    "  ELSE 'flag_recommended_suppressed' "
    "END AS action "
    "FROM trigger_events "
    "WHERE flagged_hint_ids IS NOT NULL "
    "  AND flagged_hint_ids <> '[]'::jsonb "
    "  AND flagged_hint_ids @> to_jsonb(?::int)"
)


def _make_timeline_db(dsn) -> tuple:
    """Return (conn, hint_id) on the clean Postgres test schema with one hint."""
    conn = pg_compat.connect(dsn)
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

    def test_suppressed_flag_labelled_flag_recommended_suppressed(self, in_memory_em):
        """Hint in flagged_hint_ids but not in actually_flagged_hint_ids → suppressed."""
        conn, hint_id = _make_timeline_db(in_memory_em.dsn)
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

    def test_actually_flagged_labelled_flagged(self, in_memory_em):
        """Hint in both flagged_hint_ids and actually_flagged_hint_ids → flagged."""
        conn, hint_id = _make_timeline_db(in_memory_em.dsn)
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

    def test_legacy_null_actually_flagged_falls_back_to_flagged(self, in_memory_em):
        """Legacy row: actually_flagged_hint_ids NULL → COALESCE to flagged_hint_ids → flagged."""
        conn, hint_id = _make_timeline_db(in_memory_em.dsn)
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

    def test_hint_not_in_flagged_hint_ids_excluded_from_results(self, in_memory_em):
        """Trigger event for a different hint does not appear in this hint's timeline."""
        conn, hint_id = _make_timeline_db(in_memory_em.dsn)
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


# ---------------------------------------------------------------------------
# Task 3 (F2) — the LLM hint review must not cross orgs
# ---------------------------------------------------------------------------
#
# Two orgs with identical `global` hint text (mandatory under T10's
# copy-on-promote design, not accidental) used to land in ONE
# hint_review_pages row and ONE LLM prompt, so org B's hint could be disabled
# on org A's evidence. The fix groups the fetched hints by org_id before
# building the chunk plan: one global chunk + one chunk per distinct domain,
# PER org, and a domain chunk's context_global_hints is that org's globals
# only. hint_review_pages.org_id (added by Task 2) records which org each
# page belongs to.

def _org_llm_side_effect(model_string, messages, extra_kwargs, timeout=120):
    """Echo a 'keep' decision for every hint ID this chunk asked a decision for.

    Parses "ID: <n>" lines from the prompt (the decision-hints section) —
    the [G<n>] context-block lines never match, so context-only globals never
    get spurious decisions.
    """
    content = messages[0]["content"]
    ids = [int(m) for m in re.findall(r"^ID: (\d+)$", content, re.MULTILINE)]
    payload = json.dumps({
        "decisions": [
            {"id": hid, "recommendation": "keep", "reason": "ok"} for hid in ids
        ],
        "summary": "reviewed",
    })
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=payload))]
    )


class TestRunHintReviewOrgIsolation:
    """_run_hint_review's chunk plan must partition by org, not just scope/domain."""

    @staticmethod
    def _insert_hint(conn, hint_id: int, org_id: str | None, *, scope: str,
                      domain: str | None, feedback_text: str, now: str) -> None:
        conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(id, feedback_text, scope, domain, is_active, applied_count, "
            " success_count, failure_count, org_id, created_at, last_seen) "
            "VALUES (?, ?, ?, ?, 1, 5, 4, 1, ?, ?, ?)",
            (hint_id, feedback_text, scope, domain, org_id, now, now),
        )

    def test_two_orgs_identical_global_text_produce_two_pages(self, in_memory_em):
        """Same global hint text in org A and org B must not collapse into one page.

        Before the fix: one 'global' chunk holds both hints, one page row,
        one LLM prompt — org B's hint could be disabled on org A's evidence.
        After the fix: two page rows, one per org.
        """
        dsn = in_memory_em.dsn
        conn = pg_compat.connect(dsn)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO hint_review_sessions (id, status, hint_count, created_at) "
            "VALUES (1, 'pending_llm', 0, ?)", (now,),
        )
        self._insert_hint(conn, 2, "org-a", scope="global", domain=None,
                           feedback_text="Use explicit waits before clicking", now=now)
        self._insert_hint(conn, 3, "org-b", scope="global", domain=None,
                           feedback_text="Use explicit waits before clicking", now=now)
        conn.commit()
        conn.close()

        mock_fb = MagicMock()
        with (
            patch("src.backend.api.learning_endpoints._admin_conn",
                  side_effect=lambda: pg_compat.connect(dsn)),
            patch("src.backend.api.learning_endpoints._call_conflict_detection_llm",
                  side_effect=_org_llm_side_effect),
            patch("src.backend.api.learning_endpoints._get_conflict_detection_model",
                  return_value="test-model"),
            patch("src.backend.api.learning_endpoints._get_conflict_detection_completion_kwargs",
                  return_value={}),
        ):
            _run_hint_review(1, mock_fb)

        check = pg_compat.connect(dsn)
        try:
            pages = check.execute(
                "SELECT scope_type, org_id, status FROM hint_review_pages "
                "WHERE session_id=1 ORDER BY org_id"
            ).fetchall()
            assert len(pages) == 2, f"Expected 2 pages (one per org), got {len(pages)}: {pages}"
            orgs = {p["org_id"] for p in pages}
            assert orgs == {"org-a", "org-b"}, orgs
            for p in pages:
                assert p["scope_type"] == "global"
                assert p["status"] == "succeeded"

            recs = check.execute(
                "SELECT hint_id FROM hint_review_recommendations WHERE session_id=1"
            ).fetchall()
            rec_ids = {r["hint_id"] for r in recs}
            assert rec_ids == {2, 3}, rec_ids
        finally:
            check.close()

    def test_domain_chunk_context_carries_only_its_own_org_globals(self, in_memory_em):
        """A domain chunk's GLOBAL HINTS context block must not include another org's globals.

        Both orgs use the SAME domain string (shop.example.com) so a
        domain-only grouping bug (forgetting org) would not be caught by
        page count alone — the assertion is on prompt CONTENT.
        """
        dsn = in_memory_em.dsn
        conn = pg_compat.connect(dsn)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO hint_review_sessions (id, status, hint_count, created_at) "
            "VALUES (1, 'pending_llm', 0, ?)", (now,),
        )
        self._insert_hint(conn, 10, "org-a", scope="global", domain=None,
                           feedback_text="Org A global hint", now=now)
        self._insert_hint(conn, 11, "org-a", scope="domain", domain="shop.example.com",
                           feedback_text="Org A domain hint", now=now)
        self._insert_hint(conn, 20, "org-b", scope="global", domain=None,
                           feedback_text="Org B global hint", now=now)
        self._insert_hint(conn, 21, "org-b", scope="domain", domain="shop.example.com",
                           feedback_text="Org B domain hint", now=now)
        conn.commit()
        conn.close()

        captured_prompts: list[str] = []

        def _capturing_llm(model_string, messages, extra_kwargs, timeout=120):
            captured_prompts.append(messages[0]["content"])
            return _org_llm_side_effect(model_string, messages, extra_kwargs, timeout)

        mock_fb = MagicMock()
        with (
            patch("src.backend.api.learning_endpoints._admin_conn",
                  side_effect=lambda: pg_compat.connect(dsn)),
            patch("src.backend.api.learning_endpoints._call_conflict_detection_llm",
                  side_effect=_capturing_llm),
            patch("src.backend.api.learning_endpoints._get_conflict_detection_model",
                  return_value="test-model"),
            patch("src.backend.api.learning_endpoints._get_conflict_detection_completion_kwargs",
                  return_value={}),
        ):
            _run_hint_review(1, mock_fb)

        org_a_domain_prompt = next(p for p in captured_prompts if "ID: 11" in p)
        org_b_domain_prompt = next(p for p in captured_prompts if "ID: 21" in p)

        assert "[G10]" in org_a_domain_prompt, "org A's domain chunk must see org A's global as context"
        assert "[G20]" not in org_a_domain_prompt, "org A's domain chunk must NOT see org B's global"

        assert "[G20]" in org_b_domain_prompt, "org B's domain chunk must see org B's global as context"
        assert "[G10]" not in org_b_domain_prompt, "org B's domain chunk must NOT see org A's global"

        # Page rows: 4 total (1 global + 1 domain per org), each stamped with its own org.
        check = pg_compat.connect(dsn)
        try:
            pages = check.execute(
                "SELECT scope_type, org_id FROM hint_review_pages WHERE session_id=1"
            ).fetchall()
            assert len(pages) == 4, f"Expected 4 pages, got {len(pages)}: {pages}"
            assert all(p["org_id"] in ("org-a", "org-b") for p in pages), pages
        finally:
            check.close()


class TestApplyReviewSessionOrgGuard:
    """apply_review_session must not mutate a hint whose org no longer matches
    the org the approved recommendation was selected against — defence in
    depth per Task 3 (F2). It is NOT the correctness fix (the chunk-plan
    grouping above is); on correct, non-racing data this predicate changes
    nothing. The scenario below simulates a hint whose org_id is reassigned
    (committed by a second connection) between apply_review_session's
    approved_recs SELECT and its UPDATE, inside the same transaction —
    Postgres READ COMMITTED means that intervening commit is visible to the
    next statement in this transaction.

    Fix round 1 (coordinator Important finding): a guard-blocked UPDATE
    (0 rows matched) must not also fabricate a hint_audit row claiming the
    change happened, must not mark the recommendation applied=1, and must
    not count toward the response's applied_count — otherwise the guard is
    SQL-correct but the system's own record of what it did is false, and the
    recommendation can never be retried. All three tests below (one per
    guarded branch, per the coordinator's Minor finding) assert on all four
    of: the hint's row state, hint_audit contents, the recommendation's
    `applied` column, and the response body — not just the row state that
    the first round's single disable-only test covered.
    """

    @staticmethod
    def _setup_disable(conn) -> tuple[int, int]:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(id, feedback_text, scope, domain, is_active, applied_count, "
            " success_count, failure_count, org_id, created_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1, "Use explicit waits", "global", None, 1, 10, 8, 2, "org-a", now, now),
        )
        conn.execute(
            "INSERT INTO hint_review_sessions (id, status, hint_count, created_at) "
            "VALUES (1, 'pending_review', 1, ?)", (now,),
        )
        conn.execute(
            "INSERT INTO hint_review_recommendations "
            "(id, session_id, hint_id, recommendation, reason, admin_decision, applied, created_at) "
            "VALUES (1, 1, 1, 'disable', 'duplicate', 'approved', 0, ?)", (now,),
        )
        conn.commit()
        return 1, 1

    @staticmethod
    def _setup_reactivate(conn) -> tuple[int, int]:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(id, feedback_text, scope, domain, is_active, applied_count, "
            " success_count, failure_count, conflict_flagged, conflict_flagged_at, "
            " conflict_flag_reason, disabled_at, org_id, created_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1, "Use explicit waits", "global", None, 0, 10, 8, 2,
             1, now, "Conflicts with hint 2", now, "org-a", now, now),
        )
        conn.execute(
            "INSERT INTO hint_review_sessions (id, status, hint_count, created_at) "
            "VALUES (1, 'pending_review', 1, ?)", (now,),
        )
        conn.execute(
            "INSERT INTO hint_review_recommendations "
            "(id, session_id, hint_id, recommendation, reason, admin_decision, applied, created_at) "
            "VALUES (1, 1, 1, 'reactivate', 'looks fine now', 'approved', 0, ?)", (now,),
        )
        conn.commit()
        return 1, 1

    @staticmethod
    def _setup_unflag(conn) -> tuple[int, int]:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(id, feedback_text, scope, domain, is_active, applied_count, "
            " success_count, failure_count, conflict_flagged, conflict_flagged_at, "
            " conflict_flag_reason, org_id, created_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1, "Use explicit waits", "global", None, 1, 10, 8, 2,
             1, now, "Trigger 2 flagged this", "org-a", now, now),
        )
        conn.execute(
            "INSERT INTO hint_review_sessions (id, status, hint_count, created_at) "
            "VALUES (1, 'pending_review', 1, ?)", (now,),
        )
        conn.execute(
            "INSERT INTO hint_review_recommendations "
            "(id, session_id, hint_id, recommendation, reason, admin_decision, applied, created_at) "
            "VALUES (1, 1, 1, 'unflag', 'flag looks wrong', 'approved', 0, ?)", (now,),
        )
        conn.commit()
        return 1, 1

    @staticmethod
    def _make_racy_audit_actor(dsn, hint_id):
        """_audit_actor side effect: on its first call only, a SECOND
        connection commits hint_id's org_id org-a -> org-b, then closes —
        simulating a race between the approved_recs SELECT (already done by
        the time apply_review_session reaches this call) and the current
        branch's guarded UPDATE (the very next statement). Delegates to the
        real _audit_actor so the rest of the call behaves normally.
        """
        from src.backend.api.learning_endpoints import _audit_actor as real_audit_actor
        raced = {"done": False}

        def racy_audit_actor(admin, request_actor=None):
            if not raced["done"]:
                raced["done"] = True
                race_conn = pg_compat.connect(dsn)
                race_conn.execute(
                    "UPDATE nl_feedback_corrections SET org_id=? WHERE id=?",
                    ("org-b", hint_id),
                )
                race_conn.commit()
                race_conn.close()
            return real_audit_actor(admin, request_actor)

        return racy_audit_actor

    def _run_racy_apply(self, in_memory_em, caplog, setup_fn):
        """Shared drive: seed via setup_fn, race the hint to org-b mid-apply,
        call apply_review_session, return (result, hint_id, dsn) for the
        caller's branch-specific assertions."""
        from src.backend.api.learning_endpoints import apply_review_session

        dsn = in_memory_em.dsn
        setup_conn = pg_compat.connect(dsn)
        session_id, hint_id = setup_fn(setup_conn)
        setup_conn.close()

        def admin_conn_factory():
            return pg_compat.connect(dsn)

        mock_fb = MagicMock()
        with (
            patch("src.backend.api.learning_endpoints._admin_conn",
                  side_effect=admin_conn_factory),
            patch("src.backend.api.learning_endpoints._audit_actor",
                  side_effect=self._make_racy_audit_actor(dsn, hint_id)),
            caplog.at_level("WARNING", logger="src.backend.api.learning_endpoints"),
        ):
            result = apply_review_session(session_id, fb=mock_fb)

        return result, session_id, hint_id, dsn

    def _assert_guard_blocked_bookkeeping(self, dsn, session_id, hint_id, result, caplog):
        """The four assertions the coordinator's Important finding requires,
        shared by all three branch tests below."""
        assert result["applied_count"] == 0, (
            "a guard-blocked write must not count toward applied_count"
        )

        check_conn = pg_compat.connect(dsn)
        try:
            audit_rows = check_conn.execute(
                "SELECT action FROM hint_audit WHERE hint_id=?", (hint_id,),
            ).fetchall()
            assert audit_rows == [], (
                "a blocked write must not fabricate an audit row, got "
                f"{[r['action'] for r in audit_rows]}"
            )

            rec = check_conn.execute(
                "SELECT applied FROM hint_review_recommendations "
                "WHERE session_id=? AND hint_id=?",
                (session_id, hint_id),
            ).fetchone()
            assert rec["applied"] == 0, (
                "recommendation must stay unapplied (applied=0) so it can be retried"
            )
        finally:
            check_conn.close()

        warnings = [r.message for r in caplog.records if r.levelno >= 30]  # WARNING=30
        assert any(str(session_id) in m and str(hint_id) in m for m in warnings), (
            f"expected a warning naming the session and hint id, got: {warnings}"
        )

    def test_disable_org_reassigned_mid_transaction_blocks_everything(self, in_memory_em, caplog):
        result, session_id, hint_id, dsn = self._run_racy_apply(
            in_memory_em, caplog, self._setup_disable
        )

        check_conn = pg_compat.connect(dsn)
        try:
            hint = check_conn.execute(
                "SELECT is_active, org_id FROM nl_feedback_corrections WHERE id=?",
                (hint_id,),
            ).fetchone()
            assert hint["org_id"] == "org-b", "the race should have moved the hint to org-b"
            assert hint["is_active"] == 1, (
                "the disable must be blocked once the hint's org no longer "
                "matches the org the recommendation was approved against"
            )
        finally:
            check_conn.close()

        self._assert_guard_blocked_bookkeeping(dsn, session_id, hint_id, result, caplog)

    def test_reactivate_org_reassigned_mid_transaction_blocks_everything(self, in_memory_em, caplog):
        result, session_id, hint_id, dsn = self._run_racy_apply(
            in_memory_em, caplog, self._setup_reactivate
        )

        check_conn = pg_compat.connect(dsn)
        try:
            hint = check_conn.execute(
                "SELECT is_active, conflict_flagged, org_id "
                "FROM nl_feedback_corrections WHERE id=?",
                (hint_id,),
            ).fetchone()
            assert hint["org_id"] == "org-b", "the race should have moved the hint to org-b"
            assert hint["is_active"] == 0, (
                "the reactivate must be blocked once the hint's org no longer matches"
            )
            assert hint["conflict_flagged"] == 1, (
                "reactivate's conflict-clearing side effects must be blocked too"
            )
        finally:
            check_conn.close()

        self._assert_guard_blocked_bookkeeping(dsn, session_id, hint_id, result, caplog)

    def test_unflag_org_reassigned_mid_transaction_blocks_everything(self, in_memory_em, caplog):
        result, session_id, hint_id, dsn = self._run_racy_apply(
            in_memory_em, caplog, self._setup_unflag
        )

        check_conn = pg_compat.connect(dsn)
        try:
            hint = check_conn.execute(
                "SELECT conflict_flagged, org_id FROM nl_feedback_corrections WHERE id=?",
                (hint_id,),
            ).fetchone()
            assert hint["org_id"] == "org-b", "the race should have moved the hint to org-b"
            assert hint["conflict_flagged"] == 1, (
                "the unflag must be blocked once the hint's org no longer matches"
            )
        finally:
            check_conn.close()

        self._assert_guard_blocked_bookkeeping(dsn, session_id, hint_id, result, caplog)


# ---------------------------------------------------------------------------
# Two admins clicking "Apply" at the same instant
# ---------------------------------------------------------------------------


class TestConcurrentApplyIsSerialised:
    """`apply_review_session` must apply an approved recommendation ONCE.

    The check-then-apply pair (read status='pending_review', then write the
    hints and the audit rows) had no lock behind it on Postgres. The function
    opens with `conn.execute("BEGIN IMMEDIATE")`, which pg_compat turns into a
    no-op (`_is_noop`), so the SELECT took no row lock and two concurrent
    callers could both observe 'pending_review' and both run the whole loop.

    Reproduced against a real stack before the fix: two requests released off
    one barrier BOTH returned {"applied_count": 6} and `hint_audit` went from
    6 `llm_review_*` rows to 12 — every hint's timeline recording one approved
    decision twice. Hint state survives it (the UPDATEs are idempotent) and
    the engagement KPI survives it (both clauses are EXISTS, not COUNT), so
    the damage is confined to the audit trail — which is exactly the record
    this area exists to keep honest.

    The fix is `FOR UPDATE` on the session SELECT: the second caller blocks
    there, and re-reads the committed 'completed' status when released, so it
    takes the 409 the endpoint already had.
    """

    @staticmethod
    def _seed(conn) -> int:
        """One session, two approved recommendations covering both branches:
        'keep' (writes an audit row only) and 'disable' (also mutates state)."""
        now = datetime.now(timezone.utc).isoformat()
        for hint_id, text in ((1, "Use explicit waits"), (2, "Prefer role locators")):
            conn.execute(
                "INSERT INTO nl_feedback_corrections "
                "(id, feedback_text, scope, domain, is_active, applied_count, "
                " success_count, failure_count, conflict_flagged, org_id, "
                " created_at, last_seen) "
                "VALUES (?, ?, 'global', NULL, 1, 0, 0, 0, 0, 'org-a', ?, ?)",
                (hint_id, text, now, now),
            )
        conn.execute(
            "INSERT INTO hint_review_sessions (id, status, hint_count, created_at) "
            "VALUES (1, 'pending_review', 2, ?)", (now,),
        )
        for rec_id, hint_id, rec in ((1, 1, "keep"), (2, 2, "disable")):
            conn.execute(
                "INSERT INTO hint_review_recommendations "
                "(id, session_id, hint_id, recommendation, reason, "
                " admin_decision, applied, created_at) "
                "VALUES (?, 1, ?, ?, 'llm said so', 'approved', 0, ?)",
                (rec_id, hint_id, rec, now),
            )
        conn.commit()
        return 1

    def test_two_concurrent_applies_write_one_audit_row_per_recommendation(
        self, in_memory_em,
    ):
        import threading

        from fastapi import HTTPException

        from src.backend.api.learning_endpoints import apply_review_session

        dsn = in_memory_em.dsn
        setup_conn = pg_compat.connect(dsn)
        session_id = self._seed(setup_conn)
        setup_conn.close()

        barrier = threading.Barrier(2)
        outcomes: list = [None, None]

        def fire(slot: int):
            barrier.wait()          # both threads enter apply at the same instant
            try:
                outcomes[slot] = ("ok", apply_review_session(
                    session_id, fb=MagicMock()))
            except HTTPException as exc:
                outcomes[slot] = ("http", exc.status_code)
            except Exception as exc:                     # noqa: BLE001
                outcomes[slot] = ("err", repr(exc))

        with patch("src.backend.api.learning_endpoints._admin_conn",
                   side_effect=lambda: pg_compat.connect(dsn)):
            threads = [threading.Thread(target=fire, args=(i,)) for i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)

        kinds = sorted(kind for kind, _ in outcomes)
        assert kinds == ["http", "ok"], (
            f"exactly one apply must win and one must be refused, got {outcomes}"
        )
        winner = next(payload for kind, payload in outcomes if kind == "ok")
        loser = next(payload for kind, payload in outcomes if kind == "http")
        assert winner["applied_count"] == 2
        assert loser == 409, f"the second apply must 409, got {loser}"

        check_conn = pg_compat.connect(dsn)
        try:
            rows = check_conn.execute(
                "SELECT action FROM hint_audit WHERE action LIKE 'llm_review%' "
                "ORDER BY id"
            ).fetchall()
            actions = sorted(r["action"] for r in rows)
            assert actions == ["llm_review_disable", "llm_review_keep"], (
                "one audit row per approved recommendation — a duplicate here "
                f"means the second apply ran the loop too, got {actions}"
            )
            assert check_conn.execute(
                "SELECT status FROM hint_review_sessions WHERE id=?", (session_id,),
            ).fetchone()["status"] == "completed"
            assert [r["applied"] for r in check_conn.execute(
                "SELECT applied FROM hint_review_recommendations "
                "WHERE session_id=? ORDER BY id", (session_id,),
            ).fetchall()] == [1, 1]
        finally:
            check_conn.close()

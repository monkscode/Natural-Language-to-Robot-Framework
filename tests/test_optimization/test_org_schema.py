# tests/test_optimization/test_org_schema.py
"""Phase 1c: org_id exists on the scoped tables. is_shared no longer does.

hint_review_pages (schema v22, F2/T3 prep) joins _ORG_TABLES for the same
reason on a later timeline: a later task writes org_id on that table per row,
and this list is where a dropped/typo'd column on any scoped table already
gets caught."""

import pytest

pytestmark = pytest.mark.integration

_ORG_TABLES = (
    "execution_records", "nl_feedback_corrections", "learning_anchors",
    "execution_embeddings", "anti_patterns", "kw_query_patterns",
    "hint_review_pages",
)


def _columns(conn, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = ? AND table_schema = 'learning_test'", (table,),
    ).fetchall()
    return {r["column_name"] for r in rows}


def test_org_id_present_on_scoped_tables(in_memory_em):
    with in_memory_em.read_conn() as conn:
        for t in _ORG_TABLES:
            assert "org_id" in _columns(conn, t), f"{t} missing org_id"


def test_is_shared_is_gone_from_hints(in_memory_em):
    """Dropped in v20: a hint belongs to exactly one org. The flag was read on
    retrieval only and never on mutation, so a hint visible to every org had its
    counters, flags and disables decided by whichever tenant happened to use
    it."""
    with in_memory_em.read_conn() as conn:
        assert "is_shared" not in _columns(conn, "nl_feedback_corrections")


def test_hint_review_pages_org_id_is_nullable_text(in_memory_em):
    """v22: hint_review_pages predates org partitioning (Phase 1c), so
    existing pages have no correct org to backfill — org_id must stay
    nullable, not default to '' or any sentinel, and must be TEXT like every
    other org_id column in _ORG_TABLES."""
    with in_memory_em.read_conn() as conn:
        rows = conn.execute(
            "SELECT is_nullable, data_type FROM information_schema.columns "
            "WHERE table_name = ? AND table_schema = 'learning_test' "
            "AND column_name = 'org_id'",
            ("hint_review_pages",),
        ).fetchall()
        assert len(rows) == 1, "hint_review_pages.org_id column not found"
        assert rows[0]["is_nullable"] == "YES", (
            "hint_review_pages.org_id must stay nullable (no backfill)"
        )
        assert rows[0]["data_type"] == "text"

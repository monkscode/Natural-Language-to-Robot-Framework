# tests/test_optimization/test_org_schema.py
"""Phase 1c: org_id exists on the scoped tables. is_shared no longer does."""

import pytest

pytestmark = pytest.mark.integration

_ORG_TABLES = (
    "execution_records", "nl_feedback_corrections", "learning_anchors",
    "execution_embeddings", "anti_patterns", "kw_query_patterns",
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

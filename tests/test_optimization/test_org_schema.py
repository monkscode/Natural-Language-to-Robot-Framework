# tests/test_optimization/test_org_schema.py
"""Phase 1c: org_id (+ is_shared on hints) columns exist on the scoped tables."""

import pytest

pytestmark = pytest.mark.integration

_ORG_TABLES = (
    "execution_records", "nl_feedback_corrections", "learning_anchors",
    "execution_embeddings", "anti_patterns", "kw_query_patterns",
)


def _columns(conn, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = ?", (table,),
    ).fetchall()
    return {r["column_name"] for r in rows}


def test_org_id_present_on_scoped_tables(in_memory_em):
    with in_memory_em.read_conn() as conn:
        for t in _ORG_TABLES:
            assert "org_id" in _columns(conn, t), f"{t} missing org_id"


def test_is_shared_present_on_hints(in_memory_em):
    with in_memory_em.read_conn() as conn:
        assert "is_shared" in _columns(conn, "nl_feedback_corrections")

"""--apply then --restore returns the learning tables to exactly their pre-apply state.

Runs the real tool against real Postgres in the isolated test schema the
test_optimization conftest builds (search_path = <test schema>, public), so the
restore's SQL — named-column inserts, the vector(384) anchor, FOR UPDATE, the
drift refusals — is exercised on real tables, never on the live public schema.

Referenced by: tools/backfill_failure_categories.py
Depends on: tests/test_optimization/conftest.py (in_memory_em)
"""
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from psycopg.rows import dict_row

from tools.backfill_failure_categories import main

PLACEHOLDER_MSG = (
    "TimeoutError: locator.click: Timeout 10000ms exceeded.\n"
    "Call log:\n  - waiting for locator('//PLACEHOLDER_FOR_x')"
)
NEVER_RESOLVED_MSG = (
    "TimeoutError: locator.click: Timeout 10000ms exceeded.\n"
    "Call log:\n  - waiting for locator('#missing')"
)
VECTOR = "[" + ",".join(["0.1"] * 384) + "]"


@pytest.fixture
def test_db(in_memory_em):
    """A dict-row connection to the isolated schema, and the dsn main() must use."""
    dsn = in_memory_em.dsn
    assert "search_path" in dsn                     # never the live public schema alone
    conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
    yield conn, dsn
    for row in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema() "
                            "AND tablename LIKE 'e5b_backfill_%'").fetchall():
        conn.execute(f"DROP TABLE {row['tablename']}")
    conn.close()


def _seed(conn):
    conn.execute("INSERT INTO execution_records (workflow_id, timestamp, user_query, test_status, "
                 "failure_category, error_message) VALUES ('wf-1', '2026-09-18 10:00:00', 'q', "
                 "'failed', 'D1', %s)", (PLACEHOLDER_MSG,))
    conn.execute("INSERT INTO execution_embeddings (workflow_id, embedding, failure_category) "
                 "VALUES ('wf-1', %s::vector, 'D1')", (VECTOR,))
    conn.execute("INSERT INTO anti_patterns (id, failure_category, error_message, last_seen) "
                 "OVERRIDING SYSTEM VALUE VALUES (7, 'D1', %s, '2026-08-10 09:44:23')",
                 (NEVER_RESOLVED_MSG,))
    conn.execute("INSERT INTO anti_patterns (id, failure_category, error_message, last_seen) "
                 "OVERRIDING SYSTEM VALUE VALUES (10, 'D1', %s, '2026-08-10 09:44:23')",
                 (PLACEHOLDER_MSG,))
    conn.execute("INSERT INTO learning_anchors (anchor_key, kind, record_id, anchor_query, embedding) "
                 "VALUES ('anti:10', 'anti', 10, 'q', %s::vector)", (VECTOR,))


def _state(conn) -> dict:
    """Every value the apply and restore may touch, byte-for-byte comparable."""
    return {
        "records": conn.execute("SELECT workflow_id, failure_category FROM execution_records "
                                "ORDER BY 1").fetchall(),
        "embeddings": conn.execute("SELECT workflow_id, failure_category, embedding::text AS e "
                                   "FROM execution_embeddings ORDER BY 1").fetchall(),
        "anti": conn.execute("SELECT * FROM anti_patterns ORDER BY id").fetchall(),
        "anchors": conn.execute("SELECT anchor_key, kind, record_id, anchor_query, "
                                "embedding::text AS e, org_id FROM learning_anchors "
                                "ORDER BY 1").fetchall(),
    }


def _main(dsn, argv):
    with patch("src.backend.core.config.settings", MagicMock(DATABASE_URL=dsn)):
        main(argv)


def _stamp(conn) -> str:
    rows = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema() "
                        "AND tablename LIKE 'e5b_backfill_%_labels'").fetchall()
    assert len(rows) == 1
    return rows[0]["tablename"][len("e5b_backfill_"):][:14]


def test_apply_then_restore_returns_exactly_the_pre_apply_state(test_db):
    conn, dsn = test_db
    _seed(conn)
    before = _state(conn)

    _main(dsn, ["--apply"])
    after_apply = _state(conn)
    assert after_apply != before                           # the apply really changed something
    assert [r["failure_category"] for r in after_apply["records"]] == ["C1"]
    assert [a["id"] for a in after_apply["anti"]] == [7]   # placeholder anti-pattern 10 deleted

    _main(dsn, ["--restore", _stamp(conn), "--apply"])

    assert _state(conn) == before


def test_restore_refuses_on_drift_and_writes_nothing(test_db):
    conn, dsn = test_db
    _seed(conn)
    _main(dsn, ["--apply"])
    conn.execute("UPDATE execution_records SET failure_category = 'C2' WHERE workflow_id = 'wf-1'")
    drifted = _state(conn)

    with pytest.raises(SystemExit):
        _main(dsn, ["--restore", _stamp(conn), "--apply"])

    assert _state(conn) == drifted


def test_restore_refuses_a_reinforced_anti_pattern(test_db):
    conn, dsn = test_db
    _seed(conn)
    _main(dsn, ["--apply"])
    conn.execute("UPDATE anti_patterns SET last_seen = '2099-01-01 00:00:00' WHERE id = 7")
    reinforced = _state(conn)

    with pytest.raises(SystemExit):
        _main(dsn, ["--restore", _stamp(conn), "--apply"])

    assert _state(conn) == reinforced

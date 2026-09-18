"""The back-fill re-derives a stored label from its own stored message.

The planners are pure functions; main() runs against a mocked psycopg
connection. Nothing here touches a database.

Referenced by: tools/backfill_failure_categories.py
Depends on: pytest, unittest.mock
"""
from unittest.mock import MagicMock, patch

import pytest

from tools.backfill_failure_categories import (
    main,
    plan_backfill,
    plan_placeholder_deletions,
)

PLACEHOLDER_MSG = (
    "TimeoutError: locator.click: Timeout 10000ms exceeded.\n"
    "Call log:\n  - waiting for locator('//PLACEHOLDER_FOR_x')"
)
NEVER_RESOLVED_MSG = (
    "TimeoutError: locator.click: Timeout 10000ms exceeded.\n"
    "Call log:\n  - waiting for locator('#missing')"
)


def test_placeholder_row_moves_from_d1_to_c1():
    rows = [{
        "table": "execution_records",
        "id": "wf-1",
        "failure_category": "D1",
        "error_message": "TimeoutError: locator.click: waiting for locator('//PLACEHOLDER_FOR_x')",
    }]

    plan = plan_backfill(rows)

    assert plan == [{
        "table": "execution_records",
        "id": "wf-1",
        "old": "D1",
        "new": "C1",
        "specific_type": "placeholder_never_resolved",
    }]


def test_a_row_that_does_not_change_is_omitted():
    rows = [{
        "table": "execution_records",
        "id": "wf-2",
        "failure_category": "C2",
        "error_message": "Error: locator.click: Error: strict mode violation: locator('x') resolved to 2 elements",
    }]

    assert plan_backfill(rows) == []


def test_a_row_with_no_message_is_left_alone():
    rows = [{"table": "execution_records", "id": "wf-3",
             "failure_category": "D1", "error_message": None}]

    assert plan_backfill(rows) == []


def test_a_composite_label_is_never_rederived():
    """A1/A7 come from the query and the code, not from the message."""
    rows = [{"table": "execution_records", "id": "wf-4",
             "failure_category": "A1", "error_message": "'1 == 20' should be true."}]

    assert plan_backfill(rows) == []


def test_a_placeholder_anti_pattern_is_deleted_not_relabelled():
    rows = [{"table": "anti_patterns", "id": "10",
             "failure_category": "D1", "error_message": PLACEHOLDER_MSG}]

    assert plan_backfill(rows) == []
    assert plan_placeholder_deletions(rows) == ["10"]


def test_a_placeholder_execution_record_is_relabelled_not_deleted():
    """execution_records is history: it keeps the row, with the true label."""
    rows = [{"table": "execution_records", "id": "wf-5",
             "failure_category": "D1", "error_message": PLACEHOLDER_MSG}]

    assert plan_placeholder_deletions(rows) == []
    assert [entry["new"] for entry in plan_backfill(rows)] == ["C1"]


# --- main() against a mocked connection ---------------------------------------

EXECUTION_ROWS = [{"id": "wf-1", "failure_category": "D1", "error_message": PLACEHOLDER_MSG}]
ANTI_PATTERN_ROWS = [
    {"id": "7", "failure_category": "D1", "error_message": NEVER_RESOLVED_MSG},
    {"id": "10", "failure_category": "D1", "error_message": PLACEHOLDER_MSG},
]


def _fake_connection(update_rowcount: int = 1, delete_rowcount: int = 1):
    """A psycopg-shaped double: SELECTs return rows, writes report a rowcount."""
    conn = MagicMock()

    def execute(sql, params=None):
        cursor = MagicMock()
        cursor.rowcount = 1
        if sql.startswith("SELECT") and "FROM execution_records" in sql:
            cursor.fetchall.return_value = EXECUTION_ROWS
        elif sql.startswith("SELECT") and "FROM anti_patterns" in sql:
            cursor.fetchall.return_value = ANTI_PATTERN_ROWS
        elif sql.startswith("UPDATE"):
            cursor.rowcount = update_rowcount
        elif sql.startswith("DELETE FROM anti_patterns"):
            cursor.rowcount = delete_rowcount
        return cursor

    conn.execute.side_effect = execute
    return conn


def _params(conn, prefix: str) -> list:
    """The bound parameters of every statement starting with prefix, in order."""
    return [call.args[1] for call in conn.execute.call_args_list
            if call.args[0].startswith(prefix)]


def _run(argv, conn):
    connect = MagicMock()
    connect.return_value.__enter__.return_value = conn
    with patch("psycopg.connect", connect), \
            patch("src.backend.core.config.settings",
                  MagicMock(DATABASE_URL="postgresql://test")):
        main(argv)


def _statements(conn) -> list[str]:
    return [call.args[0] for call in conn.execute.call_args_list]


def test_dry_run_is_read_only_and_writes_nothing(capsys):
    conn = _fake_connection()

    _run([], conn)

    assert conn.read_only is True
    assert [s for s in _statements(conn) if not s.startswith("SELECT")] == []
    conn.commit.assert_not_called()
    out = capsys.readouterr().out
    assert "3 rows read: 2 would be relabelled, 1 placeholder anti-pattern(s) would be deleted" in out


def test_apply_snapshots_everything_before_its_first_write():
    conn = _fake_connection()

    _run(["--apply"], conn)

    statements = _statements(conn)
    first_write = next(i for i, s in enumerate(statements)
                       if s.startswith(("UPDATE", "DELETE")))
    snapshot = [i for i, s in enumerate(statements)
                if s.startswith(("CREATE TABLE e5b_backfill_", "INSERT INTO e5b_backfill_"))]
    assert len([s for s in statements if s.startswith("CREATE TABLE e5b_backfill_")]) == 3
    assert snapshot and max(snapshot) < first_write
    assert conn.read_only is False
    deletes = [s for s in statements if s.startswith("DELETE")]
    assert any("FROM learning_anchors" in s for s in deletes)
    assert any("FROM anti_patterns" in s for s in deletes)
    assert len([s for s in statements if s.startswith("UPDATE execution_records")]) == 1
    assert len([s for s in statements if s.startswith("UPDATE anti_patterns")]) == 1
    assert len([s for s in statements if s.startswith("UPDATE execution_embeddings")]) == 1
    conn.commit.assert_called_once()


def test_apply_binds_the_new_label_and_the_row_id():
    conn = _fake_connection()

    _run(["--apply"], conn)

    assert _params(conn, "UPDATE execution_records") == [("C1", "wf-1")]
    assert _params(conn, "UPDATE anti_patterns") == [("C1", "7")]
    assert _params(conn, "DELETE FROM learning_anchors") == [([10],)]
    assert _params(conn, "DELETE FROM anti_patterns") == [([10],)]


def test_apply_mirrors_each_relabelled_run_onto_its_embedding_row():
    conn = _fake_connection()

    _run(["--apply"], conn)

    assert _params(conn, "UPDATE execution_embeddings") == [("C1", "wf-1")]
    snapshot_mirrors = [call.args[1] for call in conn.execute.call_args_list
                        if call.args[0].startswith("INSERT INTO e5b_backfill_")
                        and "FROM execution_embeddings" in call.args[0]]
    assert snapshot_mirrors == [("C1", "wf-1")]


def test_a_relabel_that_matches_no_row_rolls_the_run_back():
    conn = _fake_connection(update_rowcount=0)

    with pytest.raises(RuntimeError, match="expected 1 row relabelled"):
        _run(["--apply"], conn)

    conn.commit.assert_not_called()


def test_apply_with_nothing_to_change_takes_no_snapshot(capsys):
    conn = _fake_connection()
    conn.execute.side_effect = lambda sql, params=None: MagicMock(
        **{"fetchall.return_value": [], "rowcount": 0})

    _run(["--apply"], conn)

    assert [s for s in _statements(conn) if not s.startswith("SELECT")] == []
    conn.commit.assert_not_called()
    assert "Nothing to apply" in capsys.readouterr().out


def test_an_anti_pattern_delete_that_misses_a_row_rolls_the_run_back():
    conn = _fake_connection(delete_rowcount=0)

    with pytest.raises(RuntimeError, match="expected 1 anti-pattern"):
        _run(["--apply"], conn)

    conn.commit.assert_not_called()


def test_a_composite_anti_pattern_is_never_deleted_from_its_message():
    """A composite row's message is a sentence about the query, not an error."""
    rows = [{"table": "anti_patterns", "id": "11",
             "failure_category": "A1", "error_message": PLACEHOLDER_MSG}]

    assert plan_placeholder_deletions(rows) == []

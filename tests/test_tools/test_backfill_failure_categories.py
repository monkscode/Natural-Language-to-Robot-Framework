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
    parse_last_seen,
    parse_stamp,
    plan_backfill,
    plan_placeholder_deletions,
    plan_restore_refusals,
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


# --- restore: pure planners ---------------------------------------------------

from datetime import datetime, timezone

APPLIED_AT = datetime(2026, 9, 18, 19, 10, 4, tzinfo=timezone.utc)
LABELS = [
    {"source_table": "execution_records", "row_id": "wf-1", "old_category": "D1", "new_category": "C1"},
    {"source_table": "execution_embeddings", "row_id": "wf-1", "old_category": "D1", "new_category": "C1"},
    {"source_table": "anti_patterns", "row_id": "7", "old_category": "D1", "new_category": "C1"},
]


def _untouched(labels=LABELS, last_seen="2026-08-10 09:44:23"):
    """Live rows that still hold exactly what the apply wrote."""
    return {(l["source_table"], l["row_id"]): {
        "row_id": l["row_id"], "failure_category": l["new_category"],
        "last_seen": last_seen if l["source_table"] == "anti_patterns" else None,
    } for l in labels}


def test_parse_stamp_reads_the_utc_second_of_the_apply():
    assert parse_stamp("20260918191004") == APPLIED_AT


@pytest.mark.parametrize("stamp", ["", "2026", "2026091819100", "202609181910045",
                                   "20260918191004; DROP TABLE x", "20261318191004", None])
def test_parse_stamp_refuses_anything_that_is_not_a_real_14_digit_instant(stamp):
    """The stamp becomes part of an SQL identifier: nothing but 14 digits may pass."""
    with pytest.raises(ValueError):
        parse_stamp(stamp)


@pytest.mark.parametrize("text, expected", [
    ("2026-09-18 19:10:04", APPLIED_AT),                      # the datetime() shim's shape, UTC
    ("2026-09-18T19:10:04+00:00", APPLIED_AT),                # a Python isoformat() writer
    ("2026-09-19T00:40:04+05:30", APPLIED_AT),                # same instant, another offset
    ("2026-09-18T19:10:04.250000+00:00", APPLIED_AT.replace(microsecond=250000)),
])
def test_parse_last_seen_reads_both_writers_as_utc(text, expected):
    assert parse_last_seen(text) == expected


@pytest.mark.parametrize("text", ["yesterday", "", "18/09/2026 19:10"])
def test_parse_last_seen_returns_none_for_text_that_is_not_a_timestamp(text):
    assert parse_last_seen(text) is None


def test_nothing_moved_means_no_refusal():
    assert plan_restore_refusals(LABELS, _untouched(), [], APPLIED_AT) == []


def test_a_label_changed_since_the_apply_refuses():
    live = _untouched()
    live[("execution_records", "wf-1")]["failure_category"] = ""   # a passing re-run cleared it
    assert plan_restore_refusals(LABELS, live, [], APPLIED_AT) == [
        "execution_records wf-1: now '', but the apply wrote 'C1'"]


def test_a_row_that_no_longer_exists_refuses():
    live = _untouched()
    del live[("execution_embeddings", "wf-1")]
    assert plan_restore_refusals(LABELS, live, [], APPLIED_AT) == [
        "execution_embeddings wf-1: the row no longer exists"]


def test_an_anti_pattern_reinforced_after_the_apply_refuses():
    live = _untouched(last_seen="2026-09-19 08:00:00")
    assert plan_restore_refusals(LABELS, live, [], APPLIED_AT) == [
        "anti_patterns 7: reinforced at 2026-09-19T08:00:00+00:00, at or after the apply"]


def test_reinforced_in_the_same_second_as_the_apply_refuses():
    """A false refusal costs a look; a false restore writes stale data silently."""
    live = _untouched(last_seen="2026-09-18 19:10:04")
    assert len(plan_restore_refusals(LABELS, live, [], APPLIED_AT)) == 1


def test_an_unreadable_last_seen_refuses_instead_of_guessing():
    live = _untouched(last_seen="not a date")
    assert plan_restore_refusals(LABELS, live, [], APPLIED_AT) == [
        "anti_patterns 7: last_seen 'not a date' is not a timestamp, so reinforcement "
        "cannot be ruled out"]


def test_a_never_reinforced_anti_pattern_does_not_refuse():
    """NULL last_seen: reinforcement always stamps it, so NULL means it never happened."""
    assert plan_restore_refusals(LABELS, _untouched(last_seen=None), [], APPLIED_AT) == []


def test_collisions_are_refusals_too():
    assert plan_restore_refusals(LABELS, _untouched(), ["anti-pattern id 10 is in use again"],
                                 APPLIED_AT) == ["anti-pattern id 10 is in use again"]


# --- restore: the CLI shell, against a mocked connection -----------------------

STAMP = "20260918191004"
CURRENT_TABLE_ORDER = ("execution_records", "execution_embeddings", "anti_patterns")


def _fake_restore_connection(labels=LABELS, live=None, collisions=(), snapshots=(STAMP,),
                             missing=None, update_rowcount=1, inserted=(1, 1), counts=(1, 1)):
    """A psycopg-shaped double for --restore. Nothing here touches a database.

    Every snapshot the double lists returns the same `labels`; `live` (default: the
    rows still hold what the apply wrote) answers the current-row SELECTs.
    """
    live = _untouched(labels) if live is None else live
    inserts = iter(inserted)
    conn = MagicMock()

    def execute(sql, params=None):
        cursor = MagicMock()
        cursor.rowcount = 1
        if sql.startswith("SELECT to_regclass"):
            cursor.fetchone.return_value = {"t": None if params[0] == missing else params[0]}
        elif sql.startswith("SELECT tablename FROM pg_tables"):
            cursor.fetchall.return_value = [
                {"tablename": f"e5b_backfill_{s}_labels"} for s in snapshots]
        elif sql.startswith("SELECT source_table"):
            cursor.fetchall.return_value = labels
        elif sql.startswith("SELECT count(*) AS n FROM e5b_backfill_"):
            cursor.fetchone.return_value = {
                "n": counts[0] if "_anti_patterns" in sql else counts[1]}
        elif sql.startswith(("SELECT workflow_id AS row_id", "SELECT id::text AS row_id")):
            table = next(t for t in CURRENT_TABLE_ORDER if f"FROM {t} " in sql)
            cursor.fetchall.return_value = [
                row for (t, _), row in live.items() if t == table and row["row_id"] in params[0]]
        elif sql.startswith("SELECT a.id::text AS v") and "ON a.id = s.id" in sql:
            cursor.fetchall.return_value = [{"v": v} for v in collisions]
        elif sql.startswith("SELECT a."):
            cursor.fetchall.return_value = []
        elif sql.startswith("UPDATE"):
            cursor.rowcount = update_rowcount
        elif sql.startswith("INSERT INTO anti_patterns") or sql.startswith(
                "INSERT INTO learning_anchors"):
            cursor.rowcount = next(inserts)
        return cursor

    conn.execute.side_effect = execute
    return conn


def test_restore_dry_run_writes_nothing(capsys):
    conn = _fake_restore_connection()

    _run(["--restore", STAMP], conn)

    assert conn.read_only is True
    assert [s for s in _statements(conn) if s.startswith(("UPDATE", "INSERT", "DELETE"))] == []
    assert not any("FOR UPDATE" in s for s in _statements(conn))
    conn.commit.assert_not_called()
    assert "Dry run: nothing was written" in capsys.readouterr().out


def test_restore_apply_locks_then_writes_every_label_and_row_back():
    conn = _fake_restore_connection()

    _run(["--restore", STAMP, "--apply"], conn)

    statements = _statements(conn)
    current_selects = [s for s in statements
                       if s.startswith(("SELECT workflow_id AS row_id", "SELECT id::text AS row_id"))]
    assert current_selects and all(s.endswith("FOR UPDATE") for s in current_selects)
    assert _params(conn, "UPDATE execution_records") == [("D1", "wf-1")]
    assert _params(conn, "UPDATE execution_embeddings") == [("D1", "wf-1")]
    assert _params(conn, "UPDATE anti_patterns") == [("D1", "7")]
    inserts = [s for s in statements if s.startswith("INSERT INTO")]
    assert inserts[0].startswith("INSERT INTO anti_patterns (id, failure_category")
    assert "OVERRIDING SYSTEM VALUE" in inserts[0]
    assert "SELECT *" not in " ".join(inserts)
    assert inserts[1].startswith("INSERT INTO learning_anchors (anchor_key")
    conn.commit.assert_called_once()


def test_restore_refusal_writes_nothing_and_exits_nonzero(capsys):
    live = _untouched()
    live[("execution_records", "wf-1")]["failure_category"] = "C2"
    conn = _fake_restore_connection(live=live)

    with pytest.raises(SystemExit) as exc:
        _run(["--restore", STAMP, "--apply"], conn)

    assert "nothing was written" in str(exc.value)
    assert [s for s in _statements(conn) if s.startswith(("UPDATE", "INSERT", "DELETE"))] == []
    conn.commit.assert_not_called()
    assert "now 'C2', but the apply wrote 'C1'" in capsys.readouterr().out


def test_restore_refuses_a_collision(capsys):
    conn = _fake_restore_connection(collisions=("10",))

    with pytest.raises(SystemExit):
        _run(["--restore", STAMP, "--apply"], conn)

    conn.commit.assert_not_called()
    assert "anti-pattern id 10 is in use again" in capsys.readouterr().out


def test_restore_refuses_while_a_newer_apply_is_still_in_place(capsys):
    """Restores run newest first: the newer snapshot's rows still hold their new labels."""
    conn = _fake_restore_connection(snapshots=(STAMP, "20260921120000"))

    with pytest.raises(SystemExit):
        _run(["--restore", STAMP, "--apply"], conn)

    conn.commit.assert_not_called()
    assert "snapshot 20260921120000 is newer and not restored" in capsys.readouterr().out


def test_restore_refuses_a_missing_snapshot_table():
    conn = _fake_restore_connection(missing=f"e5b_backfill_{STAMP}_anchors")

    with pytest.raises(SystemExit, match="does not exist"):
        _run(["--restore", STAMP, "--apply"], conn)

    conn.commit.assert_not_called()


def test_a_bad_stamp_is_refused_before_any_connection():
    connect = MagicMock()
    with patch("psycopg.connect", connect), pytest.raises(SystemExit):
        main(["--restore", "2026; DROP TABLE anti_patterns"])
    connect.assert_not_called()


def test_an_empty_stamp_is_refused_before_any_connection():
    connect = MagicMock()
    with patch("psycopg.connect", connect), pytest.raises(SystemExit):
        main(["--restore", "", "--apply"])
    connect.assert_not_called()


def test_a_restore_update_that_misses_a_row_rolls_back():
    conn = _fake_restore_connection(update_rowcount=0)

    with pytest.raises(RuntimeError, match="expected 1 row restored"):
        _run(["--restore", STAMP, "--apply"], conn)

    conn.commit.assert_not_called()


def test_a_restore_insert_count_mismatch_rolls_back():
    conn = _fake_restore_connection(inserted=(0, 1))

    with pytest.raises(RuntimeError, match="restored, got 0"):
        _run(["--restore", STAMP, "--apply"], conn)

    conn.commit.assert_not_called()

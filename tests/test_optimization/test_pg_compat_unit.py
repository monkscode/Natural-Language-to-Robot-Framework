"""Unit tests for the SQLite-compatible psycopg adapter (pg_compat).

Every learning-engine statement flows through this adapter, so a translation
or error-mapping bug here corrupts queries or turns handled IntegrityErrors
into 500s for every engine at once. Pure unit tests — the psycopg cursor and
connection are faked, so these run without Postgres.
"""

import sqlite3
from unittest.mock import MagicMock

import psycopg
import pytest

from src.backend.crew_ai.optimization.pg_compat import (
    CompatConnection,
    CompatCursor,
    CompatRow,
    _NoopCursor,
    compat_row,
    translate,
)


# ---------------------------------------------------------------------------
# translate(): SQLite ? -> psycopg %s with string-literal awareness
# ---------------------------------------------------------------------------

class TestTranslate:
    def test_placeholders_become_psycopg_params(self):
        assert translate("SELECT * FROM t WHERE a = ? AND b = ?") == \
            "SELECT * FROM t WHERE a = %s AND b = %s"

    def test_question_mark_inside_string_literal_is_preserved(self):
        assert translate("SELECT * FROM t WHERE url = 'http://x?y=1' AND a = ?") == \
            "SELECT * FROM t WHERE url = 'http://x?y=1' AND a = %s"

    def test_escaped_quote_inside_literal_does_not_end_the_string(self):
        # '' is the SQL escape for a quote INSIDE a string: the ? after it is
        # still inside the literal and must survive untranslated.
        assert translate("SELECT 'it''s a ?' , ?") == "SELECT 'it''s a ?' , %s"

    def test_percent_is_escaped_for_psycopg(self):
        assert translate("SELECT * FROM t WHERE a LIKE '%abc%' AND b = ?") == \
            "SELECT * FROM t WHERE a LIKE '%%abc%%' AND b = %s"


# ---------------------------------------------------------------------------
# CompatRow / compat_row: sqlite3.Row semantics
# ---------------------------------------------------------------------------

def _row(**cols):
    names = list(cols)
    return CompatRow(names, {n: i for i, n in enumerate(names)}, list(cols.values()))


class TestCompatRow:
    def test_index_by_name_and_position(self):
        r = _row(id=7, name="x")
        assert r["id"] == 7 and r[0] == 7
        assert r["name"] == "x" and r[1] == "x"

    def test_get_returns_default_for_missing_column(self):
        r = _row(id=7)
        assert r.get("id") == 7
        assert r.get("missing") is None
        assert r.get("missing", "fallback") == "fallback"

    def test_iterates_values_and_supports_len_contains_dict(self):
        r = _row(id=7, name="x")
        assert list(r) == [7, "x"]  # sqlite3.Row iterates values
        assert len(r) == 2
        assert "name" in r and "missing" not in r
        assert r.keys() == ["id", "name"]
        assert dict(zip(r.keys(), r)) == {"id": 7, "name": "x"}

    def test_factory_handles_cursor_without_description(self):
        cur = MagicMock()
        cur.description = None
        make = compat_row(cur)
        row = make([1])
        assert row.keys() == []
        assert row[0] == 1  # positional access still works


# ---------------------------------------------------------------------------
# _NoopCursor: what PRAGMA / BEGIN IMMEDIATE callers get back
# ---------------------------------------------------------------------------

def test_noop_cursor_yields_nothing_and_supports_with():
    with _NoopCursor() as cur:
        assert cur.fetchone() is None
        assert cur.fetchall() == []
        assert cur.rowcount == 0
        assert cur.lastrowid is None


# ---------------------------------------------------------------------------
# CompatCursor: translation, IntegrityError mapping, passthrough
# ---------------------------------------------------------------------------

class TestCompatCursor:
    def test_pragma_is_a_noop(self):
        real = MagicMock()
        cur = CompatCursor(real)
        assert cur.execute("PRAGMA journal_mode=WAL") is cur
        assert cur.execute("BEGIN IMMEDIATE") is cur
        real.execute.assert_not_called()

    def test_execute_without_params_skips_translation(self):
        real = MagicMock()
        CompatCursor(real).execute("SELECT '%' AS pct")
        real.execute.assert_called_once_with("SELECT '%' AS pct")

    def test_execute_with_params_translates_placeholders(self):
        real = MagicMock()
        CompatCursor(real).execute("SELECT ? WHERE a = ?", (1, 2))
        real.execute.assert_called_once_with("SELECT %s WHERE a = %s", (1, 2))

    def test_integrity_error_maps_to_sqlite(self):
        real = MagicMock()
        real.execute.side_effect = psycopg.errors.IntegrityError("duplicate key")
        with pytest.raises(sqlite3.IntegrityError, match="duplicate key"):
            CompatCursor(real).execute("INSERT INTO t VALUES (?)", (1,))

    def test_executemany_translates_and_maps_integrity_error(self):
        real = MagicMock()
        cur = CompatCursor(real)
        assert cur.executemany("INSERT INTO t VALUES (?)", [(1,), (2,)]) is cur
        real.executemany.assert_called_once_with(
            "INSERT INTO t VALUES (%s)", [(1,), (2,)])
        real.executemany.side_effect = psycopg.errors.IntegrityError("dup")
        with pytest.raises(sqlite3.IntegrityError):
            cur.executemany("INSERT INTO t VALUES (?)", [(1,)])

    def test_fetch_and_metadata_pass_through(self):
        real = MagicMock(rowcount=3, description=("col",))
        real.fetchone.return_value = "one"
        real.fetchall.return_value = ["all"]
        cur = CompatCursor(real)
        assert cur.fetchone() == "one"
        assert cur.fetchall() == ["all"]
        assert cur.rowcount == 3
        assert cur.description == ("col",)

    def test_context_manager_closes_real_cursor_even_if_close_fails(self):
        real = MagicMock()
        with CompatCursor(real) as cur:
            assert isinstance(cur, CompatCursor)
        real.close.assert_called_once()
        real.close.side_effect = RuntimeError("already closed")
        with CompatCursor(real):
            pass  # close failure is swallowed; no exception escapes


# ---------------------------------------------------------------------------
# CompatConnection: sqlite3.Connection surface
# ---------------------------------------------------------------------------

class TestCompatConnection:
    def test_execute_pragma_returns_noop_cursor(self):
        real = MagicMock()
        conn = CompatConnection(real)
        assert isinstance(conn.execute("PRAGMA foo"), _NoopCursor)
        real.cursor.assert_not_called()

    def test_execute_maps_integrity_error(self):
        real = MagicMock()
        real.cursor.return_value.execute.side_effect = \
            psycopg.errors.IntegrityError("unique violation")
        with pytest.raises(sqlite3.IntegrityError):
            CompatConnection(real).execute("INSERT INTO t VALUES (?)", (1,))

    def test_executemany_maps_integrity_error(self):
        real = MagicMock()
        real.cursor.return_value.executemany.side_effect = \
            psycopg.errors.IntegrityError("unique violation")
        with pytest.raises(sqlite3.IntegrityError):
            CompatConnection(real).executemany("INSERT INTO t VALUES (?)", [(1,)])

    def test_cursor_wraps_real_cursor_in_compat(self):
        real = MagicMock()
        cur = CompatConnection(real).cursor()
        assert isinstance(cur, CompatCursor)

    def test_lifecycle_delegates_to_real_connection(self):
        real = MagicMock(closed=False, broken=True)
        conn = CompatConnection(real)
        conn.commit()
        conn.rollback()
        conn.close()
        real.commit.assert_called_once()
        real.rollback.assert_called_once()
        real.close.assert_called_once()
        assert conn.closed is False
        assert conn.broken is True  # writer reconnect check reads this

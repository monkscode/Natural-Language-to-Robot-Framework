"""
SQLite-compatible psycopg adapter for the learning store (Phase 4 migration).

The learning engines and dashboard endpoints run hundreds of SQLite-dialect
statements (`?` placeholders, sqlite3.Row access, `except sqlite3.IntegrityError`).
Rather than hand-edit every one, this thin adapter wraps a psycopg connection so
that SQLite-style SQL executes on PostgreSQL unchanged:

- `?` placeholders -> psycopg `%s`; literal `%` -> `%%` (only when params are
  passed — psycopg leaves `%` alone for param-less queries). String literals are
  respected so a `?` inside quotes is left intact.
- Rows behave like sqlite3.Row: r['col'] AND r[0] AND dict(r) all work.
- `PRAGMA …` and `BEGIN IMMEDIATE` become no-ops (Postgres handles locking
  differently; the single-writer queue already serialises writes).
- psycopg IntegrityError is re-raised as sqlite3.IntegrityError, so existing
  `except sqlite3.IntegrityError` handlers keep working.

NOT translated (ported at the call sites instead): last_insert_rowid, strftime,
INSERT OR IGNORE — Postgres has different syntax for these. (The hint-id array
columns are native jsonb as of slice 4.5, so there is no json_each/json_valid to
translate — the KPI queries use jsonb operators directly.)

Referenced by: postgres_execution_memory.py and the learning engines via the
connection objects it hands out.
"""

import logging
import sqlite3

import psycopg

from src.backend.core.config import PG_CONNECT_TIMEOUT_S

logger = logging.getLogger(__name__)


def translate(sql: str) -> str:
    """SQLite `?` -> psycopg `%s`, escaping literal `%` -> `%%`.

    Only called when params are present. A single-quoted string literal is
    tracked so `?`/`%` inside quotes are handled correctly ('' is the SQL escape
    for a quote inside a string).
    """
    out: list[str] = []
    in_str = False
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            out.append(ch)
            if in_str and i + 1 < n and sql[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            in_str = not in_str
            i += 1
            continue
        if ch == "%":
            out.append("%%")
            i += 1
            continue
        if ch == "?" and not in_str:
            out.append("%s")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


class CompatRow:
    """sqlite3.Row-like row: indexable by column name OR position, dict-able."""

    __slots__ = ("_cols", "_index", "_vals")

    def __init__(self, cols, index, vals):
        self._cols = cols
        self._index = index
        self._vals = vals

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._vals[key]
        return self._vals[self._index[key]]

    def get(self, key, default=None):
        i = self._index.get(key)
        return default if i is None else self._vals[i]

    def keys(self):
        return list(self._cols)

    def __iter__(self):
        return iter(self._vals)  # sqlite3.Row iterates values

    def __len__(self):
        return len(self._vals)

    def __contains__(self, key):
        return key in self._index


def compat_row(cursor):
    """psycopg row factory producing CompatRow objects."""
    desc = cursor.description
    cols = [c.name for c in desc] if desc else []
    index = {name: i for i, name in enumerate(cols)}

    def make(values):
        return CompatRow(cols, index, values)

    return make


def _is_noop(sql: str) -> bool:
    s = sql.lstrip().upper()
    return s.startswith("PRAGMA") or s.startswith("BEGIN")


class _NoopCursor:
    rowcount = 0
    lastrowid = None

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class CompatCursor:
    """Wraps a psycopg cursor: translates SQL, maps IntegrityError, no-ops PRAGMA."""

    def __init__(self, real):
        self._real = real

    def execute(self, sql, params=None):
        if _is_noop(sql):
            return self
        try:
            if params is None:
                self._real.execute(sql)
            else:
                self._real.execute(translate(sql), params)
        except psycopg.errors.IntegrityError as e:
            raise sqlite3.IntegrityError(str(e)) from e
        return self

    def executemany(self, sql, seq):
        try:
            self._real.executemany(translate(sql), list(seq))
        except psycopg.errors.IntegrityError as e:
            raise sqlite3.IntegrityError(str(e)) from e
        return self

    def fetchone(self):
        return self._real.fetchone()

    def fetchall(self):
        return self._real.fetchall()

    @property
    def rowcount(self):
        return self._real.rowcount

    @property
    def description(self):
        return self._real.description

    def __enter__(self):
        return self

    def __exit__(self, *a):
        try:
            self._real.close()
        except Exception:
            pass
        return False


class CompatConnection:
    """sqlite3-Connection-like wrapper over a psycopg connection."""

    def __init__(self, real):
        self._real = real  # psycopg connection (row_factory=compat_row)
        self.row_factory = None  # accept the attribute; ignored (we always use compat rows)

    def execute(self, sql, params=None):
        """conn.execute(...) returns a cursor (sqlite3 semantics)."""
        if _is_noop(sql):
            return _NoopCursor()
        cur = self._real.cursor()
        try:
            if params is None:
                cur.execute(sql)
            else:
                cur.execute(translate(sql), params)
        except psycopg.errors.IntegrityError as e:
            raise sqlite3.IntegrityError(str(e)) from e
        return cur

    def executemany(self, sql, seq):
        cur = self._real.cursor()
        try:
            cur.executemany(translate(sql), list(seq))
        except psycopg.errors.IntegrityError as e:
            raise sqlite3.IntegrityError(str(e)) from e
        return cur

    def cursor(self):
        return CompatCursor(self._real.cursor())

    def commit(self):
        self._real.commit()

    def rollback(self):
        self._real.rollback()

    def close(self):
        self._real.close()

    @property
    def closed(self):
        return self._real.closed

    @property
    def broken(self):
        """True when the underlying connection died mid-use (server restart,
        network drop) — used by the writer's reconnect check."""
        return self._real.broken


def connect(dsn: str, autocommit: bool = False) -> CompatConnection:
    """Open a SQLite-compatible psycopg connection (bounded connect timeout)."""
    real = psycopg.connect(
        dsn, row_factory=compat_row, autocommit=autocommit,
        connect_timeout=PG_CONNECT_TIMEOUT_S,
    )
    return CompatConnection(real)

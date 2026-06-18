"""Postgres catalog introspection helpers for the migrated optimization tests.

The optimization suite now runs against PostgreSQL (Phase 4), so the SQLite-only
schema introspection these tests used (``sqlite_master``, ``PRAGMA table_info``)
no longer works. These helpers return the same information from pg_catalog /
information_schema, scoped to the connection's active schema (the per-test
``learning_test`` schema set via the DSN search_path).

Each takes any connection exposing the sqlite3-style ``.execute(sql, params)``
(i.e. the ``in_memory_db`` compat connection).
"""


def table_names(conn) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
        ).fetchall()
    }


def index_names(conn, table: str | None = None) -> set[str]:
    if table is None:
        return {
            r[0]
            for r in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
            ).fetchall()
        }
    return {
        r[0]
        for r in conn.execute(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = current_schema() AND tablename = ?",
            (table,),
        ).fetchall()
    }


def column_names(conn, table: str) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ?",
            (table,),
        ).fetchall()
    }

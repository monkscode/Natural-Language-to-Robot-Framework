"""Regression tests for the pytest-session Postgres isolation guard.

Proves the defect described in
.superpowers/sdd/2026-08-28-feedback-integrity-remediation/task-0-brief.md
is fixed: the whole pytest session must run against a throwaway
`nlrf_test_<pid>` database, never the live `nlrf` database, so that a
second, concurrently-running session can no longer collide with (or corrupt)
the first session's tables via the shared `public` schema.

Referenced by: none (leaf test module).
Depends on: tests.conftest (_BASE_DATABASE_URL, sweep_stale_test_databases,
_with_dbname helpers installed by the DB isolation guard).
"""

import os
import logging
from urllib.parse import urlsplit

import psycopg
import pytest

logger = logging.getLogger(__name__)


def _postgres_reachable() -> bool:
    """Best-effort connectivity probe used to skip this whole module when
    Postgres is unavailable, mirroring tests/test_auth/conftest.py's
    pytest.skip-on-connect-failure pattern."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return False
    try:
        conn = psycopg.connect(url, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001 — any connect failure means skip
        logger.info("tests/test_infra: Postgres unreachable, skipping module: %s", exc)
        return False
    conn.close()
    return True


pytestmark = pytest.mark.skipif(not _postgres_reachable(), reason="Postgres unavailable")


def test_session_database_url_is_a_throwaway_database():
    """This is the regression test: it fails on today's code, which runs the
    suite directly against the live `nlrf` database."""
    from tests.conftest import _BASE_DATABASE_URL

    session_url = os.environ.get("DATABASE_URL", "")
    session_db = urlsplit(session_url).path.lstrip("/")
    base_db = urlsplit(_BASE_DATABASE_URL).path.lstrip("/")

    assert session_db.startswith("nlrf_test_"), (
        f"expected the pytest session's DATABASE_URL to name a throwaway "
        f"nlrf_test_<pid> database, got {session_db!r} (DATABASE_URL={session_url!r})"
    )
    assert session_db != base_db, (
        f"session database {session_db!r} must not be the base/live database {base_db!r}"
    )


def test_public_schema_has_no_execution_records_or_is_empty():
    """The database the suite is connected to must be a fresh throwaway: it
    either has no public.execution_records table at all, or one with zero
    rows (weaker assertion so this does not depend on which fixtures, if
    any, already ran and created the table in `public`)."""
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS ("
                "  SELECT 1 FROM information_schema.tables"
                "  WHERE table_schema = 'public' AND table_name = 'execution_records'"
                ")"
            )
            (exists,) = cur.fetchone()
            if not exists:
                return
            cur.execute("SELECT count(*) FROM public.execution_records")
            (row_count,) = cur.fetchone()
        assert row_count == 0, (
            f"public.execution_records has {row_count} rows — this is not a "
            "fresh throwaway database"
        )
    finally:
        conn.close()


def test_stale_sweep_does_not_drop_a_database_with_active_backends():
    """The stale-database sweep must skip any nlrf_test_% database that
    currently has open backends, even though it matches the sweep's name
    pattern — only a database with zero backends is eligible for the sweep.

    Exercises the helper directly against a database created (and held open)
    by this test, named distinctly from any real session's own
    nlrf_test_<pid> database so the two cannot collide.
    """
    from tests.conftest import _BASE_DATABASE_URL, _with_dbname, sweep_stale_test_databases

    admin_dsn = _with_dbname(_BASE_DATABASE_URL, "postgres")
    guard_db = f"nlrf_test_guard_{os.getpid()}"

    admin = psycopg.connect(admin_dsn, autocommit=True)
    admin.execute(f'DROP DATABASE IF EXISTS "{guard_db}"')
    admin.execute(f'CREATE DATABASE "{guard_db}"')

    held_conn = None
    try:
        held_dsn = _with_dbname(_BASE_DATABASE_URL, guard_db)
        held_conn = psycopg.connect(held_dsn)  # keeps an active backend open

        sweep_stale_test_databases(admin_dsn)

        with admin.cursor() as cur:
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)",
                (guard_db,),
            )
            (still_exists,) = cur.fetchone()
        assert still_exists, (
            "sweep_stale_test_databases dropped a database with an active "
            "backend — it must only drop databases with zero backends"
        )
    finally:
        if held_conn is not None:
            held_conn.close()
        admin.execute(f'DROP DATABASE IF EXISTS "{guard_db}"')
        admin.close()

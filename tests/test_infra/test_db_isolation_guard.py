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
from unittest.mock import patch
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

    Also asserts the session's OWN throwaway database survives this same
    sweep call — this is the exact call shape the task-0 review used to
    reproduce the Critical defect: this test's guard_db-specific assertion
    passed even while the sweep silently destroyed the calling session's own
    database in the same call, because nothing here was checking for that.
    """
    from tests.conftest import (
        _BASE_DATABASE_URL, _throwaway_db_name, _with_dbname, sweep_stale_test_databases,
    )

    own_db = _throwaway_db_name
    assert own_db is not None, "expected an active session throwaway database"

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

        with admin.cursor() as cur:
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)",
                (own_db,),
            )
            (own_db_still_exists,) = cur.fetchone()
        assert own_db_still_exists, (
            "sweep_stale_test_databases dropped the session's own throwaway "
            "database while handling this same call — this is the exact "
            "Critical defect reproduced live in the task-0 review"
        )
    finally:
        if held_conn is not None:
            held_conn.close()
        admin.execute(f'DROP DATABASE IF EXISTS "{guard_db}"')
        admin.close()


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "YES", " 1 "])
def test_truthy_env_recognizes_truthy_spellings(value):
    """_truthy_env is the single source of truth LIVE_DB_OPT_OUT is computed
    from at module-import time — test the spelling rules directly rather
    than only indirectly through an env var + reimport."""
    from tests.conftest import _truthy_env

    assert _truthy_env(value) is True


@pytest.mark.parametrize("value", ["0", "false", "no", "", "banana", None])
def test_truthy_env_rejects_everything_else(value):
    from tests.conftest import _truthy_env

    assert _truthy_env(value) is False


def test_apply_database_url_override_opted_out_skips_the_redirect(monkeypatch):
    """opted_out=True must skip the throwaway-database redirect entirely —
    _create_throwaway_database must never be called, and DATABASE_URL must
    be left exactly as it was."""
    from tests import conftest as c

    sentinel_dsn = "postgresql://sentinel:sentinel@localhost:5432/sentinel_untouched"
    monkeypatch.setenv("DATABASE_URL", sentinel_dsn)

    with patch.object(c, "_create_throwaway_database") as mock_create:
        result = c._apply_database_url_override(sentinel_dsn, opted_out=True)

    assert result is False
    mock_create.assert_not_called()
    assert os.environ["DATABASE_URL"] == sentinel_dsn


def test_apply_database_url_override_not_opted_out_runs_the_redirect_logic():
    """opted_out=False must still run the redirect logic (i.e.
    _create_throwaway_database gets called). Mocked to return None so this
    doesn't touch a real database; only the dispatch decision is under
    test here."""
    from tests import conftest as c

    with patch.object(c, "_create_throwaway_database", return_value=None) as mock_create:
        result = c._apply_database_url_override(
            "postgresql://x:x@localhost:5432/x", opted_out=False
        )

    assert result is False  # the mock simulates throwaway-creation failure
    mock_create.assert_called_once()


def test_keeper_connection_is_open_for_the_sessions_own_database():
    """Critical-fix requirement 1: a long-lived connection to the session's
    own database must exist for the life of the session — this is what
    makes "zero backends" a sound staleness signal for every OTHER database
    the sweep considers. Sanity-checks the module state directly (existence
    and liveness of the connection) rather than re-deriving the
    pg_stat_activity math already covered by the sweep tests below."""
    from tests import conftest as c

    assert c._keeper_conn is not None, (
        "expected a keeper connection to the session's own throwaway database"
    )
    with c._keeper_conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)


def test_sweep_never_drops_the_sessions_own_database_even_when_idle():
    """Critical-fix requirement 2: the sweep must exclude the session's own
    current database by name, unconditionally — that exclusion must hold on
    its own, not only because the keeper connection happens to keep the
    backend count above zero, because this function is reachable directly
    from a test (as here) and must be safe regardless.

    This reproduces, exactly, the scenario the task-0 review used to prove
    the Critical defect live: calling sweep_stale_test_databases() from
    inside a test, against the session's own database, and finding it gone
    afterward.
    """
    from tests import conftest as c

    own_db = c._throwaway_db_name
    assert own_db is not None, "expected an active session throwaway database"

    admin_dsn = c._with_dbname(c._BASE_DATABASE_URL, "postgres")
    c.sweep_stale_test_databases(admin_dsn)

    admin = psycopg.connect(admin_dsn, autocommit=True)
    try:
        with admin.cursor() as cur:
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)",
                (own_db,),
            )
            (still_exists,) = cur.fetchone()
        assert still_exists, (
            "sweep_stale_test_databases dropped the session's own throwaway "
            "database — this is the exact Critical defect reproduced live "
            "in the task-0 review"
        )
    finally:
        admin.close()


def test_tier2_live_db_tests_skip_is_driven_by_opt_out_not_guard_success():
    """Fail-safe requirement: whether the Tier-2 live-workflow tests
    (tests/test_integration/test_live_workflow.py::TestWorkflowApiEndpoints)
    run must be driven ONLY by whether the operator explicitly opted out via
    NLRF_TEST_LIVE_DB, never by whether the throwaway-database guard itself
    happened to succeed.

    _tier2_live_db_tests_should_skip is a pure function of exactly one
    boolean (the opt-out signal) — guard success/failure isn't even a
    parameter it can see — so this proves the wiring by construction rather
    than by trying to reproduce a guard failure (Postgres up but role lacks
    CREATEDB, or similar) against the real server.
    """
    from tests.test_integration.test_live_workflow import _tier2_live_db_tests_should_skip

    # Explicitly opted in (NLRF_TEST_LIVE_DB truthy) -> must run, full stop.
    assert _tier2_live_db_tests_should_skip(live_db_opt_out=True) is False
    # Not opted in -> must skip, REGARDLESS of whether the throwaway-database
    # guard succeeded or failed. This is the fail-safe property itself: a
    # guard failure (e.g. Postgres up but CREATEDB denied) must never be
    # interpreted as permission to write to whatever database is live.
    assert _tier2_live_db_tests_should_skip(live_db_opt_out=False) is True

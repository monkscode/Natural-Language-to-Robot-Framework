"""
Shared pytest fixtures for NL repo test suite.

Provides:
- mock_settings: Patched Settings with safe test defaults
- sample_robot_code: Reusable RF code snippet
- tmp_metrics_dir: Temporary directory for metrics files

Also installs a whole-session Postgres isolation guard (see the module-level
block below `_BASE_DATABASE_URL = _base_database_url()`): before anything
imports src.backend, it points DATABASE_URL at a throwaway
`nlrf_test_<pid>_<random>` database so the ~26 hard-coded-schema DB fixtures
across the suite (which all build their DSNs from settings.DATABASE_URL) can
never collide with, or
silently fall through to, the live `nlrf` database. `DB_ISOLATION_ACTIVE`
(module-level bool) tells importers whether the redirect actually happened;
it is False when NLRF_TEST_LIVE_DB opted out, or when throwaway-database
creation itself failed — so it must never be read as "safe to touch live
data" (a guard failure is not an opt-out). `LIVE_DB_OPT_OUT` (module-level
bool) is the one true signal for that: True only when NLRF_TEST_LIVE_DB was
explicitly truthy, independent of whether the guard itself succeeded.
"""

import logging
import os
import secrets
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

# Tracing OFF for the whole test process, set before anything imports
# src.backend so Settings reads it instead of .env's OBSERVABILITY_BACKEND.
#
# main.py calls init_observability() at IMPORT time, so the first test that
# imports the app installs a global OTel provider for the rest of the run. With
# the .env value (postgres) that provider's PostgresSpanExporter points at the
# live DATABASE_URL, and every create_workflow_span() afterwards exports for
# real — including from unit tests that only meant to exercise the context
# manager. Measured on the gate suite: 6 rows per run into public.llm_traces,
# against the standing rule that tests never write to the live database.
#
# Pinned here rather than in a fixture because the damage is done at import
# time, which is before any fixture runs.
os.environ.setdefault("OBSERVABILITY_BACKEND", "none")


# ---------------------------------------------------------------------------
# Whole-session Postgres isolation guard
# ---------------------------------------------------------------------------
#
# The defect (measured, see
# .superpowers/sdd/2026-08-28-feedback-integrity-remediation/task-0-brief.md):
# every Postgres-backed fixture in this suite isolates itself with a
# hard-coded schema name (learning_api_test, auth_test, ...) inside the ONE
# shared `nlrf` database, and several of those DSNs keep `public` on the
# search_path as a fallback so the pgvector `vector` type resolves. When a
# second pytest session starts while a first is still running, the second
# session's `DROP SCHEMA ... CASCADE; CREATE SCHEMA ...` deletes the first
# session's tables out from under it; the first session's unqualified reads
# and writes then silently fall through to `public` — the owner's live
# tables. This has already put real rows into the live database once.
#
# Fix: point the whole pytest session at a per-session throwaway database
# (`nlrf_test_<pid>_<random>`) instead of touching any of those 26 schema
# constants.
# With a throwaway database, `public` starts empty, so any future
# fall-through raises UndefinedTable loudly instead of corrupting data — and
# two concurrent sessions can no longer share a namespace at all, because
# Postgres databases (unlike schemas) are hard boundaries: neither session's
# DROP SCHEMA / CREATE SCHEMA / unqualified read-or-write can even see the
# other session's database.
#
# This block runs at IMPORT time of this file, exactly like the
# OBSERVABILITY_BACKEND pin above and for the same reason: it must complete
# before the first `from src.backend.core.config import settings` anywhere in
# the suite, because that import instantiates the module-level `settings`
# singleton exactly once per interpreter session. If that happened before we
# overrode DATABASE_URL, every subsequent `from src.backend.core.config
# import settings` in every test file would keep resolving to the
# pre-override (live) URL for the rest of the run — reassigning
# os.environ["DATABASE_URL"] later would have no further effect. For the same
# reason, the helpers below resolve the base DSN by reading os.environ / the
# .env file directly and never import src.backend.core.config.
#
# Opt-out: NLRF_TEST_LIVE_DB=1 (or true/yes, case-insensitive) skips the
# throwaway-database redirect entirely and leaves DATABASE_URL exactly as
# configured — no database is created or swept in that mode. Its one
# intended use is the Tier-2 "live workflow" tests
# (tests/test_integration/test_live_workflow.py), which authenticate against
# an already-running, separately-started backend process on :5000 by writing
# a real users row through settings.DATABASE_URL — that only works if pytest
# and that backend process are pointed at the same database, and the
# already-running process cannot be redirected from here. Using it means
# accepting that writes can reach whatever database is live, live nlrf
# included — it is opt-in for exactly that reason.

_ADMIN_CONNECT_TIMEOUT_S = 5
_TEST_DB_PREFIX = "nlrf_test_"
_LIVE_DB_OPT_OUT_VAR = "NLRF_TEST_LIVE_DB"
_TRUTHY_ENV_VALUES = {"1", "true", "yes"}

# Populated by _create_throwaway_database() on success; used by
# pytest_sessionfinish to drop the throwaway database again. None means no
# throwaway database was created (Postgres unreachable, role lacks CREATEDB,
# or the vector extension could not be enabled) — DATABASE_URL was left
# untouched and there is nothing to tear down.
_throwaway_db_name: str | None = None
_throwaway_admin_dsn: str | None = None

# One long-lived connection to the session's own throwaway database, opened
# once in _create_throwaway_database() and held for the life of the session.
# Its only job is to make "zero backends" a sound staleness signal in
# sweep_stale_test_databases: without it, this session's own database also
# sits at zero backends between fixtures (every connection this file opens
# elsewhere is closed immediately after use), so a sweep call made while
# idle would see it as indistinguishable from an actually-stale leftover.
# Closed in pytest_sessionfinish before the DROP DATABASE.
_keeper_conn = None


def _truthy_env(value: str | None) -> bool:
    """True for "1"/"true"/"yes", case-insensitive; False for anything else,
    including None/empty/unset."""
    return (value or "").strip().lower() in _TRUTHY_ENV_VALUES


def _repo_backend_env_path() -> Path:
    return Path(__file__).resolve().parent.parent / "src" / "backend" / ".env"


def _parse_dotenv_database_url(path: Path) -> str | None:
    """Read the DATABASE_URL= value out of an .env file by hand.

    Deliberately does not use python-dotenv's own loader here: that loader
    (as used by src/backend/core/config.py) writes into os.environ as a side
    effect, and this function must run before we have decided what
    DATABASE_URL should be for the session.
    """
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() != "DATABASE_URL":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value
    return None


def _base_database_url() -> str:
    """DSN the suite would otherwise connect with — resolved WITHOUT
    importing src.backend.core.config. Priority: an already-set
    DATABASE_URL env var (so an operator can still aim the suite at another
    server), else the DATABASE_URL line out of src/backend/.env, else the
    same default config.py uses."""
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        return env_url
    dotenv_url = _parse_dotenv_database_url(_repo_backend_env_path())
    if dotenv_url:
        return dotenv_url
    return "postgresql://nlrf:nlrf@localhost:5432/nlrf"


def _with_dbname(dsn: str, dbname: str) -> str:
    """Return `dsn` with its path (database name) replaced by `dbname`,
    leaving host/port/user/password/query/fragment untouched."""
    parts = urlsplit(dsn)
    return urlunsplit((parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment))


def sweep_stale_test_databases(admin_dsn: str) -> None:
    """Drop leftover nlrf_test_% databases left behind by crashed sessions
    (a session killed before pytest_sessionfinish never reaches its own
    teardown, so its throwaway database is only ever cleaned up here, by a
    later session's startup).

    Only drops a database with zero current backends in pg_stat_activity,
    and uses a plain DROP DATABASE (never FORCE) — so a race against a live
    concurrent session's own in-use throwaway database is simply refused by
    Postgres (a database with active backends can't be dropped without
    FORCE) instead of killing that session. Best-effort: every failure
    (import, connect, list, per-database drop) is swallowed, since this is a
    courtesy cleanup, not the primary teardown path — it must never fail
    collection.

    "Zero backends" alone is NOT a sound staleness test: it is also
    momentarily true of a live session's own throwaway database between
    fixtures, and of THIS session's own database before its keeper
    connection (see _keeper_conn) exists. So this function unconditionally
    excludes _throwaway_db_name — the calling session's own current database,
    if it has one — from the candidate list, regardless of backend count.
    That exclusion must hold on its own, independent of whether a keeper
    connection happens to be open, because this function is reachable
    directly from a test.
    """
    try:
        import psycopg
    except Exception as exc:  # noqa: BLE001 — never fail collection over a broken psycopg import
        logger.info("DB isolation: stale-database sweep skipped, psycopg unavailable: %s", exc)
        return

    try:
        conn = psycopg.connect(
            admin_dsn, autocommit=True, connect_timeout=_ADMIN_CONNECT_TIMEOUT_S
        )
    except Exception as exc:  # noqa: BLE001 — best-effort sweep, never fail collection
        logger.info("DB isolation: stale-database sweep skipped, cannot connect: %s", exc)
        return
    try:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT datname FROM pg_database WHERE datname LIKE %s",
                    (f"{_TEST_DB_PREFIX}%",),
                )
                stale_candidates = [row[0] for row in cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            logger.info("DB isolation: stale-database sweep could not list databases: %s", exc)
            return
        for datname in stale_candidates:
            if datname == _throwaway_db_name:
                # Never the calling session's own database, no matter its
                # backend count — see the docstring above.
                continue
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = %s",
                        (datname,),
                    )
                    (backend_count,) = cur.fetchone()
                if backend_count == 0:
                    conn.execute(
                        psycopg.sql.SQL("DROP DATABASE {}").format(
                            psycopg.sql.Identifier(datname)
                        )
                    )
            except Exception as exc:  # noqa: BLE001 — one bad candidate must not stop the rest
                logger.info("DB isolation: stale-database sweep skipped %s: %s", datname, exc)
                continue
    finally:
        conn.close()


def _create_throwaway_database(base_dsn: str) -> str | None:
    """Create a fresh nlrf_test_<pid>_<random> database with pgvector
    enabled and return its DSN, or None on any failure. Callers must
    degrade gracefully — Postgres unreachable, the role lacking CREATEDB,
    the vector extension being unavailable, and psycopg itself failing to
    import are all expected, non-fatal outcomes (the existing DB-backed
    fixtures already pytest.skip when Postgres is unavailable; that must
    keep working)."""
    try:
        import psycopg
    except Exception as exc:  # noqa: BLE001 — never fail collection over a broken psycopg import
        logger.warning(
            "DB isolation: psycopg unavailable, tests will use DATABASE_URL as "
            "configured: %s", exc,
        )
        return None

    admin_dsn = _with_dbname(base_dsn, "postgres")
    # os.getpid() alone is only unique within one OS/PID-namespace instance —
    # two sessions started in separate containers on the same Postgres server
    # could compute the same PID. The random suffix makes the full name
    # unique regardless; the nlrf_test_ prefix is kept so the sweep above
    # still matches it.
    dbname = f"{_TEST_DB_PREFIX}{os.getpid()}_{secrets.token_hex(4)}"

    # Best-effort courtesy cleanup of earlier crashed sessions before we
    # create our own — never lets a sweep failure block database creation.
    sweep_stale_test_databases(admin_dsn)

    try:
        admin = psycopg.connect(
            admin_dsn, autocommit=True, connect_timeout=_ADMIN_CONNECT_TIMEOUT_S
        )
    except Exception as exc:  # noqa: BLE001 — graceful degrade, never fail collection
        logger.warning(
            "DB isolation: Postgres unreachable at %s, tests will use DATABASE_URL as "
            "configured; DB-backed fixtures will pytest.skip: %s",
            admin_dsn, exc,
        )
        return None

    try:
        try:
            # Defensive pre-clean: the random suffix already makes an exact
            # name collision with a leftover vanishingly unlikely, but this
            # is a harmless no-op in the normal case and a safety net if one
            # ever did happen (e.g. a reused PID and a repeated suffix).
            admin.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
            admin.execute(f'CREATE DATABASE "{dbname}"')
        except Exception as exc:  # noqa: BLE001 — e.g. role lacks CREATEDB
            logger.warning(
                "DB isolation: could not create throwaway database %s, tests will use "
                "DATABASE_URL as configured: %s", dbname, exc,
            )
            return None

        target_dsn = _with_dbname(base_dsn, dbname)
        try:
            target = psycopg.connect(
                target_dsn, autocommit=True, connect_timeout=_ADMIN_CONNECT_TIMEOUT_S
            )
            try:
                target.execute("CREATE EXTENSION IF NOT EXISTS vector")
            finally:
                target.close()
        except Exception as exc:  # noqa: BLE001 — e.g. vector extension unavailable
            logger.warning(
                "DB isolation: could not enable pgvector in %s, dropping it and using "
                "DATABASE_URL as configured: %s", dbname, exc,
            )
            try:
                admin.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
            except Exception:  # noqa: BLE001 — best-effort cleanup of our own failed attempt
                pass
            return None

        # Open and hold one connection to our own database for the life of
        # the session (see _keeper_conn). Without this, sweep_stale_test_
        # databases' "zero backends" check cannot distinguish this session's
        # own idle database from an actually-stale leftover — a real,
        # reproduced defect this keeper connection exists to close. If we
        # can't get one, treat it exactly like any other guard failure:
        # drop what we just created and degrade rather than proceed with an
        # unsound invariant.
        try:
            keeper = psycopg.connect(
                target_dsn, autocommit=True, connect_timeout=_ADMIN_CONNECT_TIMEOUT_S
            )
        except Exception as exc:  # noqa: BLE001 — graceful degrade, never fail collection
            logger.warning(
                "DB isolation: could not open a keeper connection to %s, dropping it and "
                "using DATABASE_URL as configured: %s", dbname, exc,
            )
            try:
                admin.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
            except Exception:  # noqa: BLE001 — best-effort cleanup of our own failed attempt
                pass
            return None

        global _throwaway_db_name, _throwaway_admin_dsn, _keeper_conn
        _throwaway_db_name = dbname
        _throwaway_admin_dsn = admin_dsn
        _keeper_conn = keeper
        return target_dsn
    finally:
        admin.close()


def _apply_database_url_override(base_dsn: str, opted_out: bool) -> bool:
    """Decide between the NLRF_TEST_LIVE_DB opt-out and the throwaway-database
    redirect, and apply the result to os.environ. Returns True iff the
    redirect was actually applied (DATABASE_URL now names a throwaway
    database) — False for the opt-out, and False if throwaway-database
    creation itself failed (Postgres unreachable, no CREATEDB, no vector
    extension), in which case DATABASE_URL is left untouched either way.

    `opted_out` is passed in rather than re-read from os.environ here so
    LIVE_DB_OPT_OUT (computed once, below) is the single source of truth —
    tests, and the Tier-2 live-workflow tests' own skip condition, must key
    off that same value rather than risk drifting from what this function
    actually used.

    Split out from the module-level call below so tests can exercise this
    decision directly (e.g. with opted_out=True and _create_throwaway_database
    patched to prove it's never invoked) without re-importing this module or
    touching the real throwaway database this session already created.
    """
    if opted_out:
        logger.warning(
            "DB isolation: %s is set — using the configured DATABASE_URL as-is "
            "for this session; writes can reach whatever database that points "
            "at, live nlrf included. Intended only for the Tier-2 live-workflow "
            "tests (tests/test_integration/test_live_workflow.py).",
            _LIVE_DB_OPT_OUT_VAR,
        )
        return False
    dsn = _create_throwaway_database(base_dsn)
    if dsn is None:
        return False
    os.environ["DATABASE_URL"] = dsn
    return True


_BASE_DATABASE_URL = _base_database_url()

# Whether NLRF_TEST_LIVE_DB was explicitly truthy for this session — the
# single source of truth for "the operator opted this session out of the
# isolation guard on purpose." Computed once here, independent of whether
# the throwaway-database guard itself succeeds or fails, so that anything
# gating on "is it safe to write to whatever DATABASE_URL resolves to"
# (e.g. the Tier-2 live-workflow tests) can key off this value directly
# instead of DB_ISOLATION_ACTIVE, which conflates "opted out" with "guard
# failed" and must never be read as permission to touch live data.
LIVE_DB_OPT_OUT = _truthy_env(os.environ.get(_LIVE_DB_OPT_OUT_VAR))

DB_ISOLATION_ACTIVE = _apply_database_url_override(_BASE_DATABASE_URL, LIVE_DB_OPT_OUT)


def pytest_sessionfinish(session, exitstatus):
    """Drop this session's throwaway database.

    Closes the keeper connection first (see _keeper_conn) — otherwise it
    would itself be one of the "pooled connections outliving the fixtures"
    below, and there is no reason to make FORCE terminate a connection we
    opened and control ourselves.

    Pooled connections (psycopg_pool.ConnectionPool, used by several
    fixtures) often outlive the fixtures that opened them, so a plain DROP
    DATABASE would fail here — use WITH (FORCE) (Postgres 13+; the server is
    pgvector/pgvector:pg16) to terminate any stragglers and drop anyway. If
    this session's process is killed before reaching this hook, the database
    is simply left behind for the next session's startup sweep to collect.
    """
    if _throwaway_db_name is None:
        return

    global _keeper_conn
    if _keeper_conn is not None:
        try:
            _keeper_conn.close()
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass
        _keeper_conn = None

    try:
        import psycopg
    except Exception as exc:  # noqa: BLE001 — never fail teardown over a broken psycopg import
        logger.warning(
            "DB isolation: psycopg unavailable, could not drop throwaway database %s: %s",
            _throwaway_db_name, exc,
        )
        return

    try:
        admin = psycopg.connect(
            _throwaway_admin_dsn, autocommit=True, connect_timeout=_ADMIN_CONNECT_TIMEOUT_S
        )
    except Exception as exc:  # noqa: BLE001 — best-effort teardown
        logger.warning(
            "DB isolation: could not connect to drop throwaway database %s: %s",
            _throwaway_db_name, exc,
        )
        return
    try:
        admin.execute(f'DROP DATABASE IF EXISTS "{_throwaway_db_name}" WITH (FORCE)')
    except Exception as exc:  # noqa: BLE001 — best-effort teardown
        logger.warning(
            "DB isolation: could not drop throwaway database %s: %s",
            _throwaway_db_name, exc,
        )
    finally:
        admin.close()


@pytest.fixture(autouse=True)
def _disable_auth_rate_limit():
    """Disable the auth rate limiter by default so suites that hammer /login or
    /register aren't throttled. The dedicated rate-limit tests re-enable it."""
    try:
        from src.backend.auth.rate_limit import limiter
    except Exception:
        yield
        return
    saved = limiter.enabled
    limiter.enabled = False
    try:
        yield
    finally:
        limiter.enabled = saved


@pytest.fixture
def mock_settings():
    """Patched Settings with test defaults — no real env vars read."""
    with patch.dict(os.environ, {
        "MODEL_PROVIDER": "gemini",
        "GEMINI_API_KEY": "test-key-123",
        "ONLINE_MODEL": "gemini-2.5-flash",
        "LOCAL_MODEL": "llama3",
        "APP_PORT": "5000",
        "BROWSER_USE_SERVICE_URL": "http://localhost:4999",
        "BROWSER_HEADLESS": "true",
        "ROBOT_LIBRARY": "browser",
        "MAX_AGENT_ITERATIONS": "3",
        "ENABLE_CUSTOM_ACTIONS": "true",
        "OPTIMIZATION_ENABLED": "false",
    }, clear=False):
        from src.backend.core.config import Settings
        yield Settings()


@pytest.fixture
def sample_robot_code():
    """Reusable RF test code snippet."""
    return """*** Settings ***
Library    Browser

*** Test Cases ***
Search For Shoes
    New Browser    chromium    headless=true
    New Page       https://example.com
    Fill Text      id=search-box    shoes
    Click          id=submit-btn
    Get Text       id=results    contains    shoes
"""


@pytest.fixture
def tmp_metrics_dir(tmp_path):
    """Temporary directory for metrics file storage tests."""
    metrics_dir = tmp_path / "metrics"
    metrics_dir.mkdir()
    return metrics_dir

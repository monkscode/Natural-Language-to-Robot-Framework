"""Isolated-schema fixture for the auth integration tests.

CLAUDE.md rule: tests must not write to the live database. The auth suites
used to insert real rows into public.users (with best-effort cleanup); this
conftest reroutes them to a dedicated `auth_test` schema instead, mirroring
the isolation pattern of tests/test_optimization/conftest.py.

Isolation works by injecting a pool into auth_db._pool (get_pool() returns an
existing pool as-is), NOT by patching settings.DATABASE_URL — a session-wide
settings patch would leak into suites that run afterwards and build their own
DSNs from it.
"""

import pytest

_AUTH_PG_SCHEMA = "auth_test"


# NOT autouse: test_guards.py / test_jwt_password_roles.py are DB-free unit
# tests and must keep running when Postgres is down. The integration modules
# opt in via pytest.mark.usefixtures("auth_isolated_schema").
@pytest.fixture(scope="package")
def auth_isolated_schema():
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from src.backend.auth import db as auth_db
    from src.backend.core.config import PG_CONNECT_TIMEOUT_S, settings

    try:
        admin = psycopg.connect(
            settings.DATABASE_URL, autocommit=True,
            connect_timeout=PG_CONNECT_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 — connect failure = skip, not fail
        pytest.skip(f"Postgres unavailable: {exc}")

    admin.execute(f"DROP SCHEMA IF EXISTS {_AUTH_PG_SCHEMA} CASCADE")
    admin.execute(f"CREATE SCHEMA {_AUTH_PG_SCHEMA}")
    dsn = settings.DATABASE_URL + f"?options=-c%20search_path%3D{_AUTH_PG_SCHEMA},public"

    saved_pool = auth_db._pool
    pool = ConnectionPool(
        conninfo=dsn, min_size=1, max_size=10,
        kwargs={"row_factory": dict_row, "connect_timeout": PG_CONNECT_TIMEOUT_S},
        open=True,
    )
    auth_db._pool = pool  # get_pool() now hands out the isolated pool
    try:
        auth_db.init_auth_db()  # creates users in auth_test (first on search_path)
        yield
    finally:
        try:
            pool.close()
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass
        auth_db._pool = saved_pool
        try:
            admin.execute(f"DROP SCHEMA IF EXISTS {_AUTH_PG_SCHEMA} CASCADE")
        finally:
            admin.close()

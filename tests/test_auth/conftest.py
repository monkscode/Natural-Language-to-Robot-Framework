"""Isolated-schema fixture for the auth integration tests.

CLAUDE.md rule: tests must not write to the live database. The auth suites
used to insert real rows into public.users (with best-effort cleanup); this
conftest reroutes them to a dedicated `auth_test` schema instead, mirroring
the isolation pattern of tests/test_optimization/conftest.py.

Isolation works by injecting a pool into auth_db._pool (get_pool() returns an
existing pool as-is), NOT by patching settings.DATABASE_URL — a session-wide
settings patch would leak into suites that run afterwards and build their own
DSNs from it.

Data-plane isolation (task 16): the three app singletons (run_registry,
trace_store, workflow_metrics) are also rerouted to auth_test so that both
direct-construction (test_core) and TestClient(app) (test_api) paths write
test_runs/llm_traces/workflow_metrics into auth_test, never public.
Mechanism (a): search_path is set to auth_test ONLY (no ,public fallback),
so CREATE TABLE IF NOT EXISTS cannot see the public copies and creates fresh
isolated copies in auth_test instead. The data tables use no vector/extension
types so dropping public from the path is safe for these suites. The learning
suite uses a separate conftest with its own search_path and is unaffected.
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
    import src.backend.core.run_registry as _rr_mod
    import src.backend.core.trace_store as _ts_mod
    import src.backend.core.workflow_metrics as _wm_mod

    try:
        admin = psycopg.connect(
            settings.DATABASE_URL, autocommit=True,
            connect_timeout=PG_CONNECT_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 — connect failure = skip, not fail
        pytest.skip(f"Postgres unavailable: {exc}")

    saved_pool = auth_db._pool
    # Save the three data-plane singletons so we can restore them on teardown.
    saved_run_registry = _rr_mod._run_registry
    saved_trace_singleton = _ts_mod._singleton
    saved_metrics_collector = _wm_mod._metrics_collector

    pool = None
    isolated_registry = None
    isolated_trace = None
    isolated_metrics = None
    try:
        admin.execute(f"DROP SCHEMA IF EXISTS {_AUTH_PG_SCHEMA} CASCADE")
        admin.execute(f"CREATE SCHEMA {_AUTH_PG_SCHEMA}")
        # DATABASE_URL may already carry query params (service DSNs often add
        # sslmode) — pick the right separator instead of assuming a bare DSN.
        sep = "&" if "?" in settings.DATABASE_URL else "?"
        # Mechanism (a): search_path=auth_test ONLY — no ,public fallback.
        # This forces CREATE TABLE IF NOT EXISTS to create the data-plane
        # tables (test_runs, llm_traces, workflow_metrics) in auth_test because
        # the public copies are no longer visible; unqualified writes then also
        # resolve to auth_test. The auth/org tables also live in auth_test so
        # cross-table joins (backfill_org_ids, get_run_owner) work within the
        # single schema. The pgvector `vector` type lives in public — safe here
        # because these data tables have no vector columns.
        dsn = (
            settings.DATABASE_URL
            + f"{sep}options=-c%20search_path%3D{_AUTH_PG_SCHEMA}"
        )
        pool = ConnectionPool(
            conninfo=dsn, min_size=1, max_size=10,
            kwargs={"row_factory": dict_row, "connect_timeout": PG_CONNECT_TIMEOUT_S},
            open=True,
        )
        auth_db._pool = pool  # get_pool() now hands out the isolated pool
        auth_db.init_auth_db()  # creates users in auth_test
        from src.backend.auth.org_db import init_org_db
        init_org_db()  # organizations + org_members in the same isolated schema
        from src.backend.auth.invitations_db import init_invitations_db
        init_invitations_db()  # invitations table (register -> match_invite_on_signup)

        # Build isolated data-plane singletons pointing at the same isolated DSN
        # so both direct-construction (test_core) and TestClient(app) (test_api)
        # paths write to auth_test. Inject them into the module globals so
        # get_run_registry() / get_trace_store() / get_workflow_metrics_collector()
        # return the isolated instances during the test session.
        isolated_registry = _rr_mod.RunRegistry(dsn=dsn)
        _rr_mod._run_registry = isolated_registry

        isolated_trace = _ts_mod.PostgresSpanExporter(dsn=dsn)
        _ts_mod._singleton = isolated_trace

        isolated_metrics = _wm_mod.WorkflowMetricsCollector(dsn=dsn)
        _wm_mod._metrics_collector = isolated_metrics

        yield
    finally:
        # Restore module globals BEFORE closing isolated instances so that
        # RunRegistry.close() / WorkflowMetricsCollector.close() do not
        # accidentally null the (already restored) production singleton.
        _rr_mod._run_registry = saved_run_registry
        _ts_mod._singleton = saved_trace_singleton
        _wm_mod._metrics_collector = saved_metrics_collector

        # Close isolated instances (best-effort; the DB schema is dropped next).
        for inst, method in [
            (isolated_registry, "close"),
            (isolated_trace, "shutdown"),
            (isolated_metrics, "close"),
            (pool, "close"),
        ]:
            if inst is not None:
                try:
                    getattr(inst, method)()
                except Exception:  # noqa: BLE001 — best-effort teardown
                    pass

        auth_db._pool = saved_pool
        try:
            admin.execute(f"DROP SCHEMA IF EXISTS {_AUTH_PG_SCHEMA} CASCADE")
        finally:
            admin.close()

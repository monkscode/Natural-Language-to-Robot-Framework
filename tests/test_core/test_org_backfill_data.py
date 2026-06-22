"""backfill_data_org_ids attributes pre-tenancy rows across all data tables.

Seeds a user+org and one NULL-org row in each of test_runs, llm_traces, and
workflow_metrics, then calls backfill on each isolated instance and asserts
attribution + counts. A separate test patches the source-module accessor
symbols to exercise backfill_data_org_ids() end-to-end.

Referenced by: tests/test_core/
Depends on: src/backend/core/org_backfill.py, src/backend/core/run_registry.py,
            src/backend/core/trace_store.py, src/backend/core/workflow_metrics.py
"""
import uuid
from datetime import datetime

import psycopg
import pytest

from src.backend.core.config import PG_CONNECT_TIMEOUT_S, settings

pytestmark = [pytest.mark.integration]

_SCHEMA = "org_backfill_data_test"


@pytest.fixture(scope="module")
def _shared():
    """Isolated schema holding all four tables, torn down after all tests."""
    from src.backend.core.run_registry import RunRegistry
    from src.backend.core.trace_store import PostgresSpanExporter
    from src.backend.core.workflow_metrics import WorkflowMetricsCollector
    from src.backend.auth import db as auth_db
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    try:
        admin = psycopg.connect(
            settings.DATABASE_URL, autocommit=True,
            connect_timeout=PG_CONNECT_TIMEOUT_S,
        )
    except Exception as exc:
        pytest.skip(f"Postgres unavailable: {exc}")

    admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    admin.execute(f"CREATE SCHEMA {_SCHEMA}")
    admin.execute(f"SET search_path TO {_SCHEMA},public")

    sep = "&" if "?" in settings.DATABASE_URL else "?"
    dsn = settings.DATABASE_URL + f"{sep}options=-c%20search_path%3D{_SCHEMA},public"

    # Bootstrap auth pool pointing at the isolated schema so UserRepository +
    # OrgRepository write into the test schema (users/organizations/org_members).
    saved_pool = auth_db._pool
    pool = ConnectionPool(
        conninfo=dsn, min_size=1, max_size=10,
        kwargs={"row_factory": dict_row, "connect_timeout": PG_CONNECT_TIMEOUT_S},
        open=True,
    )
    auth_db._pool = pool
    from src.backend.auth.db import init_auth_db
    init_auth_db()
    from src.backend.auth.org_db import init_org_db
    init_org_db()

    registry = RunRegistry(dsn=dsn)
    store = PostgresSpanExporter(dsn=dsn)
    collector = WorkflowMetricsCollector(dsn=dsn)

    yield registry, store, collector, admin, dsn

    registry.close()
    store.shutdown()
    collector.close()
    pool.close()
    auth_db._pool = saved_pool
    admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    admin.close()


@pytest.fixture(autouse=True)
def _truncate(_shared):
    """Truncate all data tables before each test for isolation."""
    _registry, _store, _collector, admin, _dsn = _shared
    admin.execute("TRUNCATE workflow_metrics")
    admin.execute("TRUNCATE llm_traces")
    admin.execute("TRUNCATE test_runs")
    admin.execute("TRUNCATE org_members")
    admin.execute("TRUNCATE organizations CASCADE")
    admin.execute("TRUNCATE users CASCADE")


def _user_and_org():
    """Create a fresh user + personal org; return (user_id_str, org_id_str)."""
    from src.backend.auth.repository import UserRepository
    from src.backend.auth.org_repository import OrgRepository
    users, orgs = UserRepository(), OrgRepository()
    email = f"bf-{uuid.uuid4().hex[:8]}@e.com"
    user = users.create_user(email, "S3cretpw!")
    user_id = str(user["id"])
    org_id = orgs.ensure_personal_org(user_id, email)
    return user_id, org_id


def test_backfill_data_org_ids_attributes_all_tables(_shared):
    """NULL-org rows in each of the three tables get attributed after backfill.

    Uses isolated registry/store/collector instances on the test schema.
    Counts returned from each backfill_org_ids() call must be >= 1.
    """
    registry, store, collector, admin, _dsn = _shared

    user_id, org_id = _user_and_org()

    # ---- test_runs: pre-tenancy row (user_id set, org_id NULL) ----
    run_id = str(uuid.uuid4())
    registry.record_start(
        run_id,
        {"user_id": user_id, "email": "bf@e.com"},
        "legacy query",
        "passed",
    )
    row = admin.execute(
        "SELECT org_id FROM test_runs WHERE run_id = %s", (run_id,)
    ).fetchone()
    assert row is not None, "record_start did not insert the row"
    assert row[0] is None, f"Expected NULL org_id, got {row[0]!r}"

    # ---- llm_traces: row linked to a run that already has org_id ----
    # (The trace backfill JOINs test_runs on workflow_id; that row must carry
    # org_id already — which is why we insert it with org_id explicitly here.)
    run_with_org_id = str(uuid.uuid4())
    registry.record_start(
        run_with_org_id,
        {"user_id": user_id, "email": "bf@e.com", "org_id": org_id},
        "query with org",
        "passed",
    )
    trace_span_id = uuid.uuid4().hex
    store.insert_litellm_call(
        span_id=trace_span_id,
        trace_id=uuid.uuid4().hex,
        parent_span_id=None,
        name="gemini.litellm",
        model="gemini-2.5-flash",
        prompt_text="p",
        response_text="r",
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        cost_usd=0.0,
        duration_ms=5.0,
        workflow_id=run_with_org_id,
    )
    # Force trace row to NULL so we're testing the backfill path.
    # (insert_litellm_call tries to resolve org_id from the live process
    # singleton, not our isolated registry.)
    admin.execute(
        "UPDATE llm_traces SET org_id = NULL WHERE id = %s", (trace_span_id,)
    )
    row = admin.execute(
        "SELECT org_id FROM llm_traces WHERE id = %s", (trace_span_id,)
    ).fetchone()
    assert row is not None, "insert_litellm_call did not insert the trace row"
    assert row[0] is None, f"Expected NULL org_id on trace, got {row[0]!r}"

    # ---- workflow_metrics: pre-tenancy row linked to the same run ----
    from src.backend.core.models.workflow_metrics_models import WorkflowMetrics
    metric = WorkflowMetrics(
        workflow_id=run_with_org_id,
        url="http://example.com",
        total_llm_calls=1,
        total_cost=0.0,
        execution_time=1.0,
        timestamp=datetime.now(),
    )
    collector.record_workflow(metric)  # org_id intentionally omitted → NULL
    row = admin.execute(
        "SELECT org_id FROM workflow_metrics WHERE workflow_id = %s", (run_with_org_id,)
    ).fetchone()
    assert row is not None, "record_workflow did not insert the metrics row"
    assert row[0] is None, f"Expected NULL org_id on metrics, got {row[0]!r}"

    # ---- backfill each table via isolated instances ----
    counts = {
        "test_runs": registry.backfill_org_ids(),
        "llm_traces": store.backfill_org_ids(),
        "workflow_metrics": collector.backfill_org_ids(),
    }

    assert counts["test_runs"] >= 1, f"test_runs count: {counts['test_runs']}"
    assert counts["llm_traces"] >= 1, f"llm_traces count: {counts['llm_traces']}"
    assert counts["workflow_metrics"] >= 1, f"workflow_metrics count: {counts['workflow_metrics']}"

    # ---- assert each row now carries org_id ----
    row = admin.execute(
        "SELECT org_id FROM test_runs WHERE run_id = %s", (run_id,)
    ).fetchone()
    assert row[0] == org_id, f"test_runs org_id mismatch: {row[0]!r} != {org_id!r}"

    row = admin.execute(
        "SELECT org_id FROM llm_traces WHERE id = %s", (trace_span_id,)
    ).fetchone()
    assert row[0] == org_id, f"llm_traces org_id mismatch: {row[0]!r} != {org_id!r}"

    row = admin.execute(
        "SELECT org_id FROM workflow_metrics WHERE workflow_id = %s", (run_with_org_id,)
    ).fetchone()
    assert row[0] == org_id, f"workflow_metrics org_id mismatch: {row[0]!r} != {org_id!r}"


def test_backfill_aggregator_returns_correct_shape(_shared, monkeypatch):
    """backfill_data_org_ids() returns {"test_runs", "llm_traces", "workflow_metrics"}.

    Patches source-module accessor symbols so the aggregator uses the isolated
    instances — the lazy `from X import fn` inside the function picks up the
    patched symbol from sys.modules at call time.
    """
    registry, store, collector, admin, _dsn = _shared
    from src.backend.core.org_backfill import backfill_data_org_ids

    import src.backend.core.run_registry as rr_mod
    import src.backend.core.trace_store as ts_mod
    import src.backend.core.workflow_metrics as wm_mod
    monkeypatch.setattr(rr_mod, "get_run_registry", lambda: registry)
    monkeypatch.setattr(ts_mod, "get_trace_store", lambda: store)
    monkeypatch.setattr(wm_mod, "get_workflow_metrics_collector", lambda: collector)

    counts = backfill_data_org_ids()
    assert isinstance(counts, dict)
    # Phase 1b keys are always present; Phase 1c (Task 11) adds learning +
    # kw_query_patterns. Use a superset check so adding more tables in future
    # does not break this assertion.
    assert {"test_runs", "llm_traces", "workflow_metrics"}.issubset(counts.keys())
    assert {"learning", "kw_query_patterns"}.issubset(counts.keys())
    # learning is a dict[str,int]; all other keys are plain ints.
    for key, val in counts.items():
        if key == "learning":
            assert isinstance(val, dict), f"learning value should be dict, got {type(val)}"
        else:
            assert isinstance(val, int), f"{key} value should be int, got {type(val)}"


def test_backfill_returns_zero_when_already_attributed(_shared):
    """A second call on already-attributed rows returns zero for all tables."""
    registry, store, collector, admin, _dsn = _shared

    user_id, org_id = _user_and_org()

    # Row has org_id from the start — nothing to backfill.
    run_id = str(uuid.uuid4())
    registry.record_start(
        run_id,
        {"user_id": user_id, "email": "bf@e.com", "org_id": org_id},
        "already attributed",
        "passed",
    )

    assert registry.backfill_org_ids() == 0
    assert store.backfill_org_ids() == 0
    assert collector.backfill_org_ids() == 0


def test_backfill_resilient_to_none_trace_store(monkeypatch):
    """If get_trace_store() returns None, llm_traces returns 0 and others proceed."""
    import src.backend.core.trace_store as ts_mod
    import src.backend.core.run_registry as rr_mod
    import src.backend.core.workflow_metrics as wm_mod
    from src.backend.core.org_backfill import backfill_data_org_ids

    # get_trace_store returns None (tracing disabled / Postgres unreachable).
    monkeypatch.setattr(ts_mod, "get_trace_store", lambda: None)
    # Other accessors use zero-returning stubs to keep the test DB-free.
    monkeypatch.setattr(rr_mod, "get_run_registry", lambda: _NullRegistry())
    monkeypatch.setattr(wm_mod, "get_workflow_metrics_collector", lambda: _NullCollector())

    counts = backfill_data_org_ids()
    assert "llm_traces" in counts
    assert counts["llm_traces"] == 0
    # test_runs and workflow_metrics stubs also return 0 — they ran independently.
    assert counts["test_runs"] == 0
    assert counts["workflow_metrics"] == 0


class _NullRegistry:
    """Stub that returns 0 for backfill_org_ids()."""
    def backfill_org_ids(self) -> int:
        return 0


class _NullCollector:
    """Stub that returns 0 for backfill_org_ids()."""
    def backfill_org_ids(self) -> int:
        return 0

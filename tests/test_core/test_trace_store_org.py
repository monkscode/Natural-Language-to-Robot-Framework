"""llm_traces gains org_id, backfilled from the run's org by workflow_id."""

import uuid

import psycopg
import pytest

from src.backend.auth import db as auth_db

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


def test_trace_org_id_from_run_org():
    """org_id is attributed on llm_traces either at insert time (via get_run_registry
    lookup in insert_litellm_call) or via backfill_org_ids().

    With proper data-plane isolation (task 16), get_run_registry() returns the
    auth_test-scoped singleton, so insert_litellm_call can resolve the org_id
    from test_runs at insert time — the row arrives with org_id already set.
    backfill_org_ids() is still verified via a separately inserted row that has
    no org_id (direct SQL insert bypasses the lookup).
    """
    from src.backend.auth.repository import UserRepository
    from src.backend.auth.org_repository import OrgRepository
    from src.backend.core.config import settings, PG_CONNECT_TIMEOUT_S
    from src.backend.core.run_registry import RunRegistry
    from src.backend.core.trace_store import PostgresSpanExporter

    dsn = auth_db.get_pool().conninfo
    users, orgs = UserRepository(), OrgRepository()
    user = users.create_user(f"tr-{uuid.uuid4().hex[:8]}@e.com", "S3cretpw!")
    org_id = orgs.ensure_personal_org(str(user["id"]), user["email"])

    wid = str(uuid.uuid4())
    reg = RunRegistry(dsn=dsn)
    reg.record_start(wid, {"user_id": str(user["id"]), "email": user["email"], "org_id": org_id}, "q", "passed")

    store = PostgresSpanExporter(dsn=dsn)

    # Path 1: insert_litellm_call resolves org_id via get_run_registry() at
    # insert time (works correctly when the registry is isolated to auth_test).
    span_id = uuid.uuid4().hex
    store.insert_litellm_call(
        span_id=span_id,
        trace_id=uuid.uuid4().hex,
        parent_span_id=None,
        name="gemini/gemini-2.5-flash.litellm",
        model="gemini-2.5-flash",
        prompt_text="p",
        response_text="r",
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        cost_usd=0.0,
        duration_ms=10.0,
        workflow_id=wid,
    )
    with store._pool.connection() as conn:
        row = conn.execute("SELECT org_id FROM llm_traces WHERE id = %s", (span_id,)).fetchone()
    assert row[0] == org_id, "org_id must be set at insert time via get_run_registry() lookup"

    # Path 2: verify backfill_org_ids() fixes rows that arrived without org_id.
    # Insert a raw row via direct SQL (bypasses lookup) to simulate a legacy row.
    wid2 = str(uuid.uuid4())
    reg.record_start(wid2, {"user_id": str(user["id"]), "email": user["email"], "org_id": org_id}, "q2", "passed")
    span_id2 = uuid.uuid4().hex
    # Use the store's pool (search_path=auth_test) for the direct insert.
    with store._pool.connection() as conn:
        conn.execute(
            "INSERT INTO llm_traces (id, trace_id, name, start_time_ns, end_time_ns, "
            "duration_ms, workflow_id) VALUES (%s, %s, %s, 0, 0, 0.0, %s)",
            (span_id2, uuid.uuid4().hex, "legacy.litellm", wid2),
        )
    with store._pool.connection() as conn:
        before = conn.execute("SELECT org_id FROM llm_traces WHERE id = %s", (span_id2,)).fetchone()
    assert before[0] is None, "raw insert should have org_id=NULL before backfill"

    n = store.backfill_org_ids()
    assert n >= 1, "backfill_org_ids() must update at least the raw row"
    with store._pool.connection() as conn:
        after = conn.execute("SELECT org_id FROM llm_traces WHERE id = %s", (span_id2,)).fetchone()
    assert after[0] == org_id, "backfill must attribute the legacy row to the run's org"

    reg.close()
    store.shutdown()


def test_insert_org_lookup_memoized_per_workflow():
    """insert_litellm_call resolves org_id from the registry at most once per
    workflow_id — repeated calls for the same workflow hit the cache, not the DB."""
    from unittest.mock import MagicMock, patch
    from src.backend.core.trace_store import PostgresSpanExporter

    dsn = auth_db.get_pool().conninfo
    store = PostgresSpanExporter(dsn=dsn)
    wid = str(uuid.uuid4())

    fake_reg = MagicMock()
    fake_reg.get_run_owner.return_value = ("u-x", "org-cached")
    with patch("src.backend.core.run_registry.get_run_registry", return_value=fake_reg):
        for _ in range(3):
            store.insert_litellm_call(
                span_id=uuid.uuid4().hex, trace_id=uuid.uuid4().hex,
                parent_span_id=None, name="x.litellm", model="m",
                prompt_tokens=1, completion_tokens=1, total_tokens=2,
                cost_usd=0.0, duration_ms=1.0, workflow_id=wid,
            )

    assert fake_reg.get_run_owner.call_count == 1, "org lookup must be memoized per workflow"
    assert store._org_cache.get(wid) == "org-cached"
    store.shutdown()

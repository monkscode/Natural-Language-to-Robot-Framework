"""llm_traces gains org_id, backfilled from the run's org by workflow_id."""

import uuid

import pytest

from src.backend.auth import db as auth_db

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


def test_trace_backfill_from_run_org():
    from src.backend.auth.repository import UserRepository
    from src.backend.auth.org_repository import OrgRepository
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
    store.insert_litellm_call(
        span_id=uuid.uuid4().hex,
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
    n = store.backfill_org_ids()
    assert n >= 1
    with store._pool.connection() as conn:
        row = conn.execute("SELECT org_id FROM llm_traces WHERE workflow_id = %s", (wid,)).fetchone()
    assert row[0] == org_id
    reg.close()
    store.shutdown()

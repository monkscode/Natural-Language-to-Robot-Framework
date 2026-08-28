"""execution_records persists org_id; the dedup path is org-scoped (no cross-org merge)."""

import uuid
from datetime import datetime, timezone

import pytest

from src.backend.crew_ai.optimization.execution_memory import ExecutionRecord

pytestmark = pytest.mark.integration


def _rec(org_id, *, query="login as admin", domain="a.test", status="passed", code="x"):
    return ExecutionRecord(
        workflow_id=str(uuid.uuid4()), timestamp=datetime.now(timezone.utc),
        user_query=query, url=f"https://{domain}", domain=domain,
        robot_code=code, code_structure="linear", test_status=status, org_id=org_id,
    )


def test_store_persists_org_id(in_memory_em):
    rec = _rec("org-A")
    in_memory_em.store(rec)
    with in_memory_em.read_conn() as conn:
        row = conn.execute(
            "SELECT org_id FROM execution_records WHERE workflow_id = ?",
            (rec.workflow_id,),
        ).fetchone()
    assert row["org_id"] == "org-A"


def test_get_returns_the_org_it_stored(in_memory_em):
    """The column was persisted but `_row_to_record` did not map it back, so
    every record read through `get()` looked untenanted.

    That is the record `process_user_feedback` hands to the engines, which pass
    `record.org_id` on to Trigger 2's `get_active_hints_raw` and to the hint
    write — so the run read and could flag EVERY org's hints. The test above
    could not see it: it reads the column with raw SQL, never through `get()`.
    """
    rec = _rec("org-A")
    in_memory_em.store(rec)
    assert in_memory_em.get(rec.workflow_id).org_id == "org-A"


def test_org_rows_are_independent(in_memory_em):
    # Same query/domain/status, two orgs. Every run keeps its own row (T1), so
    # repeated runs in org A can never absorb, overwrite or hide org B's row.
    a_ids = []
    for _ in range(6):
        rec = _rec("org-A", code="A-code")
        in_memory_em.store(rec)
        a_ids.append(rec.workflow_id)
    b_rec = _rec("org-B", code="B-code")
    in_memory_em.store(b_rec)

    with in_memory_em.read_conn() as conn:
        rows = conn.execute(
            "SELECT workflow_id, org_id, robot_code FROM execution_records "
            "WHERE LOWER(TRIM(user_query)) = 'login as admin' AND domain = 'a.test'"
        ).fetchall()

    by_id = {r["workflow_id"]: r for r in rows}
    assert set(by_id) == set(a_ids) | {b_rec.workflow_id}
    assert by_id[b_rec.workflow_id]["org_id"] == "org-B"
    assert by_id[b_rec.workflow_id]["robot_code"] == "B-code"
    for wid in a_ids:
        assert by_id[wid]["org_id"] == "org-A"
        assert by_id[wid]["robot_code"] == "A-code"

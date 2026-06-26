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


def test_dedup_does_not_merge_across_orgs(in_memory_em):
    # Same query/domain/status, two orgs. With the dedup org-scoped, BOTH rows
    # exist (no cross-org merge). Run enough identical inserts to exceed the
    # dedup threshold within org A, then assert org B still gets its own row.
    for _ in range(in_memory_em.DEDUPLICATION_THRESHOLD + 1):
        in_memory_em.store(_rec("org-A", code="A-code"))
    in_memory_em.store(_rec("org-B", code="B-code"))
    with in_memory_em.read_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT org_id FROM execution_records "
            "WHERE LOWER(TRIM(user_query)) = 'login as admin' AND domain = 'a.test'"
        ).fetchall()
    orgs = {r["org_id"] for r in rows}
    assert "org-A" in orgs and "org-B" in orgs, "dedup merged org B into org A"

"""execution_embeddings: org written via store(record); find_similar org-scoped."""

import uuid
from datetime import datetime, timezone

import pytest

from src.backend.crew_ai.optimization.execution_memory import ExecutionRecord

pytestmark = pytest.mark.integration

# Fake 384-dim vector (all-MiniLM-L6-v2 dim) — avoids requiring fastembed in CI.
_FAKE_VEC = "[" + ",".join(["0.01"] * 384) + "]"


def _rec(org_id, domain):
    return ExecutionRecord(
        workflow_id=str(uuid.uuid4()), timestamp=datetime.now(timezone.utc),
        user_query="login as admin user", url=f"https://{domain}", domain=domain,
        robot_code="x", code_structure="linear", test_status="passed", org_id=org_id,
    )


def test_find_similar_is_org_scoped(in_memory_em, monkeypatch):
    monkeypatch.setattr(in_memory_em, "_embed", lambda text: _FAKE_VEC)
    in_memory_em.store(_rec("org-A", "a.test"))   # writes execution_embeddings via _store_execution_embedding
    in_memory_em.store(_rec("org-B", "b.test"))
    res_b = in_memory_em.find_similar_executions("login as admin user", org_id="org-B")
    assert res_b, "org B should see its own execution"
    assert all(r["domain"] != "a.test" for r in res_b), "org B leaked org A's execution"

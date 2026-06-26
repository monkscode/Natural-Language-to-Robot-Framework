"""learn_from_feedback persists the run's org_id on the stored correction."""

import uuid
from datetime import datetime, timezone

import pytest

from src.backend.crew_ai.optimization.execution_memory import ExecutionRecord
from src.backend.crew_ai.optimization.nl_feedback_engine import NLFeedbackEngine

pytestmark = pytest.mark.integration


def test_learned_correction_carries_org(in_memory_em):
    engine = NLFeedbackEngine(execution_memory=in_memory_em)
    wid = str(uuid.uuid4())
    record = ExecutionRecord(
        workflow_id=wid, timestamp=datetime.now(timezone.utc),
        user_query="login as admin", url="https://a.test/login", domain="a.test",
        robot_code="x", test_status="failed", failure_category="locator",
        user_feedback="use the stable id locator #submit",
        user_feedback_type="completely_wrong", org_id="org-A",
    )
    # process_feedback returns a triage dict WITHOUT feedback_text.
    # FeedbackLoop.process_execution injects feedback_text into the triage dict
    # (Step 4, "Route to engines via learn_from_feedback") before calling
    # learn_from_feedback — replicate that here so the INSERT is reached. If that
    # injection point moves, this mirror must move with it.
    insight = engine.process_feedback("admin-add", record.user_feedback, "completely_wrong")
    insight["feedback_text"] = record.user_feedback

    engine.learn_from_feedback(record, insight)

    with in_memory_em.read_conn() as conn:
        row = conn.execute(
            "SELECT org_id, is_shared FROM nl_feedback_corrections "
            "WHERE source_workflow_id = ?", (wid,)
        ).fetchone()
    assert row is not None, "Expected a correction row but none was found"
    assert row["org_id"] == "org-A", (
        f"Expected org_id='org-A' but got {row['org_id']!r} — INSERT missing org_id"
    )
    assert row["is_shared"] == 0

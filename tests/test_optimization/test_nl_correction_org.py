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
            "SELECT org_id FROM nl_feedback_corrections "
            "WHERE source_workflow_id = ?", (wid,)
        ).fetchone()
    assert row is not None, "Expected a correction row but none was found"
    assert row["org_id"] == "org-A", (
        f"Expected org_id='org-A' but got {row['org_id']!r} — INSERT missing org_id"
    )


# ---------------------------------------------------------------------------
# Dedup is org-scoped: identical feedback text from another org must create
# that org's own hint, never strengthen / reactivate / unflag the first org's.
# ---------------------------------------------------------------------------

_FEEDBACK = "wait for the dashboard spinner to disappear before asserting"


def _record(org_id, wid=None, url="https://shop.test/dash"):
    return ExecutionRecord(
        workflow_id=wid or str(uuid.uuid4()), timestamp=datetime.now(timezone.utc),
        user_query="verify the dashboard loads", url=url,
        domain="shop.test", robot_code="x", test_status="failed",
        failure_category="timing", user_feedback=_FEEDBACK,
        user_feedback_type="completely_wrong", org_id=org_id,
    )


def _insight(category="timing"):
    # Crafted triage insight (mirrors FeedbackLoop's feedback_text injection);
    # category picks the dedup branch: timing -> domain scope, locator -> url scope.
    return {"category": category, "feedback_text": _FEEDBACK, "actor": "tester"}


def _rows(em):
    with em.read_conn() as conn:
        return conn.execute(
            "SELECT id, org_id, evidence_count, is_active, conflict_flagged "
            "FROM nl_feedback_corrections WHERE feedback_text = ? ORDER BY id",
            (_FEEDBACK,),
        ).fetchall()


def test_identical_feedback_from_other_org_creates_own_hint(in_memory_em):
    engine = NLFeedbackEngine(execution_memory=in_memory_em)
    engine.learn_from_feedback(_record("org-A"), _insight())
    engine.learn_from_feedback(_record("org-B"), _insight())

    rows = _rows(in_memory_em)
    assert len(rows) == 2, (
        f"Expected one hint per org, got {len(rows)} row(s) — org-B's feedback "
        "was deduped against org-A's hint"
    )
    by_org = {r["org_id"]: r for r in rows}
    assert set(by_org) == {"org-A", "org-B"}
    assert by_org["org-A"]["evidence_count"] == 1, (
        "org-B's submission must not add evidence to org-A's hint"
    )


def test_other_org_resubmission_does_not_reactivate_or_unflag(in_memory_em):
    engine = NLFeedbackEngine(execution_memory=in_memory_em)
    engine.learn_from_feedback(_record("org-A"), _insight())
    a_id = _rows(in_memory_em)[0]["id"]
    # org-A's hint was retired and conflict-flagged in the meantime.
    in_memory_em._writer_conn.execute(
        "UPDATE nl_feedback_corrections "
        "SET is_active = 0, conflict_flagged = 1 WHERE id = ?", (a_id,),
    )
    in_memory_em._writer_conn.commit()

    engine.learn_from_feedback(_record("org-B"), _insight())

    a_row = next(r for r in _rows(in_memory_em) if r["id"] == a_id)
    assert a_row["is_active"] == 0, "org-B's feedback reactivated org-A's retired hint"
    assert a_row["conflict_flagged"] == 1, "org-B's feedback cleared org-A's conflict flag"


def test_same_org_resubmission_still_reinforces(in_memory_em):
    engine = NLFeedbackEngine(execution_memory=in_memory_em)
    engine.learn_from_feedback(_record("org-A"), _insight())
    engine.learn_from_feedback(_record("org-A"), _insight())

    rows = _rows(in_memory_em)
    assert len(rows) == 1, "same-org resubmission must reinforce, not duplicate"
    assert rows[0]["evidence_count"] == 2


def test_url_scope_dedup_is_org_scoped(in_memory_em):
    engine = NLFeedbackEngine(execution_memory=in_memory_em)
    engine.learn_from_feedback(_record("org-A"), _insight(category="locator"))
    engine.learn_from_feedback(_record("org-B"), _insight(category="locator"))

    rows = _rows(in_memory_em)
    assert len(rows) == 2, (
        "url-scoped dedup branch must also be org-scoped — org-B's identical "
        "feedback on the same url must create org-B's own hint"
    )
    assert {r["org_id"] for r in rows} == {"org-A", "org-B"}


def test_url_scope_same_org_different_pages_get_separate_hints(in_memory_em):
    # The engine's url-scope dedup key includes the url, so identical feedback
    # on two different pages of the same domain must yield two rows. The old
    # org-blind UNIQUE(feedback_text, domain, scope) constraint contradicted
    # this and silently dropped the second page's hint.
    engine = NLFeedbackEngine(execution_memory=in_memory_em)
    engine.learn_from_feedback(
        _record("org-A", url="https://shop.test/checkout"), _insight(category="locator"))
    engine.learn_from_feedback(
        _record("org-A", url="https://shop.test/cart"), _insight(category="locator"))

    rows = _rows(in_memory_em)
    assert len(rows) == 2, (
        "identical url-scoped feedback on two different pages must create one "
        "hint per page, not be dropped by the uniqueness constraint"
    )

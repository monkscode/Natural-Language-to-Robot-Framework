"""Every run keeps its own `execution_records` row (T1).

`execution_records.workflow_id` is `TEXT UNIQUE NOT NULL` — the table is a
per-run identity table, and `feedback_loop` reaches a run's record only by
`em.get(workflow_id)`. The removed aggregation branch contradicted that: after
five identical (query, domain, status, org) runs it UPDATEd whichever row was
newest in the bucket instead of inserting, so the sixth run's record never
existed and its user correction had nothing to attach to.

Three consequences are pinned here:
  * a run stored into a full bucket is retrievable, and feedback lands on it;
  * a re-run that flips failed -> passed updates ITS OWN row (the aggregation
    UPDATE bypassed the IntegrityError arm, so `_update_to_passing_state` never
    ran and another workflow received the working code);
  * `is_first_attempt`, which `_process_learning` derives from
    `em.get(run_id) is None`, is 1 then 0 across two executions of one workflow.

Referenced by: docs/superpowers/plans/2026-08-26-feedback-integrity-and-org-isolation.md (T1)
Depends on: tests/test_optimization/conftest.py (`in_memory_em`)
"""

from datetime import datetime, timezone, timedelta

import pytest

from src.backend.crew_ai.optimization.execution_memory import ExecutionRecord

pytestmark = pytest.mark.integration

OLD_CEILING = 5          # the deleted DEDUPLICATION_THRESHOLD
QUERY = "search for wireless headphones and read the first price"
DOMAIN = "shop.test"


def _rec(workflow_id, *, status="failed", code="v1", seconds=0, org_id="org-A"):
    return ExecutionRecord(
        workflow_id=workflow_id,
        timestamp=datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
        + timedelta(seconds=seconds),
        user_query=QUERY,
        url=f"https://{DOMAIN}/search",
        domain=DOMAIN,
        robot_code=code,
        code_structure="linear",
        test_status=status,
        org_id=org_id,
    )


def _fill_bucket(em, status, *, prefix, count=OLD_CEILING):
    """Store `count` runs that share one (query, domain, status, org) bucket."""
    ids = [f"{prefix}-{i}" for i in range(count)]
    for i, wid in enumerate(ids):
        em.store(_rec(wid, status=status, code=f"filler-{i}", seconds=i))
    return ids


def test_run_past_the_old_ceiling_keeps_its_own_record(in_memory_em):
    filler = _fill_bucket(in_memory_em, "failed", prefix="wf-fill")
    sixth = "wf-sixth"
    in_memory_em.store(_rec(sixth, code="sixth-code", seconds=OLD_CEILING))

    for wid in filler + [sixth]:
        assert in_memory_em.get(wid) is not None, f"{wid} lost its record"
    assert in_memory_em.get(sixth).robot_code == "sixth-code"


def test_feedback_on_the_sixth_run_lands_on_its_own_row(in_memory_em):
    filler = _fill_bucket(in_memory_em, "failed", prefix="wf-fb")
    sixth = "wf-fb-sixth"
    in_memory_em.store(_rec(sixth, code="sixth-code", seconds=OLD_CEILING))

    in_memory_em.update_user_feedback(sixth, "the price locator is wrong", "close_enough")

    assert in_memory_em.get(sixth).user_feedback == "the price locator is wrong"
    for wid in filler:
        assert in_memory_em.get(wid).user_feedback is None, (
            f"feedback for {sixth} leaked onto {wid}"
        )


def test_rerun_that_passes_updates_its_own_row_not_the_bucket(in_memory_em):
    """F5: with the bucket at the old ceiling, the aggregation UPDATE fired
    instead of raising IntegrityError, so the re-run's own row stayed 'failed'
    with a NULL working_code while an unrelated row received the code."""
    failing = "wf-caseb"
    in_memory_em.store(_rec(failing, status="failed", code="broken", seconds=0))
    passed_bucket = _fill_bucket(in_memory_em, "passed", prefix="wf-caseb-pass")

    in_memory_em.store(
        _rec(failing, status="passed", code="fixed", seconds=OLD_CEILING + 1)
    )

    own = in_memory_em.get(failing)
    assert own.test_status == "passed"
    assert own.working_code == "fixed"
    for i, wid in enumerate(passed_bucket):
        other = in_memory_em.get(wid)
        assert other.robot_code == f"filler-{i}", f"{wid} was overwritten"
        assert other.working_code is None, f"{wid} received another run's code"


def test_is_first_attempt_is_true_then_false_on_a_full_bucket(in_memory_em):
    """R6: `_process_learning` sets `is_first_attempt = (em.get(run_id) is None)`.
    With the bucket full, attempt 1's row was dropped, so attempt 2 also read
    'first' and three learning_metrics aggregates counted one workflow twice."""
    _fill_bucket(in_memory_em, "passed", prefix="wf-first")
    wid = "wf-first-subject"

    first_flags = []
    for attempt in range(2):
        first_flags.append(in_memory_em.get(wid) is None)
        in_memory_em.store(
            _rec(wid, status="passed", code=f"v{attempt}",
                 seconds=OLD_CEILING + attempt)
        )

    assert first_flags == [True, False]

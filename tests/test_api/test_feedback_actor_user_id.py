"""The submitter's user id reaches the hint, not just their email.

The email was already threaded end to end as `actor` (endpoints -> feedback
loop -> triage -> the engine's audit row). The Author permission tier needs
the stable id as well: an email can change, and the tier compares against the
caller's token `sub`. ExecutionRecord carries org_id but no user_id, so the id
rides the same three layers the email already does rather than being fetched
again from a run row that may not exist.

NULL when no token identified the submitter (AUTH_ENFORCED off). The tier must
never match on a placeholder — "unknown" is a legitimate audit actor string
and must never become a legitimate author.

Depends on: src/backend/api/endpoints.py (submit_feedback),
            src/backend/crew_ai/optimization/feedback_loop.py
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.backend.api.endpoints import router
from src.backend.auth.jwt_utils import require_user


_UID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_EMAIL = "author@example.com"


@pytest.fixture
def client_and_loop():
    """The feedback route with a stubbed learning store, so the assertion is
    about what the route HANDS the store, not what the store does with it."""
    loop = MagicMock()
    loop.process_user_feedback.return_value = {
        "outcome": "processed", "category": "structural", "confidence": 0.9,
    }
    app = FastAPI()
    app.include_router(router)
    run_row = {"workflow_id": "wf-1", "user_id": _UID, "org_id": "org-A",
               "rerun_of": None}
    registry = MagicMock()
    registry.get_run.return_value = run_row
    with patch("src.backend.api.endpoints.get_feedback_loop", return_value=loop), \
         patch("src.backend.api.endpoints.get_run_registry", return_value=registry), \
         patch("src.backend.api.endpoints.is_validated_admin", return_value=False):
        yield app, loop


def _post(app, caller):
    app.dependency_overrides[require_user] = lambda: caller
    try:
        with TestClient(app) as client:
            return client.post("/api/feedback", json={
                "workflow_id": "wf-1",
                "feedback_type": "completely_wrong",
                "feedback_text": "wait for the grid before reading a row",
            })
    finally:
        app.dependency_overrides.clear()


def test_the_submitters_user_id_is_handed_to_the_learning_store(client_and_loop):
    app, loop = client_and_loop
    caller = {"user_id": _UID, "email": _EMAIL, "org_id": "org-A",
              "org_role": "org_member", "role": "user"}
    assert _post(app, caller).status_code == 200

    kwargs = loop.process_user_feedback.call_args.kwargs
    assert kwargs["actor"] == _EMAIL
    assert kwargs["actor_user_id"] == _UID


def test_an_unauthenticated_submitter_yields_no_author_id(client_and_loop):
    """AUTH_ENFORCED off: require_user hands back None. The email already
    degrades to the string "unknown" for the audit row; the id must degrade to
    None, because the Author tier compares ids for equality."""
    app, loop = client_and_loop
    assert _post(app, None).status_code == 200

    kwargs = loop.process_user_feedback.call_args.kwargs
    assert kwargs["actor"] == "unknown"
    assert kwargs["actor_user_id"] is None

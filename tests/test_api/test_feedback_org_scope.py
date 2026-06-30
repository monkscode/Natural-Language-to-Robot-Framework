"""Feedback on another org's run is rejected; org-admin peer is accepted.

Org-dimension test strategy (mirrors test_rerun_org_scope pattern):
- owner (org A) creates a run.
- same-org org_admin peer (different user_id, same org_id) should be ALLOWED by
  caller_can_read (org_admin of the run's org), but DENIED by the old bare
  user_id == user_id check — this is the genuine RED gate before the wiring.
- stranger (org B) should be denied under both old and new code.

Real FeedbackRequest schema (read from endpoints.py):
    workflow_id: str
    feedback_text: str = ""
    feedback_type: str  # "close_enough" | "completely_wrong"

Referenced by: src/backend/api/endpoints.py (submit_feedback).
Depends on: tests/test_auth/conftest.py (auth_isolated_schema).
"""

import uuid

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from src.backend.main import app
    return TestClient(app)


def _register(client, email):
    from tests.test_api.conftest import register_active
    return register_active(client, email)


def test_feedback_denied_across_orgs(client):
    """User B (different org) cannot submit feedback on user A's run — 403."""
    from src.backend.auth.jwt_utils import decode_token
    from src.backend.core.run_registry import get_run_registry

    tok_a = _register(client, f"fa-{uuid.uuid4().hex[:8]}@e.com")
    tok_b = _register(client, f"fb-{uuid.uuid4().hex[:8]}@e.com")
    a = decode_token(tok_a)

    rid = str(uuid.uuid4())
    get_run_registry().record_start(
        rid,
        {"user_id": a["user_id"], "email": a["email"], "org_id": a["org_id"]},
        "feedback source",
        "failed",
    )

    resp = client.post(
        "/api/feedback",
        json={"workflow_id": rid, "feedback_text": "fix the locator", "feedback_type": "completely_wrong"},
        headers={"Authorization": f"Bearer {tok_b}"},
    )
    assert resp.status_code in (403, 404), (
        f"Expected 403/404 for cross-org feedback, got {resp.status_code}: {resp.text}"
    )


def test_same_org_admin_peer_can_submit_feedback(client):
    """Same-org org_admin peer (different user_id, same org_id) CAN submit feedback.

    This is the genuine org-dimension RED gate:
    - old code: compares peer.user_id != owner.user_id -> denies (wrong).
    - new code: caller_can_read sees peer is org_admin of the same org -> allows.

    feedback_type validation happens after the ownership gate, so a 400
    (invalid feedback_type passed the gate) or 200/disabled means gate passed.
    A 403 means the old user_id check is still in effect (RED).
    """
    from src.backend.auth.jwt_utils import decode_token, create_access_token
    from src.backend.auth.repository import UserRepository
    from src.backend.auth.org_repository import OrgRepository
    from src.backend.core.run_registry import get_run_registry

    users, orgs = UserRepository(), OrgRepository()

    # Owner — creates the run.
    owner = users.create_user(f"fbo-{uuid.uuid4().hex[:8]}@e.com", "S3cretpw!")
    users.set_status(str(owner["id"]), "active")
    o_org = orgs.ensure_personal_org(str(owner["id"]), owner["email"])

    # Peer — different user_id, but same org (simulate org membership).
    peer = users.create_user(f"fbp-{uuid.uuid4().hex[:8]}@e.com", "S3cretpw!")
    users.set_status(str(peer["id"]), "active")

    rid = str(uuid.uuid4())
    get_run_registry().record_start(
        rid,
        {"user_id": str(owner["id"]), "email": owner["email"], "org_id": o_org},
        "peer feedback source",
        "failed",
    )

    # Peer token: same org_id as owner, org_role=org_admin.
    peer_tok = create_access_token({
        "id": str(peer["id"]),
        "email": peer["email"],
        "role": "user",
        "display_name": "",
        "org_id": o_org,
        "org_role": "org_admin",
    })

    resp = client.post(
        "/api/feedback",
        json={"workflow_id": rid, "feedback_text": "fix the locator", "feedback_type": "completely_wrong"},
        headers={"Authorization": f"Bearer {peer_tok}"},
    )
    # The ownership gate must NOT return 403.
    # 200 (success/disabled/error) or 400 (validation) means the gate passed.
    assert resp.status_code != 403, (
        f"Same-org org_admin peer was denied (status {resp.status_code}): {resp.text}. "
        "OLD code denies by user_id mismatch; NEW code must allow via org_admin."
    )

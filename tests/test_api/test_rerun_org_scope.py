"""Re-run gate is org-scoped: a user from another org cannot re-run another's run.

Org-dimension test strategy (mirrors test_history_org_scope pattern):
- owner (org A) creates a run.
- same-org org_admin peer (different user_id, same org_id) should be ALLOWED by
  caller_can_read (org_admin of the run's org), but DENIED by the old bare
  user_id == user_id check — this is the genuine RED gate before the wiring.
- stranger (org B) should be denied under both old and new code.
- The request goes to /execute-test with {"rerun_of": rid}, which is the real
  route that calls _rerun_from_history (not /generate-and-run).

Referenced by: src/backend/api/endpoints.py (_rerun_from_history).
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
    r = client.post("/auth/register", json={"email": email, "password": "S3cretpw!"})
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def test_cross_org_rerun_denied(client):
    """User B (different org) cannot re-run user A's stored run — 404 (existence not leaked)."""
    from src.backend.auth.jwt_utils import decode_token
    from src.backend.core.run_registry import get_run_registry

    tok_a = _register(client, f"rra-{uuid.uuid4().hex[:8]}@e.com")
    tok_b = _register(client, f"rrb-{uuid.uuid4().hex[:8]}@e.com")
    a = decode_token(tok_a)

    rid = str(uuid.uuid4())
    get_run_registry().record_start(
        rid,
        {"user_id": a["user_id"], "email": a["email"], "org_id": a["org_id"]},
        "cross-org rerun source",
        "passed",
        robot_code="*** Settings ***\n*** Test Cases ***\nDummy\n    Log  hi\n",
    )

    # B (different org) tries to re-run A's stored run -> 404.
    resp = client.post(
        "/execute-test",
        json={"rerun_of": rid},
        headers={"Authorization": f"Bearer {tok_b}"},
    )
    assert resp.status_code == 404, (
        f"Expected 404 for cross-org rerun, got {resp.status_code}: {resp.text}"
    )


def test_same_org_admin_peer_can_rerun(client):
    """Same-org org_admin peer (different user_id, same org_id) CAN re-run.

    This is the genuine org-dimension RED gate:
    - old code: compares peer.user_id != owner.user_id -> denies (wrong).
    - new code: caller_can_read sees peer is org_admin of the same org -> allows.

    The rerun will attempt Docker execution and likely fail (no real Docker run
    here), but the gate itself must not return 404/403 — any other status (200,
    409, 500) means the ownership check passed.
    """
    from src.backend.auth.jwt_utils import decode_token, create_access_token
    from src.backend.auth.repository import UserRepository
    from src.backend.auth.org_repository import OrgRepository
    from src.backend.core.run_registry import get_run_registry

    users, orgs = UserRepository(), OrgRepository()

    # Owner — creates the run.
    owner = users.create_user(f"rro-{uuid.uuid4().hex[:8]}@e.com", "S3cretpw!")
    o_org = orgs.ensure_personal_org(str(owner["id"]), owner["email"])

    # Peer — different user_id, but same org (simulate org membership).
    peer = users.create_user(f"rrp-{uuid.uuid4().hex[:8]}@e.com", "S3cretpw!")

    rid = str(uuid.uuid4())
    get_run_registry().record_start(
        rid,
        {"user_id": str(owner["id"]), "email": owner["email"], "org_id": o_org},
        "peer rerun source",
        "passed",
        robot_code="*** Settings ***\n*** Test Cases ***\nDummy\n    Log  hi\n",
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
        "/execute-test",
        json={"rerun_of": rid},
        headers={"Authorization": f"Bearer {peer_tok}"},
    )
    # The ownership gate must NOT return 404 or 403.
    # The test may 409 (no stored code path issues), 500 (Docker unavailable),
    # or 200 (streaming started) — all mean the gate passed.
    assert resp.status_code not in (403, 404), (
        f"Same-org org_admin peer was denied (status {resp.status_code}): {resp.text}. "
        "OLD code denies by user_id mismatch; NEW code must allow via org_admin."
    )

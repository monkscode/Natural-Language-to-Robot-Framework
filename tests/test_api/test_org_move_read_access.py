"""Org moves: the author keeps read access; nobody else gains it (T4c Part A).

A user moved between orgs (create_team_org / add_member / reassign_user_org
all funnel through the single-active-org invariant) keeps read/download/
re-run/feedback access to a run they own in their OLD org —
caller_can_read's owner-keeps-access rule, whatever the org. A different
user who is now in the mover's NEW org still cannot see that old run: the
run's stored org_id is still the OLD org, so ordinary org-scoping denies it
— proving the new rule does not widen access beyond the owner. The moved
user's old-org run also does not appear in their NEW org's history list —
list scoping is SQL-side and unaffected by the ownership rule.

Referenced by: src/backend/auth/ownership.py (caller_can_read),
               src/backend/api/history_endpoints.py, src/backend/api/endpoints.py
               (rerun, feedback), src/backend/auth/jwt_utils.py
               (authorize_report_access).
Depends on: tests/test_auth/conftest.py (auth_isolated_schema).
"""

import uuid

import pytest
from fastapi.responses import JSONResponse

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


class _Req:
    """Bare stand-in for a Starlette Request — authorize_report_access only
    reads .headers / .cookies (mirrors test_reports_org_scope.py)."""

    def __init__(self, token):
        self.headers = {"Authorization": f"Bearer {token}"}
        self.cookies = {}


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from src.backend.main import app
    return TestClient(app)


def _new_active_user(users, email):
    user = users.create_user(email, "S3cretpw!", "Name")
    users.set_status(str(user["id"]), "active")
    return user


def _token(users, user_id):
    from src.backend.auth.endpoints import _token_payload
    return _token_payload(users.get_by_id(user_id))["access_token"]


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def moved_run(client):
    """A user (mover) owns a run in old_org, then is moved to new_org via
    reassign_user_org — the real entry point an admin uses to move someone.
    Returns the pieces every test below needs."""
    from src.backend.auth.repository import UserRepository
    from src.backend.auth.org_repository import OrgRepository
    from src.backend.core.run_registry import get_run_registry

    users, orgs = UserRepository(), OrgRepository()
    suffix = uuid.uuid4().hex[:8]
    old_admin = _new_active_user(users, f"oma-{suffix}@e.com")
    new_admin = _new_active_user(users, f"nma-{suffix}@e.com")
    mover = _new_active_user(users, f"mv-{suffix}@e.com")

    old_org = orgs.create_team_org("Old Co", str(old_admin["id"]))
    new_org = orgs.create_team_org("New Co", str(new_admin["id"]))
    orgs.add_member(old_org, str(mover["id"]), "org_member")

    rid = str(uuid.uuid4())
    get_run_registry().record_start(
        rid,
        {"user_id": str(mover["id"]), "email": mover["email"], "org_id": old_org},
        "mover's run",
        "passed",
        robot_code="*** Settings ***\n*** Test Cases ***\nDummy\n    Log    hi\n",
    )

    orgs.reassign_user_org(str(mover["id"]), old_org, new_org)
    mover_tok = _token(users, str(mover["id"]))
    return {
        "rid": rid, "mover_id": str(mover["id"]), "mover_tok": mover_tok,
        "old_org": old_org, "new_org": new_org, "users": users, "orgs": orgs,
    }


# ---------------------------------------------------------------------------
# Required test 1 — the moved owner keeps read/download/re-run/feedback on a
# run they own in the org they just left. Covers all four caller_can_read
# consumers (history_endpoints.py, jwt_utils.py, endpoints.py x2).
# ---------------------------------------------------------------------------

def test_moved_owner_reads_own_run_detail(client, moved_run):
    r = client.get(f"/api/history/{moved_run['rid']}", headers=_auth(moved_run["mover_tok"]))
    assert r.status_code == 200


def test_moved_owner_downloads_own_report(moved_run):
    from src.backend.auth.jwt_utils import authorize_report_access
    assert authorize_report_access(_Req(moved_run["mover_tok"]), moved_run["rid"]) is None


def test_moved_owner_can_rerun_own_run(client, moved_run):
    resp = client.post(
        "/execute-test", json={"rerun_of": moved_run["rid"]},
        headers=_auth(moved_run["mover_tok"]),
    )
    # The ownership gate must not return 404 (nor 401/403) — a 500/409/200
    # after that means the gate passed and something downstream (no live
    # Docker here) is the reason for any non-200.
    assert resp.status_code not in (401, 403, 404), (
        f"moved owner denied rerun (status {resp.status_code}): {resp.text}"
    )


def test_moved_owner_can_submit_feedback_on_own_run(client, moved_run):
    resp = client.post(
        "/api/feedback",
        json={"workflow_id": moved_run["rid"], "feedback_text": "ok",
              "feedback_type": "close_enough"},
        headers=_auth(moved_run["mover_tok"]),
    )
    assert resp.status_code not in (401, 403), (
        f"moved owner denied feedback (status {resp.status_code}): {resp.text}"
    )


# ---------------------------------------------------------------------------
# Required test 2 — a different user, now in the mover's NEW org, still
# cannot see the mover's OLD-org run. Proves the owner rule does not widen
# access via a same-current-org shortcut.
# ---------------------------------------------------------------------------

def test_stranger_in_new_org_still_denied_read(client, moved_run):
    stranger = _new_active_user(moved_run["users"], f"str-{uuid.uuid4().hex[:8]}@e.com")
    moved_run["orgs"].add_member(moved_run["new_org"], str(stranger["id"]), "org_member")
    stranger_tok = _token(moved_run["users"], str(stranger["id"]))

    r = client.get(f"/api/history/{moved_run['rid']}", headers=_auth(stranger_tok))
    assert r.status_code == 404


def test_stranger_in_new_org_still_denied_report(moved_run):
    from src.backend.auth.jwt_utils import authorize_report_access
    stranger = _new_active_user(moved_run["users"], f"str2-{uuid.uuid4().hex[:8]}@e.com")
    moved_run["orgs"].add_member(moved_run["new_org"], str(stranger["id"]), "org_member")
    stranger_tok = _token(moved_run["users"], str(stranger["id"]))

    denied = authorize_report_access(_Req(stranger_tok), moved_run["rid"])
    assert isinstance(denied, JSONResponse) and denied.status_code == 403


# ---------------------------------------------------------------------------
# Required test 3 — an unattributed run (user_id NULL) stays
# platform-admin-only; the owner rule (owner_id is not None) cannot widen it.
# ---------------------------------------------------------------------------

def test_unattributed_run_still_platform_admin_only(client):
    from src.backend.core.run_registry import get_run_registry
    from src.backend.auth.repository import UserRepository

    users = UserRepository()
    rid = str(uuid.uuid4())
    get_run_registry().record_start(rid, None, "orphan run", "passed")

    someone = _new_active_user(users, f"orph-{uuid.uuid4().hex[:8]}@e.com")
    tok = _token(users, str(someone["id"]))
    r = client.get(f"/api/history/{rid}", headers=_auth(tok))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Required test 4 — the moved owner's old-org run does not appear in the new
# org's history LIST. List scoping is a SQL org_id filter, not
# caller_can_read, so the owner rule cannot widen it — this is the decision.
# ---------------------------------------------------------------------------

def test_moved_owner_old_run_absent_from_new_org_history_list(client, moved_run):
    resp = client.get("/api/history", headers=_auth(moved_run["mover_tok"]))
    runs = resp.json()["runs"]
    assert all(r["run_id"] != moved_run["rid"] for r in runs)

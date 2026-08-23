"""Leaving an org ends access to that org's data — reads included.

A user moved between orgs (create_team_org / add_member / reassign_user_org
all funnel through the single-active-org invariant) keeps NOTHING of the org
they left: not the history list, not a run's detail by direct id, not its
report, and not the two actions (re-run, feedback).

That is the whole model, and it is deliberate. Nothing in a token
distinguishes an internal transfer from an offboarding — remove_member drops
the user into a fresh personal org exactly as a move does — so a rule keyed on
authorship does not grant "an author who moved", it grants "anyone who ever
authored anything here, forever". Every comparable product declines that:
GitHub, Notion and Figma all revoke on removal and keep the content with the
workspace. Decision 2026-08-23; this file previously pinned the opposite.

Nothing is lost by the ORG: the runs stay, the author's email stays on every
row, and an org_admin still sees and manages all of it. Nothing is lost by the
USER either if they come back — access is computed from their CURRENT org, so
rejoining restores everything with no retention window and no restore step.

A different user who is now in the mover's NEW org still cannot see that old
run: the run's stored org_id is still the OLD org, so ordinary org-scoping
denies it.

Referenced by: src/backend/auth/ownership.py (caller_can_access),
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
# Required test 1 — the moved owner LOSES the READ (detail + report) AND the
# two actions on a run they own in the org they just left. All four consumers
# — history_endpoints.py and jwt_utils.py on the detail/report reads,
# endpoints.py x2 on re-run/feedback — route through the single
# caller_can_access predicate (auth/ownership.py); there is no read/act split
# any more.
# ---------------------------------------------------------------------------

def test_moved_owner_cannot_read_own_old_org_run_detail(client, moved_run):
    """INVERTED 2026-08-23 — was 200. The org's data stays with the org."""
    r = client.get(f"/api/history/{moved_run['rid']}",
                   headers=_auth(moved_run["mover_tok"]))
    assert r.status_code == 404, r.text


def test_moved_owner_cannot_download_own_old_org_report(moved_run):
    """INVERTED 2026-08-23 — was allowed. The report carries whatever
    credentials the author typed into the script, which is precisely what a
    customer expects to stop leaving with a departing employee."""
    from src.backend.auth.jwt_utils import authorize_report_access

    denied = authorize_report_access(_Req(moved_run["mover_tok"]), moved_run["rid"])
    assert isinstance(denied, JSONResponse) and denied.status_code == 403


def test_rejoining_the_org_restores_everything(client, moved_run):
    """The other half of the rule, and why no retention window is needed:
    access is computed from the caller's CURRENT org, so a user who comes back
    sees their old work again with nothing to restore and nothing to expire."""
    from src.backend.auth.jwt_utils import authorize_report_access, decode_token

    moved_run["orgs"].reassign_user_org(
        moved_run["mover_id"], moved_run["new_org"], moved_run["old_org"])
    tok = _token(moved_run["users"], moved_run["mover_id"])
    assert decode_token(tok)["org_id"] == moved_run["old_org"], (
        "the fixture must actually put them back in the old org")

    r = client.get(f"/api/history/{moved_run['rid']}", headers=_auth(tok))
    assert r.status_code == 200, r.text
    assert authorize_report_access(_Req(tok), moved_run["rid"]) is None


def test_moved_owner_cannot_rerun_own_old_org_run(client, moved_run):
    """A re-run executes against the OLD org's environment — that ends with
    the membership, even though the caller wrote the script.

    Exact status, not `not in (401, 403, 404)`: that negative is satisfied by
    a 500, so it passed whether the gate worked or the request blew up. The
    re-run site answers 404 (its source read is org-scoped, so "no such run"
    and "not an org you may act in" collapse and neither leaks).
    """
    resp = client.post(
        "/execute-test", json={"rerun_of": moved_run["rid"]},
        headers=_auth(moved_run["mover_tok"]),
    )
    assert resp.status_code == 404, (
        f"moved owner should not re-run into the old org "
        f"(status {resp.status_code}): {resp.text}"
    )


def test_moved_owner_cannot_submit_feedback_on_own_old_org_run(client, moved_run):
    """Feedback mutates the OLD org's learning store, so it ends with the
    membership too.

    process_user_feedback resolves the execution record and fires conflict
    detection with org_id=record.org_id — the RUN's org, never the caller's —
    so an owner who moved kept steering org A's future generations from org B.

    Exact status, not a negative: 403 here rather than the re-run path's 404
    because this lookup is unscoped, so an unknown run reaches the same line
    with owner_id=None and is refused identically — there is no existence to
    leak, and the status is the site's existing one.
    """
    resp = client.post(
        "/api/feedback",
        json={"workflow_id": moved_run["rid"], "feedback_text": "ok",
              "feedback_type": "close_enough"},
        headers=_auth(moved_run["mover_tok"]),
    )
    assert resp.status_code == 403, (
        f"moved owner should not write feedback into the old org "
        f"(status {resp.status_code}): {resp.text}"
    )


# ---------------------------------------------------------------------------
# Required test 1b — the REMOVAL path specifically. Same split, driven through
# remove_member rather than reassign_user_org.
# ---------------------------------------------------------------------------

def test_removed_member_loses_the_read_and_the_actions(client):
    """remove_member drops the user into a fresh personal org and bumps
    token_version, so they re-login carrying a different org while their runs
    stay in the old one. That is byte-identical to a MOVE from the token's
    point of view — which is why one rule now covers both: leaving an org, for
    any reason, ends access to that org's data.

    It also pins the corner that would otherwise reopen a hole: the removed
    member must not arrive on the legacy `caller_org is None` branch, which
    still grants on a bare owner match. The bumped token_version kills their
    live token, require_user re-validates it, and _token_payload self-heals a
    personal org before minting — so the token below carries a concrete
    org_id and is refused on the org mismatch. If a future change drops that
    self-heal, the asserts here are what fail.
    """
    from src.backend.auth.repository import UserRepository
    from src.backend.auth.org_repository import OrgRepository
    from src.backend.core.run_registry import get_run_registry
    from src.backend.auth.jwt_utils import authorize_report_access

    users, orgs = UserRepository(), OrgRepository()
    suffix = uuid.uuid4().hex[:8]
    admin = _new_active_user(users, f"rma-{suffix}@e.com")
    leaver = _new_active_user(users, f"rlv-{suffix}@e.com")
    org = orgs.create_team_org("Kept Co", str(admin["id"]))
    orgs.add_member(org, str(leaver["id"]), "org_member")

    rid = str(uuid.uuid4())
    get_run_registry().record_start(
        rid,
        {"user_id": str(leaver["id"]), "email": leaver["email"], "org_id": org},
        "leaver's run",
        "passed",
        robot_code="*** Settings ***\n*** Test Cases ***\nDummy\n    Log    hi\n",
    )

    assert orgs.remove_member(org, str(leaver["id"])) is True
    tok = _token(users, str(leaver["id"]))

    from src.backend.auth.jwt_utils import decode_token
    assert decode_token(tok)["org_id"] not in (None, org), (
        "the removed member must re-login into a DIFFERENT, concrete org — "
        "an org_id of None would reach the legacy branch and grant the "
        "actions this test says are gone"
    )

    # INVERTED 2026-08-23: the read goes too. The run, its script and its
    # report all stay with the org the work was done in.
    assert client.get(f"/api/history/{rid}", headers=_auth(tok)).status_code == 404
    denied = authorize_report_access(_Req(tok), rid)
    assert isinstance(denied, JSONResponse) and denied.status_code == 403

    # The org keeps all of it, attributed: the admin still reads it.
    admin_tok = _token(users, str(admin["id"]))
    detail = client.get(f"/api/history/{rid}", headers=_auth(admin_tok))
    assert detail.status_code == 200, detail.text
    assert detail.json()["user_email"] == leaver["email"], (
        "the departed author must still be named on their work")

    # Lost: no steering the org's learning store, no executing against its
    # environment.
    fb = client.post(
        "/api/feedback",
        json={"workflow_id": rid, "feedback_text": "ok",
              "feedback_type": "close_enough"},
        headers=_auth(tok),
    )
    assert fb.status_code == 403, f"removed member kept feedback: {fb.text}"

    rr = client.post("/execute-test", json={"rerun_of": rid}, headers=_auth(tok))
    assert rr.status_code == 404, f"removed member kept re-run: {rr.text}"


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
# caller_can_access, so the owner rule cannot widen it — this is the decision.
# ---------------------------------------------------------------------------

def test_moved_owner_old_run_absent_from_new_org_history_list(client, moved_run):
    resp = client.get("/api/history", headers=_auth(moved_run["mover_tok"]))
    runs = resp.json()["runs"]
    assert all(r["run_id"] != moved_run["rid"] for r in runs)

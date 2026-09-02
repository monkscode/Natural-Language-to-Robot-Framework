"""can_view_learning — the flag that unlocks the Learning page for org admins.

An earlier task on this branch gave org admins real mutation powers over
their own org's hints (create/patch/unflag/retract/reactivate). Those powers
were unreachable because the SPA gated /learning on the PLATFORM admin role
while the API gates it on is_dashboard_viewer (auth/ownership.py), which also
admits an org admin. can_view_learning closes that gap by mirroring
is_dashboard_viewer exactly, computed in TWO places — _token_payload
(login/register/OAuth) and GET /auth/me — so a page refresh can never see a
different answer than login did (AuthContext.hydrate() calls /auth/me on
every page load, not /auth/login).

Exercised here at the HTTP level so a wiring regression in either call site
is caught; is_dashboard_viewer's own branch logic has its pure-function
tests in test_ownership.py.

Mounts just auth_router (mirrors test_endpoints_integration.py) to avoid the
heavy crewai import main.py pulls in.
"""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.backend.auth.endpoints import auth_router
from src.backend.auth.jwt_utils import create_access_token
from src.backend.auth.org_repository import OrgRepository
from src.backend.auth.repository import UserRepository

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


@pytest.fixture(scope="module")
def client():
    app = FastAPI()
    app.include_router(auth_router)
    with TestClient(app) as c:
        yield c


def _unique_email() -> str:
    return f"cvl-{uuid.uuid4().hex[:12]}@example.com"


def _register_active(client: TestClient, email: str) -> dict:
    """Register + activate a user (bypassing login, so no org is provisioned
    yet). Returns the /auth/register response body."""
    reg = client.post("/auth/register", json={"email": email, "password": "S3cretpw!"})
    assert reg.status_code == 201, reg.text
    UserRepository().set_status(reg.json()["user"]["id"], "active")
    return reg.json()


def test_solo_org_admin_login_payload_carries_flag_true(client):
    """A freshly-activated solo user self-heals into their own PERSONAL org at
    login (provision_on_approval -> ensure_personal_org), which seats them as
    org_admin of it. is_dashboard_viewer admits that, so login must carry
    can_view_learning=True while the PLATFORM role stays 'user'."""
    email = _unique_email()
    _register_active(client, email)
    login = client.post("/auth/login", json={"email": email, "password": "S3cretpw!"})
    assert login.status_code == 200
    user = login.json()["user"]
    assert user["role"] == "user"
    assert user["can_view_learning"] is True


def test_plain_org_member_login_payload_carries_flag_false(client):
    """A user added to a TEAM org as org_member (not admin): single-active-org
    membership means their personal-org admin seat is gone too (see
    OrgRepository._collapse_to_single), so nothing here says org_admin and
    the flag must read False."""
    owner_email = _unique_email()
    owner = _register_active(client, owner_email)
    org_id = OrgRepository().create_team_org("Acme CVL", owner["user"]["id"])

    member_email = _unique_email()
    member = _register_active(client, member_email)
    OrgRepository().add_member(org_id, member["user"]["id"], "org_member")

    login = client.post("/auth/login", json={"email": member_email, "password": "S3cretpw!"})
    assert login.status_code == 200
    user = login.json()["user"]
    assert user["role"] == "user"
    assert user["can_view_learning"] is False


def test_platform_admin_login_payload_carries_flag_true(client, monkeypatch):
    """A platform admin sees the flag regardless of their own org_role —
    is_dashboard_viewer short-circuits on is_platform_admin before it ever
    looks at org_role/org_id."""
    from src.backend.core.config import settings

    email = _unique_email()
    monkeypatch.setattr(settings, "ADMIN_EMAILS", email)
    reg = client.post("/auth/register", json={"email": email, "password": "S3cretpw!"})
    assert reg.status_code == 201
    assert reg.json()["user"]["role"] == "admin"
    assert reg.json()["user"]["can_view_learning"] is True


def test_me_matches_login_payload_for_same_user(client):
    """The regression this task exists to prevent: a flag set only in
    _token_payload would disappear the moment the user refreshes, since the
    SPA calls /auth/me on every page load, never /auth/login again."""
    email = _unique_email()
    _register_active(client, email)
    login = client.post("/auth/login", json={"email": email, "password": "S3cretpw!"})
    token = login.json()["access_token"]
    login_flag = login.json()["user"]["can_view_learning"]
    assert login_flag is True  # sanity: this user is a solo org_admin

    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["can_view_learning"] == login_flag


def test_me_fails_closed_on_org_admin_role_with_no_org_id(client):
    """is_dashboard_viewer refuses an org_admin claim with no org_id (a None
    org_id would silently widen a dashboard query to every org) — the flag
    must not disagree with the gate it mirrors. This combination cannot arise
    through normal login/register (org_id and org_role are always minted
    together from the same membership row), so the token is hand-crafted."""
    email = _unique_email()
    data = _register_active(client, email)
    token = create_access_token({
        "id": data["user"]["id"], "email": email, "role": "user", "display_name": "",
        "org_role": "org_admin", "org_id": None,
    })
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["can_view_learning"] is False

"""HTTP-level integration tests for the /auth endpoints (real Postgres).

Mounts just the auth_router in a minimal FastAPI app (avoids the heavy crewai
import from main.py) and drives it with TestClient. Skipped automatically when
Postgres is unreachable. Run after `docker compose up -d postgres`.
"""

import uuid
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.backend.auth import endpoints as auth_endpoints
from src.backend.auth.endpoints import auth_router
from src.backend.auth.repository import PasswordTooLong

# auth_isolated_schema (conftest) reroutes auth_db to the auth_test schema —
# these tests never touch the live public.users table — and skips the module
# when Postgres is unreachable.
pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


@pytest.fixture(scope="module")
def client_and_emails():
    app = FastAPI()
    app.include_router(auth_router)
    created: list[str] = []
    with TestClient(app) as client:
        yield client, created


def _unique_email() -> str:
    return f"ep-{uuid.uuid4().hex[:12]}@example.com"


def test_register_returns_token_and_user(client_and_emails):
    client, created = client_and_emails
    email = _unique_email()
    created.append(email)
    resp = client.post(
        "/auth/register",
        json={"email": email, "password": "S3cretpw!", "display_name": "EP"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["token_type"] == "bearer"
    assert data["access_token"]
    assert data["user"]["email"] == email
    assert data["user"]["role"] == "user"


def test_register_duplicate_returns_409(client_and_emails):
    client, created = client_and_emails
    email = _unique_email()
    created.append(email)
    client.post("/auth/register", json={"email": email, "password": "S3cretpw!"})
    resp = client.post("/auth/register", json={"email": email, "password": "S3cretpw!"})
    assert resp.status_code == 409


def test_register_short_password_returns_422(client_and_emails):
    client, _ = client_and_emails
    resp = client.post("/auth/register", json={"email": _unique_email(), "password": "short"})
    assert resp.status_code == 422


def test_register_long_password_returns_422(client_and_emails):
    """Over the 72-byte bcrypt limit -> rejected by the request model, not a 500."""
    client, _ = client_and_emails
    resp = client.post(
        "/auth/register", json={"email": _unique_email(), "password": "p" * 73}
    )
    assert resp.status_code == 422


def test_register_password_too_long_returns_400(client_and_emails):
    """If the repo's bcrypt length guard fires anyway, it surfaces as 400, never 500."""
    client, _ = client_and_emails
    with patch.object(
        auth_endpoints._repo, "create_user",
        side_effect=PasswordTooLong("password longer than 72 bytes"),
    ):
        resp = client.post(
            "/auth/register", json={"email": _unique_email(), "password": "S3cretpw!"}
        )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "password longer than 72 bytes"


def test_register_invalid_email_returns_422(client_and_emails):
    client, _ = client_and_emails
    for bad in ("not-an-email", "two words@example.com", "a@b", "x@" + "d" * 260 + ".com"):
        resp = client.post("/auth/register", json={"email": bad, "password": "S3cretpw!"})
        assert resp.status_code == 422, f"{bad!r} should be rejected"


def test_forgot_password_never_reveals_registration(client_and_emails):
    """Registered and unknown emails must get the identical response."""
    client, created = client_and_emails
    email = _unique_email()
    created.append(email)
    client.post("/auth/register", json={"email": email, "password": "S3cretpw!"})
    known = client.post("/auth/forgot-password", json={"email": email})
    unknown = client.post("/auth/forgot-password", json={"email": _unique_email()})
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    # Malformed email still 422s (validator shared with register).
    assert client.post(
        "/auth/forgot-password", json={"email": "nope"}
    ).status_code == 422


def test_logout_clears_report_cookie(client_and_emails):
    client, _ = client_and_emails
    resp = client.post("/auth/logout")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    set_cookie = resp.headers.get("set-cookie", "")
    assert "mark1_report_token=" in set_cookie  # deletion = empty value + expiry
    assert "Path=/reports" in set_cookie


def test_me_rejects_deactivated_user(client_and_emails):
    """A valid, unexpired token must stop working the moment the account is
    disabled — /me re-reads the DB precisely for this."""
    from src.backend.auth.repository import UserRepository
    client, created = client_and_emails
    email = _unique_email()
    created.append(email)
    reg = client.post("/auth/register", json={"email": email, "password": "S3cretpw!"})
    data = reg.json()
    uid = data["user"]["id"]
    token = data["access_token"]
    repo = UserRepository()
    repo.set_status(uid, "active")          # new signups are pending; approve so /me is reachable
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/auth/me", headers=headers).status_code == 200
    # Suspend writes status='suspended' + is_active=false in lockstep (mirror).
    repo.set_status(uid, "suspended")
    assert client.get("/auth/me", headers=headers).status_code == 401


def test_me_reachable_for_pending_user(client_and_emails):
    """A freshly-registered (pending) user must be able to call /auth/me so the
    SPA can render the 'pending approval' gate screen on reload."""
    client, created = client_and_emails
    email = _unique_email()
    created.append(email)
    reg = client.post("/auth/register", json={"email": email, "password": "S3cretpw!"})
    token = reg.json()["access_token"]
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"


def test_me_includes_is_org_admin_for_team_org_admin(client_and_emails):
    """A user who is org_admin of a TEAM org must see is_org_admin=True from
    /auth/me — the signal that unlocks the org-owner Team page/nav."""
    from src.backend.auth.repository import UserRepository
    from src.backend.auth.org_repository import OrgRepository
    from src.backend.auth.invitations_db import init_invitations_db
    init_invitations_db()  # /auth/register matches invites; table must exist
    client, created = client_and_emails
    email = _unique_email()
    created.append(email)
    data = client.post("/auth/register", json={"email": email, "password": "S3cretpw!"}).json()
    uid, token = data["user"]["id"], data["access_token"]
    UserRepository().set_status(uid, "active")
    OrgRepository().create_team_org("Acme", uid)  # seats uid as team org_admin
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["is_org_admin"] is True


def test_me_is_org_admin_false_for_personal_only_user(client_and_emails):
    """Being org_admin of only a PERSONAL org must NOT count — every user owns
    their personal org, so this would otherwise be true for everyone."""
    from src.backend.auth.repository import UserRepository
    from src.backend.auth.org_repository import OrgRepository
    from src.backend.auth.invitations_db import init_invitations_db
    init_invitations_db()  # /auth/register matches invites; table must exist
    client, created = client_and_emails
    email = _unique_email()
    created.append(email)
    data = client.post("/auth/register", json={"email": email, "password": "S3cretpw!"}).json()
    uid, token = data["user"]["id"], data["access_token"]
    UserRepository().set_status(uid, "active")
    OrgRepository().ensure_personal_org(uid, email)  # personal org_admin only
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["is_org_admin"] is False


def test_login_and_me_flow(client_and_emails):
    from src.backend.auth.repository import UserRepository
    client, created = client_and_emails
    email = _unique_email()
    created.append(email)
    reg = client.post("/auth/register", json={"email": email, "password": "S3cretpw!"})
    UserRepository().set_status(reg.json()["user"]["id"], "active")   # reachable /auth/me after login

    # wrong password -> 401
    assert client.post("/auth/login", json={"email": email, "password": "nope"}).status_code == 401

    # correct password -> 200 + token
    login = client.post("/auth/login", json={"email": email, "password": "S3cretpw!"})
    assert login.status_code == 200
    token = login.json()["access_token"]

    # /me requires a token
    assert client.get("/auth/me").status_code == 401
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == email


# ---------------------------------------------------------------------------
# Google SSO flow — google_oauth is patched at the endpoints' reference, so no
# real Google round-trip happens; the user rows land in the isolated schema.
# ---------------------------------------------------------------------------

def _google_enabled():
    return patch.object(auth_endpoints.google_oauth, "google_enabled", return_value=True)


def _callback_with_profile(client, profile):
    """Drive /auth/google/callback with a valid state and a canned profile."""
    with _google_enabled(), patch.object(
        auth_endpoints.google_oauth, "exchange_code", return_value=profile
    ):
        client.cookies.set("oauth_state", "stt")
        resp = client.get(
            "/auth/google/callback?state=stt&code=c", follow_redirects=False
        )
        client.cookies.clear()
    return resp


def test_google_endpoints_503_when_not_configured(client_and_emails):
    client, _ = client_and_emails
    with patch.object(
        auth_endpoints.google_oauth, "google_enabled", return_value=False
    ):
        assert client.get("/auth/google/login").status_code == 503
        assert client.get("/auth/google/callback").status_code == 503


def test_google_login_redirects_with_state_cookie(client_and_emails):
    client, _ = client_and_emails
    with _google_enabled(), patch.object(
        auth_endpoints.google_oauth, "build_authorization_url",
        return_value=("https://accounts.google.com/o/oauth2/v2/auth?x=1", "stt-123"),
    ):
        resp = client.get("/auth/google/login", follow_redirects=False)
    client.cookies.clear()
    assert resp.status_code in (302, 307)
    assert resp.headers["location"].startswith("https://accounts.google.com/")
    assert "oauth_state=stt-123" in resp.headers.get("set-cookie", "")


def test_google_callback_error_param_is_urlencoded(client_and_emails):
    """An attacker-suppliable ?error= value never lands unencoded in the redirect."""
    client, _ = client_and_emails
    with _google_enabled():
        resp = client.get(
            "/auth/google/callback?error=access%20denied", follow_redirects=False
        )
    assert resp.status_code in (302, 307)
    assert "/login?error=access%20denied" in resp.headers["location"]


def test_google_callback_state_mismatch_rejected(client_and_emails):
    client, _ = client_and_emails
    with _google_enabled():
        # No state cookie at all:
        resp = client.get(
            "/auth/google/callback?state=whatever&code=c", follow_redirects=False
        )
        assert "error=invalid_state" in resp.headers["location"]
        # Cookie present but different from the state param:
        client.cookies.set("oauth_state", "expected")
        resp = client.get(
            "/auth/google/callback?state=different&code=c", follow_redirects=False
        )
        client.cookies.clear()
        assert "error=invalid_state" in resp.headers["location"]


def test_google_callback_exchange_failure_redirects(client_and_emails):
    client, _ = client_and_emails
    with _google_enabled(), patch.object(
        auth_endpoints.google_oauth, "exchange_code",
        side_effect=RuntimeError("token endpoint down"),
    ):
        client.cookies.set("oauth_state", "stt")
        resp = client.get(
            "/auth/google/callback?state=stt&code=c", follow_redirects=False
        )
        client.cookies.clear()
    assert "error=google_failed" in resp.headers["location"]


def test_google_callback_unverified_email_rejected(client_and_emails):
    client, _ = client_and_emails
    resp = _callback_with_profile(
        client, {"sub": "g-unverified", "email": "x@y.com", "email_verified": False}
    )
    assert "error=email_unverified" in resp.headers["location"]


def test_google_callback_success_sets_token_and_report_cookie(client_and_emails):
    client, created = client_and_emails
    email = _unique_email()
    created.append(email)
    resp = _callback_with_profile(
        client,
        {"sub": f"sub-{email}", "email": email, "email_verified": True, "name": "G"},
    )
    assert "/oauth/callback#token=" in resp.headers["location"]
    cookies = "; ".join(resp.headers.get_list("set-cookie"))
    assert "mark1_report_token=" in cookies
    assert "Path=/reports" in cookies
    assert 'oauth_state="";' in cookies or "oauth_state=;" in cookies  # single-use


def test_google_callback_existing_email_never_autolinked(client_and_emails):
    client, created = client_and_emails
    email = _unique_email()
    created.append(email)
    client.post("/auth/register", json={"email": email, "password": "S3cretpw!"})
    client.cookies.clear()
    resp = _callback_with_profile(
        client, {"sub": f"other-{email}", "email": email, "email_verified": True}
    )
    assert "error=email_exists" in resp.headers["location"]


def test_google_callback_disabled_account_rejected(client_and_emails):
    client, created = client_and_emails
    email = _unique_email()
    created.append(email)
    sub = f"sub-{email}"
    profile = {"sub": sub, "email": email, "email_verified": True}
    assert "#token=" in _callback_with_profile(client, profile).headers["location"]
    from src.backend.auth import db as auth_db
    with auth_db.get_pool().connection() as conn:
        conn.execute("UPDATE users SET status = 'suspended', is_active = FALSE WHERE email = %s", (email,))
        conn.commit()
    resp = _callback_with_profile(client, profile)
    assert "error=account_disabled" in resp.headers["location"]


def test_google_callback_does_not_consume_invite_for_existing_active_user(client_and_emails):
    """An open invite to an already-active email must survive a Google sign-in.

    match_invite_on_signup must fire ONLY for a brand-new pending signup — an
    existing/active user consuming the invite would silently burn the
    org-owner's open invite with no membership ever created (membership only
    materialises at approval of a pending user via provision_on_approval)."""
    from src.backend.auth.repository import UserRepository
    from src.backend.auth.org_repository import OrgRepository
    from src.backend.auth.invitation_repository import InvitationRepository
    from src.backend.auth.invitations_db import init_invitations_db

    init_invitations_db()
    client, created = client_and_emails
    email = _unique_email()
    created.append(email)
    sub = f"sub-{email}"
    profile = {"sub": sub, "email": email, "email_verified": True, "name": "G"}

    # Seed an ACTIVE google user for this email.
    resp = _callback_with_profile(client, profile)
    assert "#token=" in resp.headers["location"]
    repo = UserRepository()
    user = repo.get_by_email(email)
    repo.set_status(str(user["id"]), "active")

    # An open invite to that same (now-active) email, from an unrelated team org.
    owner_email = _unique_email()
    created.append(owner_email)
    owner = repo.create_user(owner_email, "S3cretpw!", "Owner")
    org_id = OrgRepository().create_team_org("Acme", str(owner["id"]))
    invites = InvitationRepository()
    invites.create(email, org_id, str(owner["id"]))

    # Drive the callback again for the now-active user.
    resp = _callback_with_profile(client, profile)
    assert "#token=" in resp.headers["location"]

    # The invite must remain untouched (still 'open') — before the fix it was
    # silently consumed with no membership ever created.
    still_open = invites.find_open_by_email(email)
    assert still_open is not None
    assert still_open["status"] == "open"


def test_allowlisted_admin_signup_gets_personal_org(client_and_emails, monkeypatch):
    """An ADMIN_EMAILS signup lands instantly active and never passes through the
    Approve step — the only other place orgs are provisioned — so register itself
    must provision the personal org. The first token must already carry it."""
    from src.backend.core.config import settings
    from src.backend.auth.org_repository import OrgRepository
    from src.backend.auth.jwt_utils import decode_token
    client, created = client_and_emails
    email = _unique_email()
    created.append(email)
    monkeypatch.setattr(settings, "ADMIN_EMAILS", email)
    resp = client.post("/auth/register", json={"email": email, "password": "S3cretpw!"})
    assert resp.status_code == 201
    assert resp.json()["user"]["role"] == "admin"
    memberships = OrgRepository().get_orgs_for_user(resp.json()["user"]["id"])
    assert len(memberships) == 1
    assert memberships[0]["kind"] == "personal"
    assert decode_token(resp.json()["access_token"])["org_id"] == memberships[0]["org_id"]


def test_non_admin_signup_still_gets_no_org_until_approval(client_and_emails):
    """The signup-time provisioning applies ONLY to instantly-active (allowlisted)
    accounts; a normal signup stays pending and org-less until Approve."""
    from src.backend.auth.org_repository import OrgRepository
    client, created = client_and_emails
    email = _unique_email()
    created.append(email)
    resp = client.post("/auth/register", json={"email": email, "password": "S3cretpw!"})
    assert resp.status_code == 201
    assert resp.json()["user"]["status"] == "pending"
    assert OrgRepository().get_orgs_for_user(resp.json()["user"]["id"]) == []


def test_allowlisted_admin_google_signup_gets_personal_org(client_and_emails, monkeypatch):
    """Same hole on the Google path: an allowlisted Google signup is instantly
    active, so the callback must provision the personal org."""
    from src.backend.core.config import settings
    from src.backend.auth.repository import UserRepository
    from src.backend.auth.org_repository import OrgRepository
    from src.backend.auth.invitations_db import init_invitations_db
    init_invitations_db()
    client, created = client_and_emails
    email = _unique_email()
    created.append(email)
    monkeypatch.setattr(settings, "ADMIN_EMAILS", email)
    profile = {"sub": f"sub-{email}", "email": email, "email_verified": True, "name": "G Adm"}
    resp = _callback_with_profile(client, profile)
    assert "#token=" in resp.headers["location"]
    user = UserRepository().get_by_email(email)
    memberships = OrgRepository().get_orgs_for_user(str(user["id"]))
    assert len(memberships) == 1
    assert memberships[0]["kind"] == "personal"

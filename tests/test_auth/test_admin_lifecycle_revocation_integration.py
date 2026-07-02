"""Task 14 (M12-2): the admin lifecycle routes' token_version-revocation property.
reject/suspend/approve bump token_version (killing existing tokens — approve because
the invitee's pending token is stale once org membership is provisioned, Finding #4);
reactivate does not. Plus 404 (unknown id) and 403 (non-admin actor) on the routes."""
import uuid

import pytest
from fastapi.testclient import TestClient

from src.backend.main import app
from src.backend.auth.repository import UserRepository
from src.backend.auth.jwt_utils import create_access_token
from src.backend.auth.db import get_pool
from src.backend.auth.invitations_db import init_invitations_db

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]
client = TestClient(app)


def _admin_token():
    repo = UserRepository()
    email = f"adm-{uuid.uuid4().hex[:8]}@x.com"
    row = repo.create_user(email, "password123", "Adm")
    repo.set_platform_role(str(row["id"]), "admin")
    repo.set_status(str(row["id"]), "active")
    full = repo.get_by_id(str(row["id"]))
    return email, create_access_token({
        "id": str(full["id"]), "email": full["email"], "role": "admin",
        "display_name": "Adm", "token_version": full["token_version"],
        "status": "active",
    })


def _seed_user(repo, status):
    email = f"u-{uuid.uuid4().hex[:8]}@x.com"
    row = repo.create_user(email, "password123", "U")
    repo.set_status(str(row["id"]), status)
    return email, str(row["id"])


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def _tv(repo, uid):
    return repo.get_by_id(uid)["token_version"]


def test_reject_bumps_token_version():
    repo = UserRepository()
    admin_email, token = _admin_token()
    email, uid = _seed_user(repo, "active")
    try:
        before = _tv(repo, uid)
        r = client.post(f"/auth/admin/users/{uid}/reject", headers=_hdr(token))
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"
        assert _tv(repo, uid) == before + 1
        assert repo.get_by_id(uid)["is_active"] is False
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = ANY(%s)", ([admin_email, email],))
            conn.commit()


def test_suspend_bumps_token_version():
    repo = UserRepository()
    admin_email, token = _admin_token()
    email, uid = _seed_user(repo, "active")
    try:
        before = _tv(repo, uid)
        r = client.post(f"/auth/admin/users/{uid}/suspend", headers=_hdr(token))
        assert r.status_code == 200
        assert r.json()["status"] == "suspended"
        assert _tv(repo, uid) == before + 1
        assert repo.get_by_id(uid)["is_active"] is False
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = ANY(%s)", ([admin_email, email],))
            conn.commit()


def test_approve_bumps_token_version():
    init_invitations_db()   # approve -> provision_on_approval SELECTs invitations
    repo = UserRepository()
    admin_email, token = _admin_token()
    email, uid = _seed_user(repo, "pending")
    try:
        before = _tv(repo, uid)
        r = client.post(f"/auth/admin/users/{uid}/approve", headers=_hdr(token))
        assert r.status_code == 200
        assert r.json()["status"] == "active"
        # approve revokes the stale pending token (org_id=None) so the SPA
        # re-logs-in into a fresh org-bearing token (Finding #4).
        assert _tv(repo, uid) == before + 1
        assert repo.get_by_id(uid)["is_active"] is True
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = ANY(%s)", ([admin_email, email],))
            conn.commit()


def test_reactivate_does_not_bump_token_version():
    repo = UserRepository()
    admin_email, token = _admin_token()
    email, uid = _seed_user(repo, "suspended")
    try:
        before = _tv(repo, uid)
        r = client.post(f"/auth/admin/users/{uid}/reactivate", headers=_hdr(token))
        assert r.status_code == 200
        assert r.json()["status"] == "active"
        assert _tv(repo, uid) == before          # reactivate never revokes
        assert repo.get_by_id(uid)["is_active"] is True
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = ANY(%s)", ([admin_email, email],))
            conn.commit()


def test_transition_unknown_id_returns_404():
    admin_email, token = _admin_token()
    try:
        r = client.post(f"/auth/admin/users/{uuid.uuid4()}/suspend", headers=_hdr(token))
        assert r.status_code == 404
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = %s", (admin_email,))
            conn.commit()


def test_transition_non_admin_forbidden():
    repo = UserRepository()
    actor_email, actor_uid = _seed_user(repo, "active")   # active but NOT admin
    full = repo.get_by_id(actor_uid)
    token = create_access_token({
        "id": actor_uid, "email": actor_email, "role": "user",
        "display_name": "U", "token_version": full["token_version"], "status": "active",
    })
    _, victim_uid = _seed_user(repo, "active")
    try:
        r = client.post(f"/auth/admin/users/{victim_uid}/suspend", headers=_hdr(token))
        assert r.status_code == 403
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE id = ANY(%s)", ([actor_uid, victim_uid],))
            conn.commit()

"""Integration via TestClient: approve flips status and grants access."""
import uuid
import pytest
from fastapi.testclient import TestClient
from src.backend.main import app
from src.backend.auth.repository import UserRepository
from src.backend.auth.jwt_utils import create_access_token
from src.backend.auth.db import get_pool
from src.backend.auth.invitations_db import init_invitations_db
from src.backend.auth.org_repository import OrgRepository

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


def _active_user_token():
    """An ACTIVE non-admin user + token (must be active in the DB to pass
    require_admin's revalidation and reach the 403 role check)."""
    repo = UserRepository()
    email = f"usr-{uuid.uuid4().hex[:8]}@x.com"
    row = repo.create_user(email, "password123", "Usr")
    repo.set_status(str(row["id"]), "active")
    full = repo.get_by_id(str(row["id"]))
    return email, create_access_token({
        "id": str(full["id"]), "email": full["email"], "role": "user",
        "display_name": "Usr", "token_version": full["token_version"],
        "status": "active",
    })


def test_approve_makes_pending_user_active():
    init_invitations_db()
    repo = UserRepository()
    admin_email, token = _admin_token()
    pending_email = f"pend-{uuid.uuid4().hex[:8]}@x.com"
    try:
        pend = repo.create_user(pending_email, "password123", "Pend")
        r = client.post(f"/auth/admin/users/{pend['id']}/approve",
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["status"] == "active"
        assert repo.get_by_id(str(pend["id"]))["is_active"] is True
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([admin_email, pending_email],))
            conn.commit()


def test_create_org_unknown_owner_returns_400():
    """A nonexistent owner_user_id is a bad request, not a 500. The FK violation
    from create_team_org is mapped to a clean 400 (no orphan org left behind —
    create_team_org's connection context rolls back on the error)."""
    init_invitations_db()
    admin_email, token = _admin_token()
    try:
        r = client.post(
            "/auth/admin/orgs",
            json={"name": "Ghost Org", "owner_user_id": str(uuid.uuid4())},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 400
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = %s", (admin_email,))
            conn.commit()


def test_list_users_returns_seeded_users_with_status_and_role():
    init_invitations_db()
    repo = UserRepository()
    admin_email, token = _admin_token()
    u_email = f"mem-{uuid.uuid4().hex[:8]}@x.com"
    try:
        u = repo.create_user(u_email, "password123", "Mem")
        repo.set_status(str(u["id"]), "suspended")
        r = client.get("/auth/admin/users",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        by_email = {row["email"]: row for row in r.json()}
        assert admin_email in by_email and u_email in by_email
        assert by_email[admin_email]["role"] == "admin"
        assert by_email[admin_email]["status"] == "active"
        assert by_email[u_email]["role"] == "user"
        assert by_email[u_email]["status"] == "suspended"
        assert by_email[u_email]["display_name"] == "Mem"
        assert set(by_email[u_email].keys()) == {"id", "email", "display_name", "role", "status"}
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([admin_email, u_email],))
            conn.commit()


def test_list_users_forbidden_for_non_admin():
    init_invitations_db()
    email, token = _active_user_token()
    try:
        r = client.get("/auth/admin/users",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = %s", (email,))
            conn.commit()

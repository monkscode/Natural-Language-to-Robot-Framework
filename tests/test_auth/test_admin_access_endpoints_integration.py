"""Integration via TestClient: approve flips status and grants access."""
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

# tests/test_auth/test_org_endpoints_integration.py
import uuid
import pytest
from fastapi.testclient import TestClient
from src.backend.main import app
from src.backend.auth.repository import UserRepository
from src.backend.auth.org_repository import OrgRepository
from src.backend.auth.jwt_utils import create_access_token
from src.backend.auth.db import get_pool
from src.backend.auth.invitations_db import init_invitations_db

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]
client = TestClient(app)


def _owner_with_team():
    users, orgs = UserRepository(), OrgRepository()
    email = f"own-{uuid.uuid4().hex[:8]}@x.com"
    row = users.create_user(email, "password123", "Own")
    users.set_status(str(row["id"]), "active")
    org_id = orgs.create_team_org("Acme", str(row["id"]))
    full = users.get_by_id(str(row["id"]))
    token = create_access_token({
        "id": str(full["id"]), "email": email, "role": "user", "display_name": "Own",
        "token_version": full["token_version"], "status": "active",
        "org_id": org_id, "org_role": "org_admin",
    })
    return email, org_id, token


def test_org_owner_can_invite():
    init_invitations_db()
    email, org_id, token = _owner_with_team()
    invitee = f"guest-{uuid.uuid4().hex[:8]}@x.com"
    try:
        r = client.post("/auth/org/invitations", json={"email": invitee},
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code in (200, 201)
        r2 = client.get("/auth/org/invitations",
                        headers={"Authorization": f"Bearer {token}"})
        assert any(i["email"] == invitee for i in r2.json())
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = %s", (email,))
            conn.commit()

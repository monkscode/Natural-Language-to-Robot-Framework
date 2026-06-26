"""Platform-admin can grant/revoke admin via the API; others cannot; no self-lockout."""

import uuid

import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


@pytest.fixture(scope="module")
def client():
    from src.backend.main import app
    return TestClient(app)


def _register(client, email):
    return client.post("/auth/register", json={"email": email, "password": "S3cretpw!"}).json()


def _make_admin(client, email):
    from src.backend.auth.repository import UserRepository
    from src.backend.auth.jwt_utils import create_access_token
    reg = _register(client, email)
    uid = reg["user"]["id"]
    UserRepository().set_platform_role(uid, "admin")
    tok = create_access_token({"id": uid, "email": email, "role": "admin", "display_name": ""})
    return uid, tok


def test_platform_admin_can_promote(client):
    _, admin_tok = _make_admin(client, f"adm-{uuid.uuid4().hex[:8]}@e.com")
    target = _register(client, f"tgt-{uuid.uuid4().hex[:8]}@e.com")
    tid = target["user"]["id"]
    r = client.post(f"/auth/admin/users/{tid}/role", json={"role": "admin"},
                    headers={"Authorization": f"Bearer {admin_tok}"})
    assert r.status_code == 200
    assert r.json()["role"] == "admin"


def test_non_admin_forbidden(client):
    target = _register(client, f"nt-{uuid.uuid4().hex[:8]}@e.com")
    user_tok = target["access_token"]  # plain user
    other = _register(client, f"no-{uuid.uuid4().hex[:8]}@e.com")["user"]["id"]
    r = client.post(f"/auth/admin/users/{other}/role", json={"role": "admin"},
                    headers={"Authorization": f"Bearer {user_tok}"})
    assert r.status_code == 403


def test_self_revoke_blocked(client):
    uid, admin_tok = _make_admin(client, f"sr-{uuid.uuid4().hex[:8]}@e.com")
    r = client.post(f"/auth/admin/users/{uid}/role", json={"role": "user"},
                    headers={"Authorization": f"Bearer {admin_tok}"})
    assert r.status_code == 400  # don't let an admin lock themselves out

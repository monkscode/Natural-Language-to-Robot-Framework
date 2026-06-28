"""POST /auth/admin/users/{id}/role records an audit row whose detail carries
the from/to roles."""

import json
import uuid

import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


@pytest.fixture
def client():
    from src.backend.core import audit_log
    from src.backend.main import app
    audit_log.init_audit_log()
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
    return uid, tok, email


def test_role_change_records_detail(client):
    from src.backend.auth.db import get_pool

    _, admin_tok, admin_email = _make_admin(client, f"adm-{uuid.uuid4().hex[:8]}@e.com")
    target = _register(client, f"tgt-{uuid.uuid4().hex[:8]}@e.com")
    tid = target["user"]["id"]

    r = client.post(
        f"/auth/admin/users/{tid}/role", json={"role": "admin"},
        headers={"Authorization": f"Bearer {admin_tok}"},
    )
    assert r.status_code == 200
    rid = r.headers["X-Request-ID"]

    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT actor_email, status_code, detail FROM audit_log "
            "WHERE request_id = %s",
            (rid,),
        ).fetchone()

    assert row is not None
    assert row["actor_email"] == admin_email
    assert row["status_code"] == 200
    assert json.loads(row["detail"]) == {"from": "user", "to": "admin"}

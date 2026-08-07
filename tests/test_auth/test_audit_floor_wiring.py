"""The real app's middleware writes a floor row for a mutating request."""

import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


@pytest.fixture
def client():
    from src.backend.core import audit_log
    from src.backend.main import app
    audit_log.init_audit_log()  # ensure the table exists in the isolated schema
    return TestClient(app)


def test_mutating_request_writes_one_floor_row(client):
    from src.backend.auth.db import get_pool

    # /auth/logout is a token-less POST: the floor records it as 'unknown'.
    # (It is allow-listed in the CI test, but the floor still records it.)
    r = client.post("/auth/logout")
    assert r.status_code == 200
    rid = r.headers["X-Request-ID"]

    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT actor_email, method, path, status_code, request_id "
            "FROM audit_log WHERE request_id = %s",
            (rid,),
        ).fetchall()

    # Exactly one floor row — the whole point of the floor; a duplicate-write
    # regression must fail here, not be hidden by fetchone().
    assert len(rows) == 1, f"expected exactly one floor row, got {len(rows)}"
    row = rows[0]
    assert row["actor_email"] == "unknown"
    assert row["method"] == "POST"
    assert row["path"] == "/auth/logout"
    assert row["status_code"] == 200


def test_get_writes_no_floor_row(client):
    from src.backend.auth.db import get_pool

    r = client.get("/health")
    rid = r.headers["X-Request-ID"]
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM audit_log WHERE request_id = %s", (rid,)
        ).fetchone()
    assert row is None


def test_failed_mutating_request_still_writes_floor_row(client, monkeypatch):
    """A mutating request whose handler crashes is still audited (status 500),
    attributed to the caller. ServerErrorMiddleware sends the 500 and re-raises,
    so without the middleware's crash branch a failed mutation leaves no row."""
    import uuid

    from fastapi.testclient import TestClient
    from src.backend.auth import endpoints
    from src.backend.auth.db import get_pool
    from src.backend.auth.jwt_utils import create_access_token
    from src.backend.main import app

    # Real admin + target, then force the role handler to crash. Patch after
    # setup so the genuine grant below still runs.
    email = f"adm-{uuid.uuid4().hex[:8]}@e.com"
    reg = client.post("/auth/register", json={"email": email, "password": "S3cretpw!"}).json()
    uid = reg["user"]["id"]
    endpoints._repo.set_platform_role(uid, "admin")
    endpoints._repo.set_status(uid, "active")           # <-- ADD: actor active so the handler is reached
    tok = create_access_token({"id": uid, "email": email, "role": "admin", "display_name": ""})
    target = client.post(
        "/auth/register", json={"email": f"tgt-{uuid.uuid4().hex[:8]}@e.com", "password": "S3cretpw!"}
    ).json()
    tid = target["user"]["id"]

    def _boom(*_a, **_k):
        raise RuntimeError("handler crashed")

    monkeypatch.setattr(endpoints._repo, "set_platform_role", _boom)

    # The crash path can't set the X-Request-ID response header, so supply a
    # known id and look the row up by it. raise_server_exceptions=False lets us
    # read the synthesised 500 instead of having TestClient re-raise.
    rid = f"crash-{uuid.uuid4().hex[:8]}"
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.post(
        f"/auth/admin/users/{tid}/role", json={"role": "admin"},
        headers={"Authorization": f"Bearer {tok}", "X-Request-ID": rid},
    )
    assert r.status_code == 500

    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT actor_email, method, status_code FROM audit_log "
            "WHERE request_id = %s",
            (rid,),
        ).fetchall()

    assert len(rows) == 1, f"expected one floor row for the failed mutation, got {len(rows)}"
    assert rows[0]["status_code"] == 500
    assert rows[0]["method"] == "POST"
    assert rows[0]["actor_email"] == email

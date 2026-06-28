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
        row = conn.execute(
            "SELECT actor_email, method, path, status_code, request_id "
            "FROM audit_log WHERE request_id = %s",
            (rid,),
        ).fetchone()

    assert row is not None
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

"""audit_log schema + writer round-trip against an isolated Postgres schema."""

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


def test_init_creates_table_and_write_round_trips():
    from src.backend.core import audit_log
    from src.backend.auth.db import get_pool

    audit_log.init_audit_log()  # creates audit_log in the isolated auth_test schema

    audit_log.write_audit_log(
        actor_email="a@b.com",
        actor_user_id="u1",
        org_id="org9",
        method="POST",
        path="/api/learning/hints/5",
        status_code=200,
        request_id="rid-123",
        source="backend",
        detail='{"from": "user", "to": "admin"}',
    )

    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT actor_email, actor_user_id, org_id, method, path, "
            "status_code, source, request_id, detail "
            "FROM audit_log WHERE request_id = %s",
            ("rid-123",),
        ).fetchone()

    assert row["actor_email"] == "a@b.com"
    assert row["actor_user_id"] == "u1"
    assert row["org_id"] == "org9"
    assert row["method"] == "POST"
    assert row["path"] == "/api/learning/hints/5"
    assert row["status_code"] == 200
    assert row["source"] == "backend"
    assert row["detail"] == '{"from": "user", "to": "admin"}'

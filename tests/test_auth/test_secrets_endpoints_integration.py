"""Integration tests for /api/secrets (TestClient, live Postgres in an isolated schema).

A plain team MEMBER (not an admin) calls every route. Every response body and
every audit row is searched for the marker value: it must never come back.
"""
import json
import logging
import uuid
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from src.backend.auth import secrets as store
from src.backend.auth.db import get_pool
from src.backend.auth.jwt_utils import create_access_token
from src.backend.auth.org_repository import OrgRepository
from src.backend.auth.repository import UserRepository
from src.backend.core import audit_log
from src.backend.core.config import settings
from src.backend.main import app

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]

MARKER = "pw_marker_131"
client = TestClient(app)


@pytest.fixture
def key():
    with patch.object(settings, "SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode()), \
         patch.object(settings, "AUTH_ENFORCED", True):
        yield


def _token(users, user_id, email, org_id, org_role):
    full = users.get_by_id(user_id)
    return create_access_token({
        "id": user_id, "email": email, "role": "user", "display_name": "T",
        "token_version": full["token_version"], "status": "active",
        "org_id": org_id, "org_role": org_role,
    })


@pytest.fixture
def world(key):
    """org A with a plain member (token_a), org B with its owner (token_b)."""
    store.init_secrets_db()
    audit_log.init_audit_log()
    users, orgs = UserRepository(), OrgRepository()
    emails = [f"sec-{uuid.uuid4().hex[:8]}@x.com" for _ in range(3)]
    try:
        owner_a = users.create_user(emails[0], "password123", "OwnA")
        member_a = users.create_user(emails[1], "password123", "MemA")
        owner_b = users.create_user(emails[2], "password123", "OwnB")
        ids = [str(r["id"]) for r in (owner_a, member_a, owner_b)]
        for uid in ids:
            users.set_status(uid, "active")
        org_a = orgs.create_team_org("SecA", ids[0])
        org_b = orgs.create_team_org("SecB", ids[2])
        orgs.add_member(org_a, ids[1], "org_member")
        yield {
            "org_a": org_a,
            "org_b": org_b,
            "a": {"Authorization": "Bearer " + _token(users, ids[1], emails[1], org_a, "org_member")},
            "b": {"Authorization": "Bearer " + _token(users, ids[2], emails[2], org_b, "org_admin")},
        }
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = ANY(%s)", (emails,))
            conn.commit()


def _audit_text(request_id: str) -> str:
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT path, detail FROM audit_log WHERE request_id = %s", (request_id,)
        ).fetchall()
    return json.dumps([dict(r) for r in rows])


def test_member_sets_and_lists(world):
    r = client.put("/api/secrets/db_pass", json={"value": MARKER}, headers=world["a"])
    assert r.status_code == 200
    assert r.json() == {"name": "DB_PASS", "replaced": False}
    assert MARKER not in r.text
    again = client.put("/api/secrets/DB_pass", json={"value": MARKER + "2"}, headers=world["a"])
    assert again.json() == {"name": "DB_PASS", "replaced": True}
    listed = client.get("/api/secrets", headers=world["a"])
    assert listed.status_code == 200
    assert [s["name"] for s in listed.json()] == ["DB_PASS"]
    assert listed.json()[0]["updated_at"]
    assert MARKER not in listed.text


def test_put_audit_row_holds_names_not_values(world):
    r = client.put("/api/secrets/api_key", json={"value": MARKER}, headers=world["a"])
    detail = json.loads(json.loads(_audit_text(r.headers["X-Request-ID"]))[0]["detail"])
    assert detail == {"org_id": world["org_a"], "names": ["API_KEY"]}
    assert MARKER not in _audit_text(r.headers["X-Request-ID"])


def test_upload_adds_and_replaces_in_one_answer(world):
    client.put("/api/secrets/two", json={"value": "old"}, headers=world["a"])
    content = f"ONE={MARKER}\nTWO={MARKER}\nTHREE=x\n"
    r = client.post("/api/secrets/upload", json={"content": content}, headers=world["a"])
    assert r.status_code == 200
    assert sorted(r.json()["added"]) == ["ONE", "THREE"]
    assert r.json()["replaced"] == ["TWO"]
    assert MARKER not in r.text
    audit = _audit_text(r.headers["X-Request-ID"])
    assert MARKER not in audit and "ONE" in audit


def test_upload_with_a_bad_line_names_the_line_and_saves_nothing(world):
    content = f"GOOD={MARKER}\n9bad={MARKER}\nALSO={MARKER}\n"
    r = client.post("/api/secrets/upload", json={"content": content}, headers=world["a"])
    assert r.status_code == 400
    assert "Line 2" in r.json()["detail"]
    assert MARKER not in r.text
    assert client.get("/api/secrets", headers=world["a"]).json() == []


def test_bad_name_is_400_without_the_value(world):
    r = client.put("/api/secrets/1bad", json={"value": MARKER}, headers=world["a"])
    assert r.status_code == 400
    assert MARKER not in r.text


def test_bad_value_is_400(world):
    r = client.put("/api/secrets/ok", json={"value": ""}, headers=world["a"])
    assert r.status_code == 400
    r = client.put("/api/secrets/ok", json={"value": "x" * 1001}, headers=world["a"])
    assert r.status_code == 400


@pytest.mark.parametrize("method,path,raw", [
    ("put", "/api/secrets/ok", b'{"value": "a\\ud800b"}'),            # a lone surrogate cannot be UTF-8
    ("post", "/api/secrets/upload", b'{"content": "A=a\\ud800b\\n"}'),
])
def test_a_lone_surrogate_is_400_with_a_fixed_sentence(world, method, path, raw):
    headers = {**world["a"], "Content-Type": "application/json"}
    r = getattr(client, method)(path, content=raw, headers=headers)
    assert r.status_code == 400
    assert "cannot be stored" in r.json()["detail"]
    assert client.get("/api/secrets", headers=world["a"]).json() == []


@pytest.mark.parametrize("method,path,body", [
    ("put", "/api/secrets/x", {"secret": MARKER}),                        # misnamed field
    ("put", "/api/secrets/x", {"value": 123, "other": MARKER}),           # wrong type + extra
    ("put", "/api/secrets/x", [MARKER]),                                  # not an object
    ("post", "/api/secrets/upload", {"text": MARKER}),                    # misnamed field
    ("post", "/api/secrets/upload", {"content": 5, "other": MARKER}),     # wrong type + extra
    ("post", "/api/secrets/upload", [MARKER]),
])
def test_malformed_body_is_422_and_never_echoes_a_value(world, method, path, body):
    r = getattr(client, method)(path, json=body, headers=world["a"])
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str)
    assert MARKER not in r.text


@pytest.mark.parametrize("method,path", [("put", "/api/secrets/x"), ("post", "/api/secrets/upload")])
def test_non_json_body_is_422_and_never_echoes(world, method, path):
    r = getattr(client, method)(
        path, content=f"{MARKER} not json".encode(),
        headers={**world["a"], "Content-Type": "application/json"},
    )
    assert r.status_code == 422
    assert MARKER not in r.text


def test_extra_field_beside_a_valid_value_is_ignored_and_not_echoed(world):
    r = client.put("/api/secrets/x", json={"value": "v", "other": MARKER}, headers=world["a"])
    assert r.status_code == 200
    assert MARKER not in r.text


def test_no_token_with_auth_enforced_is_401(world):
    assert client.get("/api/secrets").status_code == 401
    assert client.put("/api/secrets/x", json={"value": "v"}).status_code == 401


def test_no_token_with_auth_off_is_401_not_a_crash(world):
    with patch.object(settings, "AUTH_ENFORCED", False):
        for r in (client.get("/api/secrets"),
                  client.put("/api/secrets/x", json={"value": "v"}),
                  client.post("/api/secrets/upload", json={"content": "A=b"}),
                  client.delete("/api/secrets/x")):
            assert r.status_code == 401


def test_a_user_with_no_org_is_403(world):
    users = UserRepository()
    email = f"noorg-{uuid.uuid4().hex[:8]}@x.com"
    try:
        row = users.create_user(email, "password123", "No")
        uid = str(row["id"])
        users.set_status(uid, "active")
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM org_members WHERE user_id = %s", (uid,))
            conn.commit()
        headers = {"Authorization": "Bearer " + _token(users, uid, email, None, None)}
        assert client.get("/api/secrets", headers=headers).status_code == 403
        assert client.put("/api/secrets/x", json={"value": "v"}, headers=headers).status_code == 403
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = %s", (email,))
            conn.commit()


def test_key_unset_is_503_on_all_four_routes(world):
    sentence = "Secrets are not set up on this server (SECRETS_ENCRYPTION_KEY)"
    with patch.object(settings, "SECRETS_ENCRYPTION_KEY", ""):
        for r in (client.get("/api/secrets", headers=world["a"]),
                  client.put("/api/secrets/x", json={"value": MARKER}, headers=world["a"]),
                  client.post("/api/secrets/upload", json={"content": f"A={MARKER}"}, headers=world["a"]),
                  client.delete("/api/secrets/x", headers=world["a"])):
            assert r.status_code == 503
            assert r.json()["detail"] == sentence
            assert MARKER not in r.text


def test_delete_twice_is_204_then_404(world):
    client.put("/api/secrets/gone", json={"value": MARKER}, headers=world["a"])
    first = client.delete("/api/secrets/gone", headers=world["a"])
    assert first.status_code == 204
    assert first.content == b""
    assert _audit_text(first.headers["X-Request-ID"]).count("GONE") >= 1
    assert client.delete("/api/secrets/gone", headers=world["a"]).status_code == 404


def test_another_org_cannot_see_or_delete(world):
    client.put("/api/secrets/theirs", json={"value": MARKER}, headers=world["a"])
    assert client.get("/api/secrets", headers=world["b"]).json() == []
    assert client.delete("/api/secrets/theirs", headers=world["b"]).status_code == 404
    assert [s["name"] for s in client.get("/api/secrets", headers=world["a"]).json()] == ["THEIRS"]


def test_too_many_secrets_is_400(world):
    content = "\n".join(f"K{i}=v" for i in range(store.MAX_SECRETS_PER_ORG + 1))
    r = client.post("/api/secrets/upload", json={"content": content}, headers=world["a"])
    assert r.status_code == 400
    assert client.get("/api/secrets", headers=world["a"]).json() == []


# --- startup warning (R10): the key itself is checked, never echoed ---

def test_startup_warns_once_when_the_key_is_unset(caplog):
    from src.backend import main
    with patch.object(settings, "SECRETS_ENCRYPTION_KEY", ""), caplog.at_level(logging.WARNING):
        main._warn_if_secrets_key_unusable()
    warnings = [r for r in caplog.records if "SECRETS_ENCRYPTION_KEY" in r.getMessage()]
    assert len(warnings) == 1


def test_startup_warns_when_the_key_is_malformed_without_echoing_it(caplog):
    from src.backend import main
    with patch.object(settings, "SECRETS_ENCRYPTION_KEY", MARKER), caplog.at_level(logging.WARNING):
        main._warn_if_secrets_key_unusable()
    assert len([r for r in caplog.records if "SECRETS_ENCRYPTION_KEY" in r.getMessage()]) == 1
    assert MARKER not in caplog.text


def test_startup_is_quiet_with_a_valid_key(caplog):
    from src.backend import main
    with patch.object(settings, "SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode()), \
         caplog.at_level(logging.WARNING):
        main._warn_if_secrets_key_unusable()
    assert not [r for r in caplog.records if "SECRETS_ENCRYPTION_KEY" in r.getMessage()]

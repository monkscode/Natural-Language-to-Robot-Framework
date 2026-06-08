"""HTTP-level integration tests for the /auth endpoints (real Postgres).

Mounts just the auth_router in a minimal FastAPI app (avoids the heavy crewai
import from main.py) and drives it with TestClient. Skipped automatically when
Postgres is unreachable. Run after `docker compose up -d postgres`.
"""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.backend.auth import db as auth_db
from src.backend.auth.endpoints import auth_router

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client_and_emails():
    try:
        auth_db.init_auth_db()
    except Exception as exc:  # noqa: BLE001 — connect failure = skip, not fail
        pytest.skip(f"Postgres unavailable: {exc}")
    app = FastAPI()
    app.include_router(auth_router)
    created: list[str] = []
    with TestClient(app) as client:
        yield client, created
    try:
        with auth_db.get_pool().connection() as conn:
            for email in created:
                conn.execute("DELETE FROM users WHERE email = %s", (email.lower(),))
            conn.commit()
    except Exception:  # noqa: BLE001 — best-effort cleanup
        pass


def _unique_email() -> str:
    return f"ep-{uuid.uuid4().hex[:12]}@example.com"


def test_register_returns_token_and_user(client_and_emails):
    client, created = client_and_emails
    email = _unique_email()
    created.append(email)
    resp = client.post(
        "/auth/register",
        json={"email": email, "password": "S3cretpw!", "display_name": "EP"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["token_type"] == "bearer"
    assert data["access_token"]
    assert data["user"]["email"] == email
    assert data["user"]["role"] == "user"


def test_register_duplicate_returns_409(client_and_emails):
    client, created = client_and_emails
    email = _unique_email()
    created.append(email)
    client.post("/auth/register", json={"email": email, "password": "S3cretpw!"})
    resp = client.post("/auth/register", json={"email": email, "password": "S3cretpw!"})
    assert resp.status_code == 409


def test_register_short_password_returns_422(client_and_emails):
    client, _ = client_and_emails
    resp = client.post("/auth/register", json={"email": _unique_email(), "password": "short"})
    assert resp.status_code == 422


def test_login_and_me_flow(client_and_emails):
    client, created = client_and_emails
    email = _unique_email()
    created.append(email)
    client.post("/auth/register", json={"email": email, "password": "S3cretpw!"})

    # wrong password -> 401
    assert client.post("/auth/login", json={"email": email, "password": "nope"}).status_code == 401

    # correct password -> 200 + token
    login = client.post("/auth/login", json={"email": email, "password": "S3cretpw!"})
    assert login.status_code == 200
    token = login.json()["access_token"]

    # /me requires a token
    assert client.get("/auth/me").status_code == 401
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == email

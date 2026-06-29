"""Postgres-backed tests for token_version revocation (logout-all).

Mounts just the auth_router (avoids main.py's heavy imports) and drives it with
TestClient. Skipped automatically when Postgres is unreachable.
"""
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.backend.auth.endpoints import auth_router
from src.backend.auth.repository import UserRepository

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


@pytest.fixture(scope="module")
def client():
    app = FastAPI()
    app.include_router(auth_router)
    with TestClient(app) as c:
        yield c


def _unique_email() -> str:
    return f"tv-{uuid.uuid4().hex[:12]}@example.com"


def test_bump_token_version_increments():
    repo = UserRepository()
    row = repo.create_user(_unique_email(), "S3cretpw!", "TV")
    assert repo.get_by_id(row["id"])["token_version"] == 0
    assert repo.bump_token_version(row["id"]) == 1
    assert repo.get_by_id(row["id"])["token_version"] == 1


def test_logout_all_revokes_existing_token(client):
    email = _unique_email()
    reg = client.post(
        "/auth/register", json={"email": email, "password": "S3cretpw!"}
    ).json()
    token = reg["access_token"]
    UserRepository().set_status(reg["user"]["id"], "active")     # reachable /auth/me
    auth = {"Authorization": f"Bearer {token}"}

    # The freshly minted token works.
    assert client.get("/auth/me", headers=auth).status_code == 200

    # Revoke all tokens for this user.
    assert client.post("/auth/logout-all", headers=auth).status_code == 200

    # The same token is now rejected on the DB-backed path.
    revoked = client.get("/auth/me", headers=auth)
    assert revoked.status_code == 401
    assert revoked.json()["detail"] == "Token revoked"

    # A fresh login mints a token carrying the new token_version, which works.
    new_token = client.post(
        "/auth/login", json={"email": email, "password": "S3cretpw!"}
    ).json()["access_token"]
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {new_token}"}).status_code == 200

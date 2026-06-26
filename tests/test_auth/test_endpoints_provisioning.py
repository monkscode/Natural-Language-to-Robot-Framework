"""Registering (and Google sign-in) provisions a personal org for the new user."""

import uuid

import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


@pytest.fixture(scope="module")
def client():
    # Import inside the fixture so the isolated-schema pool is already injected.
    from src.backend.main import app
    return TestClient(app)


def _unique_email() -> str:
    return f"ep-{uuid.uuid4().hex[:12]}@example.com"


def test_register_provisions_personal_org(client):
    from src.backend.auth.repository import UserRepository
    from src.backend.auth.org_repository import OrgRepository

    email = _unique_email()
    resp = client.post("/auth/register", json={"email": email, "password": "S3cretpw!"})
    assert resp.status_code == 201

    user = UserRepository().get_by_email(email)
    memberships = OrgRepository().get_orgs_for_user(str(user["id"]))
    assert len(memberships) == 1
    assert memberships[0]["org_role"] == "org_admin"
    assert memberships[0]["kind"] == "personal"

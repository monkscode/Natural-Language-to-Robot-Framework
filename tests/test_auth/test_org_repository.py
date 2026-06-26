"""Integration tests for OrgRepository against a real PostgreSQL.

Skipped when Postgres is unreachable. Rows live in the throwaway auth_test
schema (dropped at session end).
"""

import uuid

import pytest

from src.backend.auth.repository import UserRepository
from src.backend.auth.org_repository import OrgRepository

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


def _unique_email() -> str:
    return f"org-{uuid.uuid4().hex[:12]}@example.com"


@pytest.fixture(scope="module")
def repos():
    yield UserRepository(), OrgRepository()


def test_ensure_creates_personal_org_with_admin_role(repos):
    users, orgs = repos
    email = _unique_email()
    user = users.create_user(email, "S3cretpw!")
    org_id = orgs.ensure_personal_org(str(user["id"]), email)
    assert org_id
    memberships = orgs.get_orgs_for_user(str(user["id"]))
    assert len(memberships) == 1
    assert memberships[0]["org_id"] == org_id
    assert memberships[0]["org_role"] == "org_admin"
    assert memberships[0]["kind"] == "personal"
    assert memberships[0]["name"] == email


def test_ensure_is_idempotent(repos):
    users, orgs = repos
    email = _unique_email()
    user = users.create_user(email, "S3cretpw!")
    first = orgs.ensure_personal_org(str(user["id"]), email)
    second = orgs.ensure_personal_org(str(user["id"]), email)
    assert first == second
    assert len(orgs.get_orgs_for_user(str(user["id"]))) == 1


def test_get_orgs_empty_for_unprovisioned_user(repos):
    users, orgs = repos
    user = users.create_user(_unique_email(), "S3cretpw!")
    assert orgs.get_orgs_for_user(str(user["id"])) == []

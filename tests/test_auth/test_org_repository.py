"""Integration tests for OrgRepository against a real PostgreSQL.

Skipped when Postgres is unreachable. Rows live in the throwaway auth_test
schema (dropped at session end).
"""

import uuid

import pytest

from src.backend.auth.db import get_pool
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


def test_ensure_creates_personal_org_with_owner_stamped(repos):
    """Change 2 step 3 (fresh create): a brand new personal org carries
    owner_user_id from the INSERT itself — no legacy NULL-owner gap for any
    org minted from here on."""
    users, orgs = repos
    email = _unique_email()
    user = users.create_user(email, "S3cretpw!")
    org_id = orgs.ensure_personal_org(str(user["id"]), email)
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT owner_user_id FROM organizations WHERE id = %s", (org_id,)
        ).fetchone()
    assert str(row["owner_user_id"]) == str(user["id"])


def test_ensure_personal_org_stamps_legacy_null_owner(repos):
    """Change 2 step 1's opportunistic stamp: a personal org reachable
    through org_members whose owner_user_id is still NULL (legacy data
    predating this column, or a backfill hole) gets claimed on the very
    next call — the last moment the link is knowable, before a future team
    join deletes the membership and the link with it."""
    users, orgs = repos
    email = _unique_email()
    user = users.create_user(email, "S3cretpw!")
    with get_pool().connection() as conn:
        # Bypass ensure_personal_org to simulate a pre-Change-2 row: no
        # owner_user_id set at creation.
        org = conn.execute(
            "INSERT INTO organizations (name, kind) VALUES (%s, 'personal') "
            "RETURNING id",
            (f"Legacy {uuid.uuid4().hex[:8]}",),
        ).fetchone()
        conn.execute(
            "INSERT INTO org_members (org_id, user_id, org_role) "
            "VALUES (%s, %s, 'org_admin')",
            (org["id"], str(user["id"])),
        )
        conn.commit()

    result = orgs.ensure_personal_org(str(user["id"]), email)

    assert result == str(org["id"])
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT owner_user_id FROM organizations WHERE id = %s", (org["id"],)
        ).fetchone()
    assert str(row["owner_user_id"]) == str(user["id"])


def test_ensure_personal_org_stamp_tolerates_owner_collision(repos):
    """Corner case (task-2 brief): the opportunistic stamp must never raise
    even when it collides with uq_org_owner_personal. Legacy data can leave
    a user's LIVE membership in org A (owner_user_id NULL) while they
    already OWN a different personal org B — e.g. the name-based backfill
    claimed B while A, whose name was not an email, was never touched.
    ensure_personal_org sits on the login path and must return org A's id
    either way, never strand the user behind an exception."""
    users, orgs = repos
    email = _unique_email()
    user = users.create_user(email, "S3cretpw!")
    with get_pool().connection() as conn:
        # org B: already owned by this user (ownership alone is what
        # matters — no membership row needed to reproduce the collision).
        org_b = conn.execute(
            "INSERT INTO organizations (name, kind, owner_user_id) "
            "VALUES (%s, 'personal', %s) RETURNING id",
            (f"Legacy B {uuid.uuid4().hex[:8]}", str(user["id"])),
        ).fetchone()
        # org A: the user's LIVE membership, owner_user_id still NULL.
        org_a = conn.execute(
            "INSERT INTO organizations (name, kind) VALUES (%s, 'personal') "
            "RETURNING id",
            (f"Legacy A {uuid.uuid4().hex[:8]}",),
        ).fetchone()
        conn.execute(
            "INSERT INTO org_members (org_id, user_id, org_role) "
            "VALUES (%s, %s, 'org_admin')",
            (org_a["id"], str(user["id"])),
        )
        conn.commit()

    result = orgs.ensure_personal_org(str(user["id"]), email)  # must not raise

    assert result == str(org_a["id"]), "returns the org the user is a MEMBER of"
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT owner_user_id FROM organizations WHERE id = %s", (org_a["id"],)
        ).fetchone()
    assert row["owner_user_id"] is None, "guarded stamp must not have applied"
    with get_pool().connection() as conn:
        b_row = conn.execute(
            "SELECT owner_user_id FROM organizations WHERE id = %s", (org_b["id"],)
        ).fetchone()
    assert str(b_row["owner_user_id"]) == str(user["id"])  # untouched

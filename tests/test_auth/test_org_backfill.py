"""Backfill assigns a personal org to every user that lacks one. Idempotent."""

import uuid

import pytest

from src.backend.auth.repository import UserRepository
from src.backend.auth.org_repository import OrgRepository

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


def _unique_email() -> str:
    return f"bf-{uuid.uuid4().hex[:12]}@example.com"


def test_backfill_provisions_orphan_users():
    users, orgs = UserRepository(), OrgRepository()
    # Two users created WITHOUT provisioning (simulates pre-tenancy rows).
    u1 = users.create_user(_unique_email(), "S3cretpw!")
    u2 = users.create_user(_unique_email(), "S3cretpw!")
    assert orgs.get_orgs_for_user(str(u1["id"])) == []

    created = orgs.backfill_personal_orgs()
    assert created >= 2  # both orphans, possibly others from earlier tests

    assert len(orgs.get_orgs_for_user(str(u1["id"]))) == 1
    assert len(orgs.get_orgs_for_user(str(u2["id"]))) == 1


def test_backfill_is_idempotent():
    users, orgs = UserRepository(), OrgRepository()
    users.create_user(_unique_email(), "S3cretpw!")
    orgs.backfill_personal_orgs()
    second_pass = orgs.backfill_personal_orgs()
    assert second_pass == 0  # nothing left to provision

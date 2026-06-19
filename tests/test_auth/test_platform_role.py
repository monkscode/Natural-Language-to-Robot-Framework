"""Platform-admin is DB-managed: explicit grant/revoke + promote-only env seed."""

import uuid
from unittest.mock import patch

import pytest

from src.backend.auth.repository import UserRepository

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


def _email() -> str:
    return f"pr-{uuid.uuid4().hex[:8]}@e.com"


def test_set_platform_role_grants_and_revokes():
    users = UserRepository()
    u = users.create_user(_email(), "S3cretpw!")
    assert users.get_by_id(str(u["id"]))["role"] == "user"
    users.set_platform_role(str(u["id"]), "admin")
    assert users.get_by_id(str(u["id"]))["role"] == "admin"
    users.set_platform_role(str(u["id"]), "user")
    assert users.get_by_id(str(u["id"]))["role"] == "user"


def test_set_platform_role_rejects_unknown_role():
    users = UserRepository()
    u = users.create_user(_email(), "S3cretpw!")
    with pytest.raises(ValueError):
        users.set_platform_role(str(u["id"]), "superuser")


def test_seed_promotes_allowlisted_user_only():
    users = UserRepository()
    seeded = users.create_user(_email(), "S3cretpw!")
    other = users.create_user(_email(), "S3cretpw!")
    with patch("src.backend.auth.admin_seed.settings") as s:
        s.admin_emails_list = [seeded["email"]]
        from src.backend.auth.admin_seed import seed_platform_admins
        n = seed_platform_admins()
    assert n >= 1
    assert users.get_by_id(str(seeded["id"]))["role"] == "admin"
    assert users.get_by_id(str(other["id"]))["role"] == "user"  # not in list -> untouched

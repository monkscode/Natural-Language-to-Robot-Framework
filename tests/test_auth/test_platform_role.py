"""Platform-admin is DB-managed: explicit grant/revoke only."""

import uuid

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

import uuid
import pytest
from src.backend.auth.repository import UserRepository
from src.backend.auth.db import get_pool

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


def _email():
    return f"life-{uuid.uuid4().hex[:8]}@x.com"


def _cleanup(email):
    with get_pool().connection() as conn:
        conn.execute("DELETE FROM users WHERE email = %s", (email,))
        conn.commit()


def test_new_password_signup_is_pending_and_inactive():
    repo = UserRepository()
    email = _email()
    try:
        row = repo.create_user(email, "password123", "Life")
        full = repo.get_by_id(str(row["id"]))
        assert full["status"] == "pending"
        assert full["is_active"] is False
    finally:
        _cleanup(email)


def test_set_status_mirrors_is_active_and_bumps_token():
    repo = UserRepository()
    email = _email()
    try:
        created = repo.create_user(email, "password123", "Life")
        uid = str(created["id"])

        approved = repo.set_status(uid, "active")
        assert approved["old_status"] == "pending"
        assert approved["status"] == "active"
        assert repo.get_by_id(uid)["is_active"] is True

        before = repo.get_by_id(uid)["token_version"]
        suspended = repo.set_status(uid, "suspended", bump_token=True)
        assert suspended["status"] == "suspended"
        after = repo.get_by_id(uid)
        assert after["is_active"] is False
        assert after["token_version"] == before + 1

        assert repo.set_status(str(uuid.uuid4()), "active") is None
    finally:
        _cleanup(email)


def test_login_allowed_pending_denied_suspended():
    repo = UserRepository()
    email = _email()
    try:
        created = repo.create_user(email, "password123", "Life")
        uid = str(created["id"])
        # pending can authenticate (to reach the waiting screen)
        assert repo.verify_credentials(email, "password123") is not None
        repo.set_status(uid, "suspended", bump_token=True)
        assert repo.verify_credentials(email, "password123") is None
    finally:
        _cleanup(email)


def test_user_public_includes_status():
    repo = UserRepository()
    email = _email()
    try:
        repo.create_user(email, "password123", "Life")
        from src.backend.auth.endpoints import _user_public
        row = repo.get_by_email(email)
        assert _user_public(row)["status"] == "pending"
    finally:
        _cleanup(email)

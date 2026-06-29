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

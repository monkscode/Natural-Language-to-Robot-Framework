"""Unit tests for auth JWT minting, password hashing, and role assignment.

DB-free: exercises jwt_utils + repository helper functions directly. The auth
code reads the module-level `settings` singleton, so we monkeypatch its
attributes (the global mock_settings fixture yields a *separate* Settings()).
"""

import jwt
import pytest

from src.backend.core.config import settings
from src.backend.auth import jwt_utils
from src.backend.auth.repository import hash_password, verify_password, role_for_email


@pytest.fixture
def auth_settings(monkeypatch):
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "unit-test-secret")
    monkeypatch.setattr(settings, "JWT_EXPIRY_HOURS", 24)
    monkeypatch.setattr(settings, "ADMIN_EMAILS", "admin@corp.com, boss@corp.com")
    return settings


def test_token_roundtrip(auth_settings):
    token = jwt_utils.create_access_token(
        {"id": "u-1", "email": "a@b.com", "role": "user", "display_name": "A"}
    )
    decoded = jwt.decode(token, "unit-test-secret", algorithms=["HS256"])
    assert decoded["sub"] == "u-1"
    assert decoded["email"] == "a@b.com"
    assert decoded["role"] == "user"
    assert decoded["name"] == "A"


def test_token_wrong_secret_rejected(auth_settings):
    token = jwt_utils.create_access_token({"id": "u-1", "email": "a@b.com", "role": "user"})
    with pytest.raises(jwt.PyJWTError):
        jwt.decode(token, "a-different-secret", algorithms=["HS256"])


def test_password_hash_roundtrip():
    hashed = hash_password("S3cretpw!")
    assert hashed != "S3cretpw!"
    assert verify_password("S3cretpw!", hashed) is True
    assert verify_password("wrong-password", hashed) is False


def test_verify_password_handles_missing_hash():
    # google-only accounts have no password hash
    assert verify_password("anything", None) is False
    assert verify_password("anything", "") is False


def test_role_for_email(auth_settings):
    assert role_for_email("admin@corp.com") == "admin"
    assert role_for_email("ADMIN@corp.com") == "admin"  # case-insensitive
    assert role_for_email("  boss@corp.com  ") == "admin"  # whitespace-tolerant
    assert role_for_email("random@corp.com") == "user"


def test_role_for_email_empty_admin_list(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_EMAILS", "")
    assert role_for_email("anyone@corp.com") == "user"

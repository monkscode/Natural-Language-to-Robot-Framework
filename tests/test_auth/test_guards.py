"""Unit tests for the auth dependencies.

Calls get_current_user / require_user / require_admin directly with crafted
credentials (no DB, no TestClient, no httpx) to validate the security matrix:
permissive while AUTH_ENFORCED is False, strict when True.
"""

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from src.backend.core.config import settings
from src.backend.auth import jwt_utils


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _token(role: str = "user") -> str:
    return jwt_utils.create_access_token(
        {"id": "u-1", "email": "u@x.com", "role": role, "display_name": "U"}
    )


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "guard-secret")
    monkeypatch.setattr(settings, "JWT_EXPIRY_HOURS", 24)


# --- Escape hatch: AUTH_ENFORCED=False (local API-only debugging) ---

def test_permissive_allows_missing_token(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENFORCED", False)
    assert jwt_utils.require_user(None) is None
    assert jwt_utils.require_admin(None) is None


def test_get_current_user_is_always_strict(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENFORCED", False)
    with pytest.raises(HTTPException) as exc:
        jwt_utils.get_current_user(None)
    assert exc.value.status_code == 401


# --- Enforced: AUTH_ENFORCED=True (the default) ---

def test_enforced_blocks_missing_token(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENFORCED", True)
    with pytest.raises(HTTPException) as e1:
        jwt_utils.require_user(None)
    assert e1.value.status_code == 401
    with pytest.raises(HTTPException) as e2:
        jwt_utils.require_admin(None)
    assert e2.value.status_code == 401


def test_enforced_user_token_denied_on_admin(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENFORCED", True)
    creds = _creds(_token("user"))
    assert jwt_utils.require_user(creds)["role"] == "user"
    with pytest.raises(HTTPException) as exc:
        jwt_utils.require_admin(creds)
    assert exc.value.status_code == 403


def test_enforced_admin_token_allowed(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENFORCED", True)
    creds = _creds(_token("admin"))
    assert jwt_utils.require_admin(creds)["role"] == "admin"


# --- Token validity is checked regardless of the enforcement flag ---

def test_invalid_token_always_401(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENFORCED", False)
    creds = _creds("not-a-real-jwt")
    for fn in (jwt_utils.require_user, jwt_utils.require_admin, jwt_utils.get_current_user):
        with pytest.raises(HTTPException) as exc:
            fn(creds)
        assert exc.value.status_code == 401


def test_expired_token_401(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENFORCED", False)
    monkeypatch.setattr(settings, "JWT_EXPIRY_HOURS", -1)  # exp in the past
    creds = _creds(_token("user"))
    with pytest.raises(HTTPException) as exc:
        jwt_utils.require_user(creds)
    assert exc.value.status_code == 401

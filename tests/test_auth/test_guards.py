"""Unit tests for the auth dependencies.

Calls get_current_user / require_user / require_admin directly with crafted
credentials (no TestClient, no httpx) to validate the security matrix:
require_user is permissive only while AUTH_ENFORCED is False; require_admin
and get_current_user are always strict. require_admin's per-request DB
re-validation is exercised against a patched repository (no live DB).
"""

from unittest.mock import patch

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


def _db_row(role: str = "user", is_active: bool = True) -> dict:
    return {"id": "u-1", "email": "u@x.com", "role": role, "is_active": is_active}


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "guard-secret")
    monkeypatch.setattr(settings, "JWT_EXPIRY_HOURS", 24)


@pytest.fixture
def _admin_db(request):
    """Patch require_admin's DB re-validation to return a canned row.

    Parametrize indirectly or override per-test with `mock.return_value`.
    """
    with patch.object(jwt_utils._admin_repo, "get_by_id") as mock_get:
        mock_get.return_value = _db_row(role="admin")
        yield mock_get


# --- Escape hatch: AUTH_ENFORCED=False (local API-only debugging) ---

def test_permissive_allows_missing_token(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENFORCED", False)
    assert jwt_utils.require_user(None) is None


def test_require_admin_is_strict_even_when_not_enforced(monkeypatch, _admin_db):
    """The escape hatch never opens admin routes: no token -> 401, user row -> 403."""
    monkeypatch.setattr(settings, "AUTH_ENFORCED", False)
    with pytest.raises(HTTPException) as e1:
        jwt_utils.require_admin(None)
    assert e1.value.status_code == 401
    _admin_db.return_value = _db_row(role="user")
    with pytest.raises(HTTPException) as e2:
        jwt_utils.require_admin(_creds(_token("user")))
    assert e2.value.status_code == 403
    _admin_db.return_value = _db_row(role="admin")
    assert jwt_utils.require_admin(_creds(_token("admin")))["role"] == "admin"


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


def test_enforced_user_token_denied_on_admin(monkeypatch, _admin_db):
    monkeypatch.setattr(settings, "AUTH_ENFORCED", True)
    _admin_db.return_value = _db_row(role="user")
    creds = _creds(_token("user"))
    assert jwt_utils.require_user(creds)["role"] == "user"
    with pytest.raises(HTTPException) as exc:
        jwt_utils.require_admin(creds)
    assert exc.value.status_code == 403


def test_enforced_admin_token_allowed(monkeypatch, _admin_db):
    monkeypatch.setattr(settings, "AUTH_ENFORCED", True)
    creds = _creds(_token("admin"))
    assert jwt_utils.require_admin(creds)["role"] == "admin"


# --- require_admin re-validates against the users table per request ---

def test_admin_revoked_in_db_is_denied_immediately(_admin_db):
    """A still-valid admin token is rejected once the row is deactivated or
    demoted — revocation must not wait for token expiry."""
    creds = _creds(_token("admin"))
    _admin_db.return_value = _db_row(role="admin", is_active=False)
    with pytest.raises(HTTPException) as e1:
        jwt_utils.require_admin(creds)
    assert e1.value.status_code == 401
    _admin_db.return_value = _db_row(role="user")  # demoted since mint
    with pytest.raises(HTTPException) as e2:
        jwt_utils.require_admin(creds)
    assert e2.value.status_code == 403
    _admin_db.return_value = None  # deleted since mint
    with pytest.raises(HTTPException) as e3:
        jwt_utils.require_admin(creds)
    assert e3.value.status_code == 401


def test_admin_promotion_in_db_applies_without_relogin(_admin_db):
    """DB role wins over the stale token claim — promotion works immediately."""
    _admin_db.return_value = _db_row(role="admin")
    assert jwt_utils.require_admin(_creds(_token("user")))["role"] == "admin"


def test_admin_fails_closed_when_auth_store_down(_admin_db):
    _admin_db.side_effect = RuntimeError("connection refused")
    with pytest.raises(HTTPException) as exc:
        jwt_utils.require_admin(_creds(_token("admin")))
    assert exc.value.status_code == 503


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

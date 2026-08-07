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


def _db_row(role: str = "user", is_active: bool = True, status: str = "active") -> dict:
    return {"id": "u-1", "email": "u@x.com", "role": role,
            "is_active": is_active, "status": status}


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
    _admin_db.return_value = _db_row(role="admin", is_active=False, status="suspended")
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


def test_admin_non_uuid_sub_is_401_not_503(_admin_db):
    """A sub that is not a valid UUID is an identity we never minted (401),
    not an auth-store outage (503)."""
    import psycopg
    _admin_db.side_effect = psycopg.DataError("invalid input syntax for type uuid")
    with pytest.raises(HTTPException) as exc:
        jwt_utils.require_admin(_creds(_token("admin")))
    assert exc.value.status_code == 401


# --- require_user re-validates token_version against the DB (revocation) ---

def test_require_user_valid_token_passes(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENFORCED", True)
    creds = _creds(_token("user"))
    with patch.object(jwt_utils._admin_repo, "get_by_id", return_value=_db_row()):
        assert jwt_utils.require_user(creds)["role"] == "user"


def test_require_user_rejects_revoked_token(monkeypatch):
    """A still-unexpired token whose token_version no longer matches the DB
    (logout-all / password change) is rejected on the hot path, not just on
    /auth/me and the admin routes."""
    monkeypatch.setattr(settings, "AUTH_ENFORCED", True)
    creds = _creds(_token("user"))  # token carries token_version 0
    revoked = {**_db_row(), "token_version": 1}  # bumped since the token was minted
    with patch.object(jwt_utils._admin_repo, "get_by_id", return_value=revoked):
        with pytest.raises(HTTPException) as exc:
            jwt_utils.require_user(creds)
    assert exc.value.status_code == 401


def test_require_user_inactive_rejected(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENFORCED", True)
    creds = _creds(_token("user"))
    with patch.object(jwt_utils._admin_repo, "get_by_id",
                      return_value=_db_row(is_active=False, status="suspended")):
        with pytest.raises(HTTPException) as exc:
            jwt_utils.require_user(creds)
    assert exc.value.status_code == 401


def test_require_user_pending_status_rejected(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENFORCED", True)
    creds = _creds(_token("user"))
    with patch.object(jwt_utils._admin_repo, "get_by_id",
                      return_value=_db_row(status="pending")):
        with pytest.raises(HTTPException) as exc:
            jwt_utils.require_user(creds)
    assert exc.value.status_code == 401


def test_require_user_suspended_status_rejected(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENFORCED", True)
    creds = _creds(_token("user"))
    with patch.object(jwt_utils._admin_repo, "get_by_id",
                      return_value=_db_row(status="suspended")):
        with pytest.raises(HTTPException) as exc:
            jwt_utils.require_user(creds)
    assert exc.value.status_code == 401


def test_require_user_fails_closed_when_store_down(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENFORCED", True)
    creds = _creds(_token("user"))
    with patch.object(jwt_utils._admin_repo, "get_by_id",
                      side_effect=RuntimeError("connection refused")):
        with pytest.raises(HTTPException) as exc:
            jwt_utils.require_user(creds)
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


# --- authorize_report_access (the /reports ownership gate) ---

class _FakeRequest:
    """Minimal stand-in: authorize_report_access touches .headers/.cookies."""

    def __init__(self, authorization: str | None = None, cookie: str | None = None):
        self.headers = {"Authorization": authorization} if authorization else {}
        self.cookies = {jwt_utils.REPORT_TOKEN_COOKIE: cookie} if cookie else {}


class _StubRegistry:
    """Stub registry for the ownership layer under authorize_report_access."""

    def __init__(self, owner: str | None, org: str | None = None):
        self._owner = owner
        self._org = org

    def get_owner(self, run_id: str) -> str | None:
        return self._owner

    def get_run_owner(self, run_id: str) -> tuple[str | None, str | None]:
        return (self._owner, self._org)


def _allow_ownership(monkeypatch, owner: str = "u-1"):
    """Make the requested run belong to the _token() identity (sub='u-1') and
    stub the per-request user re-validation (token_version / active check) so the
    report path needs no live DB."""
    monkeypatch.setattr(
        "src.backend.core.run_registry.get_run_registry",
        lambda: _StubRegistry(owner),
    )
    monkeypatch.setattr(jwt_utils._admin_repo, "get_by_id", lambda uid: _db_row())


def test_reports_no_token_enforced_denied(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENFORCED", True)
    denied = jwt_utils.authorize_report_access(_FakeRequest(), "run-x")
    assert denied is not None
    assert denied.status_code == 401


def test_reports_no_token_permissive_allows(monkeypatch):
    """Mirrors require_user's AUTH_ENFORCED=False escape hatch."""
    monkeypatch.setattr(settings, "AUTH_ENFORCED", False)
    assert jwt_utils.authorize_report_access(_FakeRequest(), "run-x") is None


def test_reports_valid_bearer_allows(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENFORCED", True)
    _allow_ownership(monkeypatch)
    req = _FakeRequest(authorization=f"Bearer {_token()}")
    assert jwt_utils.authorize_report_access(req, "run-x") is None


def test_reports_bearer_scheme_is_case_insensitive(monkeypatch):
    """RFC 7235: the auth scheme is case-insensitive — same parse as HTTPBearer."""
    monkeypatch.setattr(settings, "AUTH_ENFORCED", True)
    _allow_ownership(monkeypatch)
    req = _FakeRequest(authorization=f"bearer {_token()}")
    assert jwt_utils.authorize_report_access(req, "run-x") is None


def test_reports_valid_cookie_allows(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENFORCED", True)
    _allow_ownership(monkeypatch)
    req = _FakeRequest(cookie=_token())
    assert jwt_utils.authorize_report_access(req, "run-x") is None


def test_reports_invalid_token_denied_even_when_not_enforced(monkeypatch):
    """A PRESENT but broken token is a client error regardless of the flag."""
    monkeypatch.setattr(settings, "AUTH_ENFORCED", False)
    for req in (
        _FakeRequest(authorization="Bearer not-a-jwt"),
        _FakeRequest(cookie="not-a-jwt"),
    ):
        denied = jwt_utils.authorize_report_access(req, "run-x")
        assert denied is not None
        assert denied.status_code == 401


def test_reports_revoked_token_denied(monkeypatch):
    """A still-unexpired token whose token_version was bumped (logout-all) is
    denied on the report path too — even for the run's own owner."""
    monkeypatch.setattr(settings, "AUTH_ENFORCED", True)
    monkeypatch.setattr(
        "src.backend.core.run_registry.get_run_registry",
        lambda: _StubRegistry("u-1"),  # caller owns the run
    )
    monkeypatch.setattr(
        jwt_utils._admin_repo, "get_by_id",
        lambda uid: {**_db_row(), "token_version": 5},  # bumped since mint
    )
    req = _FakeRequest(authorization=f"Bearer {_token()}")
    denied = jwt_utils.authorize_report_access(req, "run-x")
    assert denied is not None
    assert denied.status_code == 401

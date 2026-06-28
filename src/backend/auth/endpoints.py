"""
Authentication endpoints: register, login, me, logout, Google OAuth, forgot (stub).

Mints HS256 JWTs the SPA stores and sends as a Bearer token. Google SSO uses a
server-side flow with an httpOnly state cookie for CSRF; on success the backend
redirects to FRONTEND_URL/auth/callback#token=<jwt> (token in the URL fragment
so it is never sent to a server or written to access logs).

Referenced by: main.py (auth_router registration).
Depends on: auth/repository.py, auth/jwt_utils.py, auth/google_oauth.py.
"""

import logging
import re
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, field_validator

from src.backend.auth import google_oauth
from src.backend.auth.jwt_utils import (
    REPORT_TOKEN_COOKIE,
    create_access_token,
    get_current_user,
    require_admin,
)
from src.backend.auth.rate_limit import auth_rate_limit
from src.backend.auth.org_repository import OrgRepository
from src.backend.auth.repository import (
    AccountInactive,
    EmailAlreadyExists,
    PasswordTooLong,
    UserRepository,
)
from src.backend.config.logging_config import sanitize_for_log
from src.backend.core.config import settings

logger = logging.getLogger(__name__)

auth_router = APIRouter(prefix="/auth", tags=["auth"])
_repo = UserRepository()
_org_repo = OrgRepository()

_OAUTH_STATE_COOKIE = "oauth_state"
_MIN_PASSWORD_LEN = 8
_MAX_EMAIL_LEN = 254  # RFC 5321 bound; also caps regex backtracking (ReDoS)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _normalize_email(v: str) -> str:
    """Lowercase/validate an email. The regex rejects all whitespace and
    control characters, so a validated email is safe to log (no log-line
    injection) and to compare against stored rows."""
    v = v.strip().lower()
    if len(v) > _MAX_EMAIL_LEN or not _EMAIL_RE.match(v):
        raise ValueError("Enter a valid email")
    return v


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email: str
    password: str
    display_name: str = ""

    @field_validator("email")
    @classmethod
    def _email_ok(cls, v: str) -> str:
        return _normalize_email(v)

    @field_validator("password")
    @classmethod
    def _password_ok(cls, v: str) -> str:
        if len(v) < _MIN_PASSWORD_LEN:
            raise ValueError(f"Password must be at least {_MIN_PASSWORD_LEN} characters")
        # bcrypt hard limit — hashpw raises on longer input (UTF-8 bytes, not chars)
        if len(v.encode("utf-8")) > 72:
            raise ValueError("Password must be at most 72 bytes")
        return v


class LoginRequest(BaseModel):
    email: str
    password: str


class ForgotPasswordRequest(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _email_ok(cls, v: str) -> str:
        return _normalize_email(v)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _user_public(row: dict) -> dict:
    """Shape a DB row into the JSON the frontend consumes (no secrets)."""
    return {
        "id": str(row["id"]),
        "email": row["email"],
        "display_name": row.get("display_name", ""),
        "role": row.get("role", "user"),
    }


def _token_payload(row: dict) -> dict:
    user = _user_public(row)
    orgs = _org_repo.get_orgs_for_user(str(row["id"]))
    primary = orgs[0] if orgs else {}
    token = create_access_token(
        {
            "id": user["id"],
            "email": user["email"],
            "role": user["role"],
            "display_name": user["display_name"],
            "org_id": primary.get("org_id"),
            "org_role": primary.get("org_role"),
            "token_version": row.get("token_version", 0),
        }
    )
    return {"access_token": token, "token_type": "bearer", "user": user}


def _frontend_redirect(fragment_or_query: str) -> RedirectResponse:
    return RedirectResponse(f"{settings.FRONTEND_URL}{fragment_or_query}")


def _set_report_cookie(response: Response, token: str) -> None:
    """Attach the JWT as an httpOnly cookie scoped to Path=/reports.

    Report links open as plain browser navigations (no Authorization header),
    so this cookie is what authenticates them. Path-scoping means the browser
    sends it ONLY on /reports/* requests — it never rides along on API calls.
    Lifetime matches the JWT, so both expire together.
    """
    response.set_cookie(
        REPORT_TOKEN_COOKIE, token,
        max_age=settings.JWT_EXPIRY_HOURS * 3600, path="/reports",
        httponly=True, samesite="lax", secure=settings.COOKIE_SECURE,
    )


# --------------------------------------------------------------------------
# Email / password
# --------------------------------------------------------------------------

@auth_router.post("/register", status_code=201)
@auth_rate_limit()
async def register(request: Request, req: RegisterRequest, response: Response):
    try:
        row = _repo.create_user(req.email, req.password, req.display_name)
    except EmailAlreadyExists:
        raise HTTPException(status_code=409, detail="Email already registered")
    except PasswordTooLong as exc:
        # RegisterRequest already rejects >72-byte passwords (422), but the
        # repo's bcrypt length guard must surface as a user error, never a 500.
        raise HTTPException(status_code=400, detail=str(exc))
    logger.info("[AUTH] Registered user %s (role=%s)",
                sanitize_for_log(row["email"]), row["role"])
    _org_repo.ensure_personal_org(str(row["id"]), row["email"])
    payload = _token_payload(row)
    _set_report_cookie(response, payload["access_token"])
    return payload


@auth_router.post("/login")
@auth_rate_limit()
async def login(request: Request, req: LoginRequest, response: Response):
    row = _repo.verify_credentials(req.email, req.password)
    if not row:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    payload = _token_payload(row)
    _set_report_cookie(response, payload["access_token"])
    return payload


@auth_router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    """Re-read the user from the DB so role/name reflect current state."""
    row = _repo.get_by_id(user["user_id"])
    if not row or not row.get("is_active"):
        raise HTTPException(status_code=401, detail="User not found or inactive")
    if row.get("token_version", 0) != user.get("token_version", 0):
        # token was revoked by a logout-all / password change after it was minted
        raise HTTPException(status_code=401, detail="Token revoked")
    return _user_public(row)


@auth_router.post("/logout")
async def logout(response: Response):
    """Stateless JWT — the client discards the token; we clear the report cookie."""
    response.delete_cookie(REPORT_TOKEN_COOKIE, path="/reports")
    return {"status": "ok"}


@auth_router.post("/logout-all")
async def logout_all(response: Response, user: dict = Depends(get_current_user)):
    """Revoke every token previously minted for this user by bumping
    token_version. All existing tokens (this device and any other) fail the
    token_version re-check on the next request to any authenticated route
    (require_user / require_admin / report access) immediately. The caller must
    log in again to obtain a fresh token."""
    _repo.bump_token_version(user["user_id"])
    response.delete_cookie(REPORT_TOKEN_COOKIE, path="/reports")
    return {"status": "ok"}


@auth_router.post("/forgot-password")
@auth_rate_limit()
async def forgot_password(request: Request, req: ForgotPasswordRequest):
    """Stub (no email service yet). Always returns the same message so it never
    reveals whether an email is registered."""
    # The address is deliberately NOT logged: even validated, it is the one
    # user-controlled value flowing straight to a log sink here (Sonar S5145),
    # and a stub that sends no email has no operational need for it.
    logger.info("[AUTH] Password reset requested (stub — no email sent)")
    return {"status": "ok", "message": "If that email exists, a reset link has been sent."}


# --------------------------------------------------------------------------
# Google SSO (server-side OAuth2)
# --------------------------------------------------------------------------

@auth_router.get("/google/login")
async def google_login():
    if not google_oauth.google_enabled():
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")
    url, state = google_oauth.build_authorization_url()
    resp = RedirectResponse(url)
    # SameSite=Lax so the cookie survives Google's top-level redirect back to us.
    # Secure flag follows settings.COOKIE_SECURE (False for http dev, True behind HTTPS).
    resp.set_cookie(
        _OAUTH_STATE_COOKIE, state, max_age=600,
        httponly=True, samesite="lax", secure=settings.COOKIE_SECURE,
    )
    return resp


class _RoleUpdate(BaseModel):
    role: Literal["admin", "user"]


@auth_router.post("/admin/users/{user_id}/role")
def set_user_platform_role(
    user_id: str,
    body: _RoleUpdate,
    request: Request,
    admin: dict = Depends(require_admin),
):
    """Platform-admin grants/revokes another user's platform-admin role.

    role is validated to {'admin','user'} by the _RoleUpdate model (invalid
    values are rejected with 422 before this body runs)."""
    # Capture the role BEFORE the change so the audit floor's detail records the
    # transition (defaults to 'user' if the row is missing — the update below
    # then 404s and no detail is set).
    existing = _repo.get_by_id(user_id)
    old_role = (existing or {}).get("role", "user")
    if user_id == admin["user_id"] and body.role == "user":
        raise HTTPException(status_code=400, detail="cannot revoke your own platform-admin")
    row = _repo.set_platform_role(user_id, body.role)
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")
    # Enrichment hook read by the audit floor (main.py request_id_middleware):
    # the floor writes the audit row; this only exposes WHAT changed.
    request.state.audit_detail = {"from": old_role, "to": body.role}
    return _user_public(row)


@auth_router.get("/google/callback")
async def google_callback(request: Request):
    if not google_oauth.google_enabled():
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")

    def _error_redirect(code: str) -> RedirectResponse:
        # quote(): the value lands in a redirect URL — Google's own error codes
        # are plain tokens, but the param is attacker-suppliable, so never
        # interpolate it unencoded. The state cookie is single-use: drop it on
        # every exit, not just success.
        resp = _frontend_redirect(f"/login?error={quote(code, safe='')}")
        resp.delete_cookie(_OAUTH_STATE_COOKIE)
        return resp

    if request.query_params.get("error"):
        return _error_redirect(request.query_params["error"])

    state_cookie = request.cookies.get(_OAUTH_STATE_COOKIE)
    state_param = request.query_params.get("state")
    if not state_cookie or state_cookie != state_param:
        return _error_redirect("invalid_state")

    try:
        profile = google_oauth.exchange_code(str(request.url), state_cookie)
    except Exception as exc:  # noqa: BLE001 — map any failure to a clean redirect
        logger.warning("[AUTH] Google code exchange failed: %s", exc)
        return _error_redirect("google_failed")

    email = profile.get("email")
    if not email or not profile.get("email_verified", False):
        return _error_redirect("email_unverified")

    try:
        row = _repo.get_or_create_google_user(
            google_sub=profile["sub"], email=email, display_name=profile.get("name", ""),
        )
    except EmailAlreadyExists:
        return _error_redirect("email_exists")
    except AccountInactive:
        return _error_redirect("account_disabled")
    _org_repo.ensure_personal_org(str(row["id"]), row["email"])
    token = _token_payload(row)["access_token"]
    # NOTE: land on /oauth/callback (NOT /auth/callback) — the SPA dev proxy and
    # the nginx container both forward /auth/* to this backend, so a /auth/*
    # client route would 404 here instead of reaching the React router.
    resp = _frontend_redirect(f"/oauth/callback#token={token}")
    resp.delete_cookie(_OAUTH_STATE_COOKIE)
    _set_report_cookie(resp, token)
    logger.info("[AUTH] Google login for %s (role=%s)", row["email"], row["role"])
    return resp

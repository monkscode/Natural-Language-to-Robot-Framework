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

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, field_validator

from src.backend.auth import google_oauth
from src.backend.auth.jwt_utils import create_access_token, get_current_user
from src.backend.auth.repository import AccountInactive, EmailAlreadyExists, UserRepository
from src.backend.core.config import settings

logger = logging.getLogger(__name__)

auth_router = APIRouter(prefix="/auth", tags=["auth"])
_repo = UserRepository()

_OAUTH_STATE_COOKIE = "oauth_state"
_MIN_PASSWORD_LEN = 8
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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
        v = v.strip().lower()
        if not _EMAIL_RE.match(v):
            raise ValueError("Enter a valid email")
        return v

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
    token = create_access_token(
        {
            "id": user["id"],
            "email": user["email"],
            "role": user["role"],
            "display_name": user["display_name"],
        }
    )
    return {"access_token": token, "token_type": "bearer", "user": user}


def _frontend_redirect(fragment_or_query: str) -> RedirectResponse:
    return RedirectResponse(f"{settings.FRONTEND_URL}{fragment_or_query}")


# --------------------------------------------------------------------------
# Email / password
# --------------------------------------------------------------------------

@auth_router.post("/register", status_code=201)
async def register(req: RegisterRequest):
    try:
        row = _repo.create_user(req.email, req.password, req.display_name)
    except EmailAlreadyExists:
        raise HTTPException(status_code=409, detail="Email already registered")
    logger.info("[AUTH] Registered user %s (role=%s)", row["email"], row["role"])
    return _token_payload(row)


@auth_router.post("/login")
async def login(req: LoginRequest):
    row = _repo.verify_credentials(req.email, req.password)
    if not row:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return _token_payload(row)


@auth_router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    """Re-read the user from the DB so role/name reflect current state."""
    row = _repo.get_by_id(user["user_id"])
    if not row or not row.get("is_active"):
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return _user_public(row)


@auth_router.post("/logout")
async def logout():
    """Stateless JWT — the client discards the token. Endpoint exists for symmetry."""
    return {"status": "ok"}


@auth_router.post("/forgot-password")
async def forgot_password(req: ForgotPasswordRequest):
    """Stub (no email service yet). Always returns the same message so it never
    reveals whether an email is registered."""
    logger.info("[AUTH] Password reset requested for %s (stub — no email sent)", req.email)
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


@auth_router.get("/google/callback")
async def google_callback(request: Request):
    if not google_oauth.google_enabled():
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")

    if request.query_params.get("error"):
        return _frontend_redirect(f"/login?error={request.query_params['error']}")

    state_cookie = request.cookies.get(_OAUTH_STATE_COOKIE)
    state_param = request.query_params.get("state")
    if not state_cookie or state_cookie != state_param:
        return _frontend_redirect("/login?error=invalid_state")

    try:
        profile = google_oauth.exchange_code(str(request.url), state_cookie)
    except Exception as exc:  # noqa: BLE001 — map any failure to a clean redirect
        logger.warning("[AUTH] Google code exchange failed: %s", exc)
        return _frontend_redirect("/login?error=google_failed")

    email = profile.get("email")
    if not email or not profile.get("email_verified", False):
        return _frontend_redirect("/login?error=email_unverified")

    try:
        row = _repo.get_or_create_google_user(
            google_sub=profile["sub"], email=email, display_name=profile.get("name", ""),
        )
    except EmailAlreadyExists:
        return _frontend_redirect("/login?error=email_exists")
    except AccountInactive:
        return _frontend_redirect("/login?error=account_disabled")
    token = _token_payload(row)["access_token"]
    # NOTE: land on /oauth/callback (NOT /auth/callback) — the SPA dev proxy and
    # the nginx container both forward /auth/* to this backend, so a /auth/*
    # client route would 404 here instead of reaching the React router.
    resp = _frontend_redirect(f"/oauth/callback#token={token}")
    resp.delete_cookie(_OAUTH_STATE_COOKIE)
    logger.info("[AUTH] Google login for %s (role=%s)", row["email"], row["role"])
    return resp

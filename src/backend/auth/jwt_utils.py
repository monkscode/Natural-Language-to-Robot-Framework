"""
JWT minting + FastAPI auth dependencies.

create_access_token mints an HS256 token the SPA stores and sends as a Bearer
header. Three dependencies guard endpoints:

- get_current_user  — STRICT: always requires a valid token (used by /auth/me).
                      Stateless (no DB) — /auth/me re-reads the row itself.
- require_user       — requires a valid token. Stateless (no DB; hot path).
                       settings.AUTH_ENFORCED=False is an escape hatch for
                       local API-only debugging: token-less requests pass
                       through (returns the user if a token is present, None
                       if absent).
- require_admin      — always strict: valid token + role=='admin' re-validated
                       against the users table per request (revocation and
                       role changes apply immediately). The escape hatch does
                       not apply to admin routes.

A token that is PRESENT but invalid/expired always yields 401, even when not
enforced (a broken token is a client error regardless of the flag).

Referenced by: auth/endpoints.py, api/endpoints.py, api/history_endpoints.py,
api/report_endpoints.py (the /reports ownership gate), main.py (router guards).
Depends on: PyJWT, src/backend/core/config.py (JWT_SECRET_KEY, JWT_EXPIRY_HOURS,
AUTH_ENFORCED), core/run_registry.py (lazy, report ownership).
"""

import logging
from datetime import datetime, timedelta, timezone

import jwt
import psycopg
from fastapi import Depends, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.backend.auth.repository import UserRepository
from src.backend.core.config import settings

logger = logging.getLogger(__name__)

# Stateless repository shared by require_admin's per-request DB re-validation.
_admin_repo = UserRepository()

_ALGORITHM = "HS256"
# auto_error=False: missing header -> credentials is None (we decide the status),
# so absent-token can be permissive during coexistence instead of a blanket 403.
_bearer = HTTPBearer(auto_error=False)
_UNAUTH_HEADERS = {"WWW-Authenticate": "Bearer"}


def create_access_token(user: dict) -> str:
    """Mint an HS256 access token. `user` needs id, email, role, display_name."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user["id"]),
        "email": user.get("email", ""),
        "role": user.get("role", "user"),
        "name": user.get("display_name", ""),
        "org_id": user.get("org_id"),
        "org_role": user.get("org_role"),
        "iat": now,
        "exp": now + timedelta(hours=settings.JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=_ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode a raw JWT string to a user dict. Raises 401 on invalid/expired."""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired", headers=_UNAUTH_HEADERS)
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid token", headers=_UNAUTH_HEADERS)
    return {
        "user_id": payload.get("sub", ""),
        "email": payload.get("email", ""),
        "role": payload.get("role", "user"),
        "name": payload.get("name", ""),
        "org_id": payload.get("org_id"),
        "org_role": payload.get("org_role"),
    }


def _decode_to_user(credentials: HTTPAuthorizationCredentials | None) -> dict | None:
    """Decode a present token to a user dict; None if no token. Raises 401 if a
    token is present but invalid/expired."""
    if credentials is None:
        return None
    return decode_token(credentials.credentials)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """STRICT dependency — always requires a valid token. Used by /auth/me."""
    user = _decode_to_user(credentials)
    if user is None:
        raise HTTPException(401, "Not authenticated", headers=_UNAUTH_HEADERS)
    return user


def require_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict | None:
    """Requires a valid token unless settings.AUTH_ENFORCED is disabled."""
    user = _decode_to_user(credentials)
    if settings.AUTH_ENFORCED and user is None:
        raise HTTPException(401, "Not authenticated", headers=_UNAUTH_HEADERS)
    return user


def is_validated_admin(user: dict | None) -> bool:
    """True only when the token claims admin AND the users row still says
    admin + active. Mirrors require_admin's per-request DB re-validation but
    as a boolean (no raise) so callers can fall back to user-scoped behaviour.
    Fails CLOSED: any storage error means "not admin"."""
    if not user or user.get("role") != "admin":
        return False
    try:
        row = _admin_repo.get_by_id(user["user_id"])
    except Exception as exc:
        logger.warning("[AUTH] admin re-validation unavailable: %s", exc)
        return False
    return bool(row and row.get("is_active") and row.get("role") == "admin")


# --------------------------------------------------------------------------
# Report access (owner-gated /reports route)
# --------------------------------------------------------------------------
# Robot Framework log.html records every keyword argument (including text typed
# into password fields), so /reports must sit behind auth. The SPA opens reports
# as plain <a target="_blank"> navigations, which cannot carry an Authorization
# header — so login/register/Google-callback also set the JWT in an httpOnly
# cookie scoped to Path=/reports. This checker accepts either credential and
# mirrors require_user semantics: token-less requests pass only when
# settings.AUTH_ENFORCED is off; a present-but-invalid token is always 401.
#
# Beyond authentication, access is per-OWNER: /reports/{run_id}/* is served
# only to the user who ran it (test_runs.user_id) or to a validated admin.
# Unattributed runs (legacy rows, unknown ids) are admin-only — fail closed.
REPORT_TOKEN_COOKIE = "mark1_report_token"


def authorize_report_access(request, run_id: str) -> "JSONResponse | None":
    """Owner-or-admin gate for GET/HEAD /reports/{run_id}/*. Returns a 401/403
    JSONResponse to deny, or None to allow. Never raises (the report route does
    no HTTPException handling around this; a raise would surface as a 500 with a
    filesystem path in the body).

    run_id is the route's parsed path parameter — NOT re-parsed from the URL.
    The ownership check and the artifact store lookup (store.serve_artifact)
    therefore key off one identical value, which closes the '//' (empty segment)
    and '..' (parent traversal) parser-disagreement bypass that the old
    middleware + StaticFiles split allowed.
    """
    # Scheme is case-insensitive (RFC 7235) — same parse as FastAPI's
    # HTTPBearer, so a client that works on the API works here too.
    scheme, _, param = request.headers.get("Authorization", "").partition(" ")
    token: str | None = param if scheme.lower() == "bearer" and param else None
    if not token:
        token = request.cookies.get(REPORT_TOKEN_COOKIE)

    if not token:
        if settings.AUTH_ENFORCED:
            return JSONResponse(
                {"detail": "Not authenticated"}, status_code=401, headers=_UNAUTH_HEADERS
            )
        return None
    try:
        user = decode_token(token)
    except HTTPException as exc:
        return JSONResponse(
            {"detail": exc.detail}, status_code=exc.status_code, headers=_UNAUTH_HEADERS
        )

    admin = is_validated_admin(user)
    try:
        # Lazy import: jwt_utils loads during early app wiring; the registry
        # opens a DB pool on first use and must not do so at import time.
        from src.backend.core.run_registry import get_run_registry
        owner_id, org_id = get_run_registry().get_run_owner(run_id)
    except Exception as exc:
        logger.warning("[AUTH] report ownership lookup unavailable: %s", exc)
        owner_id, org_id = None, None  # fail closed

    from src.backend.auth.ownership import caller_can_read
    if caller_can_read(user, owner_id, org_id, is_platform_admin=admin):
        return None
    return JSONResponse(
        {"detail": "You do not have access to this report"}, status_code=403
    )


def require_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """Admin guard: valid token + role=='admin' **re-read from the DB**. The
    AUTH_ENFORCED escape hatch does NOT apply — admin routes are never open.

    Unlike require_user (stateless, on the hot generate path), admin access is
    re-validated against the users table on every request: deactivating or
    demoting an admin takes effect immediately instead of riding out the
    token's remaining lifetime — and a promotion is picked up without
    re-login. One primary-key SELECT per request via the pool; every
    admin-guarded route is a DB-backed dashboard anyway. Fails CLOSED when the
    auth store is unreachable (503).
    """
    user = _decode_to_user(credentials)
    if user is None:
        raise HTTPException(401, "Not authenticated", headers=_UNAUTH_HEADERS)
    try:
        row = _admin_repo.get_by_id(user["user_id"])
    except psycopg.DataError:
        # sub is not a valid UUID — not an identity we ever minted.
        raise HTTPException(401, "Invalid token", headers=_UNAUTH_HEADERS)
    except Exception as exc:
        logger.warning("[AUTH] admin re-validation unavailable: %s", exc)
        raise HTTPException(503, "Authentication store unavailable")
    if row is None or not row.get("is_active"):
        raise HTTPException(401, "User not found or inactive", headers=_UNAUTH_HEADERS)
    if row.get("role") != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    # Current DB state wins over the (possibly stale) token claims.
    user["role"] = row["role"]
    user["email"] = row.get("email", user["email"])
    return user

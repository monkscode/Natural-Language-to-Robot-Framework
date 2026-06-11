"""
JWT minting + FastAPI auth dependencies.

create_access_token mints an HS256 token the SPA stores and sends as a Bearer
header. Three dependencies guard endpoints; verification is stateless (no DB):

- get_current_user  — STRICT: always requires a valid token (used by /auth/me).
- require_user       — requires a valid token. settings.AUTH_ENFORCED=False is
                       an escape hatch for local API-only debugging: token-less
                       requests pass through (returns the user if a token is
                       present, None if absent).
- require_admin      — like require_user, plus a role=='admin' check when enforced.

A token that is PRESENT but invalid/expired always yields 401, even when not
enforced (a broken token is a client error regardless of the flag).

Referenced by: auth/endpoints.py, api/endpoints.py, main.py (router guards).
Depends on: PyJWT, src/backend/core/config.py (JWT_SECRET_KEY, JWT_EXPIRY_HOURS, AUTH_ENFORCED).
"""

import logging
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.backend.core.config import settings

logger = logging.getLogger(__name__)

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
        "iat": now,
        "exp": now + timedelta(hours=settings.JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=_ALGORITHM)


def _decode_to_user(credentials: HTTPAuthorizationCredentials | None) -> dict | None:
    """Decode a present token to a user dict; None if no token. Raises 401 if a
    token is present but invalid/expired."""
    if credentials is None:
        return None
    try:
        payload = jwt.decode(
            credentials.credentials, settings.JWT_SECRET_KEY, algorithms=[_ALGORITHM]
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired", headers=_UNAUTH_HEADERS)
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid token", headers=_UNAUTH_HEADERS)
    return {
        "user_id": payload.get("sub", ""),
        "email": payload.get("email", ""),
        "role": payload.get("role", "user"),
        "name": payload.get("name", ""),
    }


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


def require_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict | None:
    """Admin guard: valid token + role=='admin', unless settings.AUTH_ENFORCED
    is disabled."""
    user = _decode_to_user(credentials)
    if not settings.AUTH_ENFORCED:
        return user
    if user is None:
        raise HTTPException(401, "Not authenticated", headers=_UNAUTH_HEADERS)
    if user.get("role") != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return user

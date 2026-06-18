"""
Google OAuth2/OIDC helper (server-side flow) built on Authlib.

build_authorization_url() starts the flow and returns (url, state); the caller
round-trips `state` via an httpOnly cookie for CSRF. exchange_code() swaps the
callback URL for tokens and returns the verified Google profile from the OIDC
userinfo endpoint (fetched server-side with the access token). The flow is
disabled gracefully when GOOGLE_CLIENT_ID/SECRET are unset (google_enabled()).

Referenced by: auth/endpoints.py.
Depends on: authlib, src/backend/core/config.py (GOOGLE_* settings).
"""

import logging

from authlib.integrations.requests_client import OAuth2Session

from src.backend.core.config import settings

logger = logging.getLogger(__name__)

_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105 (public URL)
_USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
_SCOPE = "openid email profile"


def google_enabled() -> bool:
    """True only when both Google client credentials are configured."""
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)


def _session() -> OAuth2Session:
    return OAuth2Session(
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        scope=_SCOPE,
        redirect_uri=settings.GOOGLE_REDIRECT_URI,
    )


def build_authorization_url() -> tuple[str, str]:
    """Return (authorization_url, state). Round-trip `state` for CSRF defense."""
    url, state = _session().create_authorization_url(
        _AUTH_ENDPOINT,
        access_type="offline",
        prompt="select_account",
    )
    return url, state


def exchange_code(authorization_response_url: str, state: str) -> dict:
    """Exchange the callback URL for tokens; return the Google profile.

    Returns the userinfo dict: {sub, email, email_verified, name, picture, ...}.
    Raises on token-exchange or userinfo failure (caller maps to a redirect).
    """
    client = _session()
    client.fetch_token(
        _TOKEN_ENDPOINT,
        authorization_response=authorization_response_url,
        state=state,
    )
    resp = client.get(_USERINFO_ENDPOINT)
    resp.raise_for_status()
    return resp.json()

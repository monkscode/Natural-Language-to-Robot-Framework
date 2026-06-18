"""Unit tests for the Google OAuth helper (auth/google_oauth.py).

The OAuth2Session is mocked — no Google round-trip. What matters here is the
contract endpoints.py relies on: the session is built from settings, the
state round-trips, and a failed token exchange or userinfo fetch RAISES
(endpoints.py maps that to the google_failed redirect) instead of returning
a half-baked profile.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.backend.auth import google_oauth
from src.backend.core.config import settings


@pytest.fixture
def oauth_session():
    """Patch OAuth2Session where google_oauth uses it; yield the mock instance."""
    with patch.object(google_oauth, "OAuth2Session") as cls:
        yield cls


def test_google_enabled_requires_both_credentials():
    with patch.object(settings, "GOOGLE_CLIENT_ID", "cid"), \
         patch.object(settings, "GOOGLE_CLIENT_SECRET", "sec"):
        assert google_oauth.google_enabled() is True
    with patch.object(settings, "GOOGLE_CLIENT_ID", "cid"), \
         patch.object(settings, "GOOGLE_CLIENT_SECRET", ""):
        assert google_oauth.google_enabled() is False
    with patch.object(settings, "GOOGLE_CLIENT_ID", ""), \
         patch.object(settings, "GOOGLE_CLIENT_SECRET", "sec"):
        assert google_oauth.google_enabled() is False


def test_build_authorization_url_returns_url_and_state(oauth_session):
    session = oauth_session.return_value
    session.create_authorization_url.return_value = ("https://google/auth?x=1", "state-1")
    url, state = google_oauth.build_authorization_url()
    assert url == "https://google/auth?x=1"
    assert state == "state-1"
    # Session must carry the configured client credentials + redirect URI.
    kwargs = oauth_session.call_args.kwargs
    assert kwargs["client_id"] == settings.GOOGLE_CLIENT_ID
    assert kwargs["redirect_uri"] == settings.GOOGLE_REDIRECT_URI
    # offline + account chooser are part of the UX contract.
    auth_kwargs = session.create_authorization_url.call_args.kwargs
    assert auth_kwargs == {"access_type": "offline", "prompt": "select_account"}


def test_exchange_code_returns_verified_profile(oauth_session):
    session = oauth_session.return_value
    resp = MagicMock()
    resp.json.return_value = {"sub": "g-1", "email": "a@b.com", "email_verified": True}
    session.get.return_value = resp
    profile = google_oauth.exchange_code("https://cb?code=c&state=s", "s")
    assert profile["sub"] == "g-1"
    # The callback URL and state must both reach fetch_token (CSRF check).
    kwargs = session.fetch_token.call_args.kwargs
    assert kwargs["authorization_response"] == "https://cb?code=c&state=s"
    assert kwargs["state"] == "s"
    resp.raise_for_status.assert_called_once()


def test_exchange_code_raises_when_token_exchange_fails(oauth_session):
    session = oauth_session.return_value
    session.fetch_token.side_effect = RuntimeError("invalid_grant")
    with pytest.raises(RuntimeError, match="invalid_grant"):
        google_oauth.exchange_code("https://cb?code=bad", "s")


def test_exchange_code_raises_when_userinfo_fails(oauth_session):
    session = oauth_session.return_value
    resp = MagicMock()
    resp.raise_for_status.side_effect = RuntimeError("503 from userinfo")
    session.get.return_value = resp
    with pytest.raises(RuntimeError, match="503"):
        google_oauth.exchange_code("https://cb?code=c", "s")

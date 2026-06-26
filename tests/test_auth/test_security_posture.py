"""Unit tests for the startup security-posture guard (DB-free).

Verifies that a production deployment refuses to boot with a forgeable JWT
secret or non-Secure auth cookies, while development only warns so local http
dev keeps working.
"""
import pytest

from src.backend.auth import security_posture as sp

_STRONG = "k7Qw9Z2pX4mN8sV1tR6yB3uC0aF5gH7jL2dE4wQ9zP1xM8nS3vT6yB0uC2aF4gH"


def _set(monkeypatch, *, environment, secret, cookie_secure):
    monkeypatch.setattr(sp.settings, "ENVIRONMENT", environment)
    monkeypatch.setattr(sp.settings, "JWT_SECRET_KEY", secret)
    monkeypatch.setattr(sp.settings, "COOKIE_SECURE", cookie_secure)


# --- Always fatal: placeholder/empty secret, in any environment ---

@pytest.mark.parametrize("environment", ["development", "production"])
@pytest.mark.parametrize("secret", ["", "change-me-in-production"])
def test_placeholder_secret_is_always_fatal(monkeypatch, environment, secret):
    _set(monkeypatch, environment=environment, secret=secret, cookie_secure=True)
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        sp.validate_security_posture()


# --- Development: weak secret is allowed (warns, does not raise) ---

def test_dev_allows_weak_secret(monkeypatch, caplog):
    _set(monkeypatch, environment="development",
         secret="dev-only-local-secret-change-in-prod", cookie_secure=False)
    sp.validate_security_posture()  # must not raise
    assert any("weak" in r.message.lower() for r in caplog.records)


# --- Production: weak secret is fatal ---

def test_prod_rejects_short_secret(monkeypatch):
    _set(monkeypatch, environment="production", secret="short", cookie_secure=True)
    with pytest.raises(RuntimeError, match="weak"):
        sp.validate_security_posture()


def test_prod_rejects_dev_marker_secret(monkeypatch):
    # 32+ chars but contains an obvious dev marker.
    _set(monkeypatch, environment="production",
         secret="dev-only-local-secret-change-in-prod", cookie_secure=True)
    with pytest.raises(RuntimeError, match="weak"):
        sp.validate_security_posture()


# --- Production: insecure cookies are fatal ---

def test_prod_requires_cookie_secure(monkeypatch):
    _set(monkeypatch, environment="production", secret=_STRONG, cookie_secure=False)
    with pytest.raises(RuntimeError, match="COOKIE_SECURE"):
        sp.validate_security_posture()


# --- Production: strong secret + secure cookies boots cleanly ---

def test_prod_strong_posture_passes(monkeypatch):
    _set(monkeypatch, environment="production", secret=_STRONG, cookie_secure=True)
    sp.validate_security_posture()  # must not raise

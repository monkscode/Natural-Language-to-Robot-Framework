"""Unit tests for the startup security-posture guard (DB-free).

Verifies that a production deployment refuses to boot with a forgeable JWT
secret or non-Secure auth cookies, while development provisions a strong secret
of its own and only warns, so local http dev keeps working out of the box.
"""
from unittest.mock import patch

import pytest

from src.backend.auth import security_posture as sp

_STRONG = "k7Qw9Z2pX4mN8sV1tR6yB3uC0aF5gH7jL2dE4wQ9zP1xM8nS3vT6yB0uC2aF4gH"
_PLACEHOLDERS = ["", "change-me-in-production"]


def _set(monkeypatch, *, environment, secret, cookie_secure):
    monkeypatch.setattr(sp.settings, "ENVIRONMENT", environment)
    monkeypatch.setattr(sp.settings, "JWT_SECRET_KEY", secret)
    monkeypatch.setattr(sp.settings, "COOKIE_SECURE", cookie_secure)


@pytest.fixture
def secret_file(tmp_path):
    """Redirect the persisted development secret into a temp dir."""
    path = tmp_path / "jwt_secret"
    with patch.object(sp, "_SECRET_PATH", path):
        yield path


# --- Development: a placeholder is provisioned, not fatal ---

@pytest.mark.parametrize("secret", _PLACEHOLDERS)
def test_dev_generates_and_persists_a_strong_secret(monkeypatch, secret_file, secret):
    _set(monkeypatch, environment="development", secret=secret, cookie_secure=False)
    sp.validate_security_posture()  # must not raise
    stored = secret_file.read_text(encoding="utf-8")
    assert not sp._secret_is_weak(stored)
    assert sp.settings.JWT_SECRET_KEY == stored


def test_dev_reuses_the_persisted_secret_across_restarts(monkeypatch, secret_file):
    """Tokens must survive a container restart, so the secret must be stable."""
    _set(monkeypatch, environment="development", secret="", cookie_secure=False)
    sp.validate_security_posture()
    first = sp.settings.JWT_SECRET_KEY

    _set(monkeypatch, environment="development", secret="", cookie_secure=False)
    sp.validate_security_posture()
    assert sp.settings.JWT_SECRET_KEY == first


@pytest.mark.parametrize("content", ["", "   ", "short"])
def test_dev_replaces_an_unusable_persisted_secret(monkeypatch, secret_file, content):
    secret_file.write_text(content, encoding="utf-8")
    _set(monkeypatch, environment="development", secret="", cookie_secure=False)
    sp.validate_security_posture()
    assert not sp._secret_is_weak(secret_file.read_text(encoding="utf-8"))


def test_dev_replaces_a_corrupted_persisted_secret(monkeypatch, secret_file):
    """A truncated/binary file must be rewritten, not crash the boot it exists
    to keep working."""
    secret_file.write_bytes(b"\xff\xfe not utf-8")
    _set(monkeypatch, environment="development", secret="", cookie_secure=False)
    sp.validate_security_posture()  # must not raise
    assert not sp._secret_is_weak(secret_file.read_text(encoding="utf-8"))


def test_dev_boots_when_the_secret_cannot_be_persisted(monkeypatch, secret_file, caplog):
    """An unwritable data/ (wrong host uid on Linux) must degrade, not crash-loop."""
    _set(monkeypatch, environment="development", secret="", cookie_secure=False)
    with patch.object(sp, "_write_secret_file", side_effect=OSError("read-only fs")):
        sp.validate_security_posture()  # must not raise
    assert not sp._secret_is_weak(sp.settings.JWT_SECRET_KEY)
    assert not secret_file.exists()
    assert "could not persist" in caplog.text.lower()


def test_concurrent_starters_converge_on_the_first_written_secret(secret_file):
    """Two processes starting at once must sign with the SAME key. The create is
    first-writer-wins, so the loser adopts what already landed rather than
    overwriting it and invalidating the winner's freshly minted tokens."""
    secret_file.write_text(_STRONG, encoding="utf-8")  # the winner got there first
    assert sp._write_secret_file("k9Zt4Bq7Nx2Vm5Ls8Wd1Hf6Zj3Mc0Pg7Tb4Yn1Qs8Xk5Dv2") == _STRONG
    assert secret_file.read_text(encoding="utf-8") == _STRONG


def test_unusable_existing_secret_is_overwritten(secret_file):
    """The adopt-don't-clobber rule must not strand a corrupt file forever."""
    secret_file.write_text("", encoding="utf-8")
    assert sp._write_secret_file(_STRONG) == _STRONG
    assert secret_file.read_text(encoding="utf-8") == _STRONG


def test_generated_secret_never_carries_a_weak_marker():
    """token_urlsafe draws from base64url, so it can emit 'dev_' (~1 in 34,000).
    Persisting that would pass in dev and then fail production for no visible
    reason, so the mint loop must redraw."""
    with patch.object(sp.secrets, "token_urlsafe",
                      side_effect=["dev_" + "a" * 60, _STRONG]):
        assert sp._mint_secret() == _STRONG


# --- Production: a placeholder secret is still fatal ---

@pytest.mark.parametrize("secret", _PLACEHOLDERS)
def test_prod_placeholder_secret_is_fatal(monkeypatch, secret_file, secret):
    _set(monkeypatch, environment="production", secret=secret, cookie_secure=True)
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        sp.validate_security_posture()
    assert not secret_file.exists(), "production must never provision a secret"


def test_prod_placeholder_error_points_at_the_dev_secret_without_leaking_it(
    monkeypatch, secret_file
):
    secret_file.write_text(_STRONG, encoding="utf-8")
    _set(monkeypatch, environment="production", secret="", cookie_secure=True)
    with pytest.raises(RuntimeError) as excinfo:
        sp.validate_security_posture()
    message = str(excinfo.value)
    assert str(secret_file) in message
    assert _STRONG not in message


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

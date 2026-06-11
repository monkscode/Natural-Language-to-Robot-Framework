"""Integration tests for UserRepository against a real PostgreSQL.

Skipped automatically when Postgres is unreachable, so unit-only CI stays green.
Run locally after `docker compose up -d postgres`:
    pytest tests/test_auth/test_repository_integration.py -v
"""

import uuid

import pytest

from src.backend.core.config import settings
from src.backend.auth import db as auth_db
from src.backend.auth.repository import AccountInactive, EmailAlreadyExists, UserRepository

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def repo():
    """Bootstrap the schema, yield (repo, created_emails); clean up rows after."""
    try:
        auth_db.init_auth_db()
    except Exception as exc:  # noqa: BLE001 — any connect failure = skip, not fail
        pytest.skip(f"Postgres unavailable: {exc}")
    created: list[str] = []
    yield UserRepository(), created
    try:
        with auth_db.get_pool().connection() as conn:
            for email in created:
                conn.execute("DELETE FROM users WHERE email = %s", (email.lower(),))
            conn.commit()
    except Exception:  # noqa: BLE001 — best-effort cleanup
        pass


def _unique_email() -> str:
    return f"it-{uuid.uuid4().hex[:12]}@example.com"


def test_create_and_authenticate(repo):
    r, created = repo
    email = _unique_email()
    created.append(email)
    row = r.create_user(email, "S3cretpw!", "Test User")
    assert row["email"] == email
    assert row["role"] == "user"
    assert r.verify_credentials(email, "S3cretpw!") is not None
    assert r.verify_credentials(email, "wrong-password") is None
    assert r.verify_credentials("nobody@example.com", "x") is None


def test_duplicate_email_rejected(repo):
    r, created = repo
    email = _unique_email()
    created.append(email)
    r.create_user(email, "S3cretpw!")
    with pytest.raises(EmailAlreadyExists):
        r.create_user(email, "Another1!")


def test_admin_role_from_settings(repo, monkeypatch):
    r, created = repo
    email = _unique_email()
    created.append(email)
    monkeypatch.setattr(settings, "ADMIN_EMAILS", email)
    row = r.create_user(email, "S3cretpw!")
    assert row["role"] == "admin"


def test_google_user_create_is_idempotent(repo):
    r, created = repo
    email = _unique_email()
    created.append(email)
    sub = f"google-{uuid.uuid4().hex}"
    first = r.get_or_create_google_user(sub, email, "G User")
    second = r.get_or_create_google_user(sub, email, "G User")
    assert first["email"] == email
    assert str(second["id"]) == str(first["id"])


def test_google_login_never_auto_links_existing_email(repo):
    """A same-email password account is NOT proof of ownership — no auto-link."""
    r, created = repo
    email = _unique_email()
    created.append(email)
    r.create_user(email, "S3cretpw!")
    with pytest.raises(EmailAlreadyExists):
        r.get_or_create_google_user(f"google-{uuid.uuid4().hex}", email)


def test_google_login_rejects_inactive_account(repo):
    r, created = repo
    email = _unique_email()
    created.append(email)
    sub = f"google-{uuid.uuid4().hex}"
    r.get_or_create_google_user(sub, email, "G User")
    with auth_db.get_pool().connection() as conn:
        conn.execute("UPDATE users SET is_active = FALSE WHERE email = %s", (email,))
        conn.commit()
    with pytest.raises(AccountInactive):
        r.get_or_create_google_user(sub, email, "G User")

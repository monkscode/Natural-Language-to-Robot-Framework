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

# auth_isolated_schema (conftest) reroutes auth_db to the auth_test schema —
# these tests never touch the live public.users table — and skips the module
# when Postgres is unreachable.
pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


@pytest.fixture(scope="module")
def repo():
    """Yield (repo, created_emails). Rows live in the throwaway auth_test
    schema (dropped at session end), so no per-row cleanup is needed; the
    `created` list is kept only because the tests append to it."""
    created: list[str] = []
    yield UserRepository(), created


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


def test_role_synced_to_allowlist_at_signin(repo, monkeypatch):
    """ADMIN_EMAILS promotes at sign-in (promote-only). Removing an email from
    ADMIN_EMAILS no longer demotes — the DB role is the source of truth; revoke
    via set_platform_role instead."""
    r, created = repo
    email = _unique_email()
    created.append(email)
    assert r.create_user(email, "S3cretpw!")["role"] == "user"
    # Adding the email to ADMIN_EMAILS promotes at next sign-in.
    monkeypatch.setattr(settings, "ADMIN_EMAILS", email)
    assert r.verify_credentials(email, "S3cretpw!")["role"] == "admin"
    # Removing the email from ADMIN_EMAILS does NOT demote — admin stays admin.
    monkeypatch.setattr(settings, "ADMIN_EMAILS", "")
    assert r.verify_credentials(email, "S3cretpw!")["role"] == "admin"


def test_google_only_account_denies_password_login(repo):
    """A google-only account (no password hash) can never password-login; the
    denial burns the bcrypt timing equalizer instead of returning instantly."""
    r, created = repo
    email = _unique_email()
    created.append(email)
    r.get_or_create_google_user(f"google-{uuid.uuid4().hex}", email, "G User")
    assert r.verify_credentials(email, "any-password-1!") is None

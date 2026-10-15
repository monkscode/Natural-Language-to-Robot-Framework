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


def test_google_user_create_is_idempotent(repo):
    r, created = repo
    email = _unique_email()
    created.append(email)
    sub = f"google-{uuid.uuid4().hex}"
    first, first_created = r.get_or_create_google_user(sub, email, "G User")
    second, second_created = r.get_or_create_google_user(sub, email, "G User")
    assert first["email"] == email
    assert str(second["id"]) == str(first["id"])
    assert first_created is True
    assert second_created is False


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
        conn.execute("UPDATE users SET status = 'suspended', is_active = FALSE WHERE email = %s", (email,))
        conn.commit()
    with pytest.raises(AccountInactive):
        r.get_or_create_google_user(sub, email, "G User")


def test_sign_in_never_changes_role(repo, monkeypatch):
    """A listed address signing in stays a pending user: an address is not proof
    the caller owns it, so sign-in never grants admin."""
    r, created = repo
    email = _unique_email()
    created.append(email)
    assert r.create_user(email, "S3cretpw!")["role"] == "user"
    monkeypatch.setattr(settings, "ADMIN_EMAILS", email)
    signed_in = r.verify_credentials(email, "S3cretpw!")
    assert signed_in["role"] == "user"
    assert signed_in["status"] == "pending"
    assert r.get_by_email(email)["role"] == "user"


def test_google_only_account_denies_password_login(repo):
    """A google-only account (no password hash) can never password-login; the
    denial burns the bcrypt timing equalizer instead of returning instantly."""
    r, created = repo
    email = _unique_email()
    created.append(email)
    r.get_or_create_google_user(f"google-{uuid.uuid4().hex}", email, "G User")
    assert r.verify_credentials(email, "any-password-1!") is None


def test_reset_password_sets_new_password(repo):
    r, created = repo
    email = _unique_email()
    created.append(email)
    r.create_user(email, "OldPw123!")
    row = r.reset_password(email, "NewPw456!")
    assert row is not None and row["email"] == email
    assert r.verify_credentials(email, "NewPw456!") is not None
    assert r.verify_credentials(email, "OldPw123!") is None


def test_reset_password_revokes_existing_tokens(repo):
    """The reset must bump token_version so tokens minted before it are revoked —
    the security half of the reset (a compromise response must lock the attacker
    out immediately, not after the JWT expires)."""
    r, created = repo
    email = _unique_email()
    created.append(email)
    before = r.create_user(email, "OldPw123!")
    tv_before = r.get_by_id(before["id"])["token_version"]
    r.reset_password(email, "NewPw456!")
    assert r.get_by_id(before["id"])["token_version"] == tv_before + 1


def test_reset_password_unknown_email_returns_none(repo):
    r, _created = repo
    assert r.reset_password(_unique_email(), "Whatever1!") is None


def test_reset_password_sets_password_on_google_only_account(repo):
    """Reset works for a Google-SSO-only account too — it just sets a hash so the
    user can subsequently password-login."""
    r, created = repo
    email = _unique_email()
    created.append(email)
    r.get_or_create_google_user(f"google-{uuid.uuid4().hex}", email, "G User")
    assert r.verify_credentials(email, "Whatever1!") is None
    r.reset_password(email, "Whatever1!")
    assert r.verify_credentials(email, "Whatever1!") is not None


def test_listed_address_signup_lands_pending_user(repo, monkeypatch):
    """A listed address that self-registers lands a pending user, never an admin."""
    r, created = repo
    email = _unique_email()
    created.append(email)
    monkeypatch.setattr(settings, "ADMIN_EMAILS", email)
    row = r.create_user(email, "S3cretpw!")
    full = r.get_by_id(str(row["id"]))
    assert full["role"] == "user"
    assert full["status"] == "pending"
    assert full["is_active"] is False


def test_google_listed_address_signup_lands_pending_user(repo, monkeypatch):
    """A Google signup of a listed address lands a pending user as well."""
    r, created = repo
    email = _unique_email()
    created.append(email)
    monkeypatch.setattr(settings, "ADMIN_EMAILS", email)
    row, was_created = r.get_or_create_google_user(f"google-{uuid.uuid4().hex}", email, "G Admin")
    assert was_created is True
    full = r.get_by_id(str(row["id"]))
    assert full["role"] == "user"
    assert full["status"] == "pending"
    assert full["is_active"] is False


def test_google_signin_of_existing_account_with_listed_address_stays_pending_user(repo, monkeypatch):
    """Signing in again with a listed address never promotes an existing Google account."""
    r, created = repo
    email = _unique_email()
    created.append(email)
    sub = f"google-{uuid.uuid4().hex}"
    r.get_or_create_google_user(sub, email, "G Admin")
    monkeypatch.setattr(settings, "ADMIN_EMAILS", email)
    row, was_created = r.get_or_create_google_user(sub, email, "G Admin")
    assert was_created is False
    assert (row["role"], row["status"]) == ("user", "pending")
    full = r.get_by_email(email)
    assert (full["role"], full["status"]) == ("user", "pending")

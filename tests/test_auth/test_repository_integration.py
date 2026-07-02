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


def test_admin_email_signup_is_active_immediately(repo, monkeypatch):
    """An ADMIN_EMAILS address that self-registers must land active+admin — the
    owner can never be stuck pending (parent spec §Bootstrap)."""
    r, created = repo
    email = _unique_email()
    created.append(email)
    monkeypatch.setattr(settings, "ADMIN_EMAILS", email)
    row = r.create_user(email, "S3cretpw!")
    full = r.get_by_id(str(row["id"]))
    assert full["role"] == "admin"
    assert full["status"] == "active"
    assert full["is_active"] is True


def test_sync_activates_already_admin_but_pending_row(repo, monkeypatch):
    """A user who is role=admin but somehow still pending is normalised to active
    on their next sign-in (defence in depth for any legacy/raced row)."""
    r, created = repo
    email = _unique_email()
    created.append(email)
    r.create_user(email, "S3cretpw!")  # created as user/pending
    with auth_db.get_pool().connection() as conn:
        conn.execute("UPDATE users SET role='admin', status='pending', is_active=FALSE WHERE email=%s", (email,))
        conn.commit()
    monkeypatch.setattr(settings, "ADMIN_EMAILS", email)
    r.verify_credentials(email, "S3cretpw!")
    full = r.get_by_email(email)
    assert full["status"] == "active" and full["is_active"] is True


def test_google_admin_email_signup_is_active_immediately(repo, monkeypatch):
    """An allowlisted Google signup must also land active+admin, not pending."""
    r, created = repo
    email = _unique_email()
    created.append(email)
    monkeypatch.setattr(settings, "ADMIN_EMAILS", email)
    row, was_created = r.get_or_create_google_user(f"google-{uuid.uuid4().hex}", email, "G Admin")
    assert was_created is True
    full = r.get_by_id(str(row["id"]))
    assert full["role"] == "admin"
    assert full["status"] == "active"
    assert full["is_active"] is True

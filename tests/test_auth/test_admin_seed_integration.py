"""Integration tests for seed_platform_admins (startup bootstrap).

The seed is promote-only for role, but must ALSO force an allowlisted admin to
active so the owner can never be stuck pending after a fresh deploy (parent spec
§Bootstrap). Skipped automatically when Postgres is unreachable.
"""

import uuid

import pytest

from src.backend.auth import db as auth_db
from src.backend.auth.admin_seed import seed_platform_admins
from src.backend.auth.repository import UserRepository
from src.backend.core.config import settings

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


def _email():
    return f"seed-{uuid.uuid4().hex[:8]}@x.com"


def _cleanup(email):
    with auth_db.get_pool().connection() as conn:
        conn.execute("DELETE FROM users WHERE email = %s", (email,))
        conn.commit()


def test_seed_activates_admin_pending_row(monkeypatch):
    """seed_platform_admins flips an ADMIN_EMAILS user who is admin+pending to
    active (a legacy/raced row must not leave the owner locked out)."""
    repo = UserRepository()
    email = _email()
    try:
        repo.create_user(email, "password123", "Seed")  # user/pending
        with auth_db.get_pool().connection() as conn:
            conn.execute(
                "UPDATE users SET role='admin', status='pending', is_active=FALSE WHERE email=%s",
                (email,),
            )
            conn.commit()
        monkeypatch.setattr(settings, "ADMIN_EMAILS", email)
        seed_platform_admins()
        full = repo.get_by_email(email)
        assert full["role"] == "admin"
        assert full["status"] == "active"
        assert full["is_active"] is True
    finally:
        _cleanup(email)


def test_seed_promotes_and_activates_plain_user(monkeypatch):
    """A plain user in ADMIN_EMAILS is promoted to admin AND activated at seed."""
    repo = UserRepository()
    email = _email()
    try:
        repo.create_user(email, "password123", "Seed")  # user/pending
        monkeypatch.setattr(settings, "ADMIN_EMAILS", email)
        seed_platform_admins()
        full = repo.get_by_email(email)
        assert full["role"] == "admin"
        assert full["status"] == "active"
        assert full["is_active"] is True
    finally:
        _cleanup(email)


def test_seed_does_not_reactivate_suspended_admin(monkeypatch):
    """A suspended allow-listed admin must stay suspended after seeding — the seed
    bootstraps a PENDING owner to active, but never resurrects an account an owner
    deliberately turned off (Finding #3)."""
    repo = UserRepository()
    email = _email()
    try:
        repo.create_user(email, "password123", "Seed")
        with auth_db.get_pool().connection() as conn:
            conn.execute(
                "UPDATE users SET role='admin', status='suspended', is_active=FALSE WHERE email=%s",
                (email,),
            )
            conn.commit()
        monkeypatch.setattr(settings, "ADMIN_EMAILS", email)
        seed_platform_admins()
        full = repo.get_by_email(email)
        assert full["role"] == "admin"        # role preserved
        assert full["status"] == "suspended"  # NOT reactivated
        assert full["is_active"] is False
    finally:
        _cleanup(email)


def test_seed_promotes_suspended_user_role_but_keeps_suspended(monkeypatch):
    """A suspended NON-admin in ADMIN_EMAILS is promoted to admin role, but its
    disabled status is preserved — role promotion must not smuggle in a
    reactivation (Finding #3)."""
    repo = UserRepository()
    email = _email()
    try:
        repo.create_user(email, "password123", "Seed")
        with auth_db.get_pool().connection() as conn:
            conn.execute(
                "UPDATE users SET status='suspended', is_active=FALSE WHERE email=%s",
                (email,),
            )
            conn.commit()
        monkeypatch.setattr(settings, "ADMIN_EMAILS", email)
        seed_platform_admins()
        full = repo.get_by_email(email)
        assert full["role"] == "admin"        # role promoted
        assert full["status"] == "suspended"  # status untouched
        assert full["is_active"] is False
    finally:
        _cleanup(email)

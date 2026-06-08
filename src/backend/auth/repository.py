"""
UserRepository — all SQL for the auth/users table (the storage swap-seam).

Hand-rolled password hashing via bcrypt directly (NOT passlib, which breaks on
bcrypt 5.x). Every method borrows a short-lived pooled connection. Keeping all
user SQL behind this one class means a future storage swap is localized here.

Referenced by: auth/endpoints.py, tests/test_auth.
Depends on: auth/db.py (pool), bcrypt, src/backend/core/config.py (admin list).
"""

import logging

import bcrypt
import psycopg

from src.backend.auth.db import get_pool
from src.backend.core.config import settings

logger = logging.getLogger(__name__)


class EmailAlreadyExists(Exception):
    """Raised by create_user when the email is already registered."""


def hash_password(password: str) -> str:
    """bcrypt hash (work factor default 12). Returns a str for TEXT storage."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str | None) -> bool:
    """Constant-time bcrypt verify. False on any malformed/missing hash."""
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def role_for_email(email: str) -> str:
    """'admin' if the email is in settings.ADMIN_EMAILS, else 'user'."""
    return "admin" if email.strip().lower() in settings.admin_emails_list else "user"


class UserRepository:
    """CRUD for the users table. Stateless — safe to instantiate once and share."""

    def create_user(self, email: str, password: str, display_name: str = "") -> dict:
        """Insert a password user. Raises EmailAlreadyExists on duplicate email."""
        email = email.strip().lower()
        role = role_for_email(email)
        try:
            with get_pool().connection() as conn:
                row = conn.execute(
                    """
                    INSERT INTO users (email, hashed_password, display_name, role, auth_provider)
                    VALUES (%s, %s, %s, %s, 'password')
                    RETURNING id, email, display_name, role
                    """,
                    (email, hash_password(password), display_name, role),
                ).fetchone()
                conn.commit()
                return row
        except psycopg.errors.UniqueViolation as exc:
            raise EmailAlreadyExists(email) from exc

    def get_by_email(self, email: str) -> dict | None:
        with get_pool().connection() as conn:
            return conn.execute(
                "SELECT * FROM users WHERE email = %s",
                (email.strip().lower(),),
            ).fetchone()

    def get_by_id(self, user_id: str) -> dict | None:
        with get_pool().connection() as conn:
            return conn.execute(
                "SELECT * FROM users WHERE id = %s",
                (user_id,),
            ).fetchone()

    def verify_credentials(self, email: str, password: str) -> dict | None:
        """Return the user row if email+password match and the account is active.

        Returns None for: unknown email, inactive account, google-only account
        (no password set), or a wrong password. Touches last_login on success.
        """
        user = self.get_by_email(email)
        if not user or not user.get("is_active"):
            return None
        if not verify_password(password, user.get("hashed_password")):
            return None
        self.touch_last_login(user["id"])
        return user

    def get_or_create_google_user(
        self, google_sub: str, email: str, display_name: str = ""
    ) -> dict:
        """Find a user by google_sub or email; create one if absent.

        If an existing password account shares the email, its google_sub is
        backfilled (account linking). Always updates last_login.
        """
        email = email.strip().lower()
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE google_sub = %s OR email = %s",
                (google_sub, email),
            ).fetchone()
            if row:
                if not row.get("google_sub"):
                    conn.execute(
                        "UPDATE users SET google_sub = %s, last_login = now() WHERE id = %s",
                        (google_sub, row["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE users SET last_login = now() WHERE id = %s",
                        (row["id"],),
                    )
                conn.commit()
                return row
            new_row = conn.execute(
                """
                INSERT INTO users (email, display_name, role, auth_provider, google_sub, last_login)
                VALUES (%s, %s, %s, 'google', %s, now())
                RETURNING id, email, display_name, role
                """,
                (email, display_name, role_for_email(email), google_sub),
            ).fetchone()
            conn.commit()
            return new_row

    def touch_last_login(self, user_id) -> None:
        with get_pool().connection() as conn:
            conn.execute(
                "UPDATE users SET last_login = now() WHERE id = %s",
                (user_id,),
            )
            conn.commit()

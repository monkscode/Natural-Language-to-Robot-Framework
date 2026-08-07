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
    """Raised when the email is already registered (signup or Google sign-in
    against an existing non-Google account)."""


class AccountInactive(Exception):
    """Raised by get_or_create_google_user when the matched account is disabled."""


class PasswordTooLong(Exception):
    """Raised when the password exceeds bcrypt's byte limit."""


MAX_PASSWORD_BYTES = 72  # hard bcrypt limit — hashpw raises ValueError beyond it

# Lazily-built hash used to equalize login timing for unknown emails (see
# verify_credentials). Computed (not hardcoded) so secret scanners stay quiet;
# lazy so importing this module doesn't pay a bcrypt round. A benign double
# compute under a thread race is harmless.
_timing_hash: str | None = None


def _timing_equalizer_hash() -> str:
    global _timing_hash
    if _timing_hash is None:
        _timing_hash = hash_password("timing-equalizer")
    return _timing_hash


def hash_password(password: str) -> str:
    """bcrypt hash (work factor default 12). Returns a str for TEXT storage."""
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise PasswordTooLong(f"password longer than {MAX_PASSWORD_BYTES} bytes")
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")


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

    _VALID_STATUS = ("pending", "active", "suspended", "rejected")

    def set_status(self, user_id: str, status: str, *, bump_token: bool = False) -> dict | None:
        """Set a user's lifecycle status. status='active' is authoritative for
        usability; is_active is written in lockstep as a coarse mirror. Captures
        the prior status atomically (one locked statement) for the audit from/to.
        Bumps token_version when bump_token is set (reject/suspend) so live tokens
        die on the next request. Returns the updated public row plus 'old_status',
        or None if no user matched."""
        if status not in self._VALID_STATUS:
            raise ValueError(f"invalid status: {status!r}")
        is_active = status == "active"
        bump = 1 if bump_token else 0
        with get_pool().connection() as conn:
            row = conn.execute(
                "WITH prev AS ("
                "  SELECT id, status FROM users WHERE id = %s FOR UPDATE"
                ") "
                "UPDATE users u SET status = %s, is_active = %s, "
                "token_version = token_version + %s FROM prev WHERE u.id = prev.id "
                "RETURNING prev.status AS old_status, "
                "u.id, u.email, u.role, u.status",
                (user_id, status, is_active, bump),
            ).fetchone()
            conn.commit()
        return row

    def list_users(self) -> list[dict]:
        """Every user for the admin Members tab: id, email, display_name, role,
        status, ordered deterministically by (created_at, id). One indexed
        SELECT — no pagination (alpha scale; pagination is future work)."""
        with get_pool().connection() as conn:
            return conn.execute(
                "SELECT id, email, display_name, role, status FROM users "
                "ORDER BY created_at, id",
            ).fetchall()

    def create_user(self, email: str, password: str, display_name: str = "") -> dict:
        """Insert a password user. Raises EmailAlreadyExists on duplicate email."""
        email = email.strip().lower()
        role = role_for_email(email)
        # Allowlisted (owner) signups are usable immediately; everyone else lands
        # pending until approved. status='active' is authoritative; is_active mirrors it.
        is_admin = role == "admin"
        status = "active" if is_admin else "pending"
        try:
            with get_pool().connection() as conn:
                row = conn.execute(
                    """
                    INSERT INTO users (email, hashed_password, display_name, role,
                                       auth_provider, status, is_active)
                    VALUES (%s, %s, %s, %s, 'password', %s, %s)
                    RETURNING id, email, display_name, role, status
                    """,
                    (email, hash_password(password), display_name, role, status, is_admin),
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

    def set_platform_role(self, user_id: str, role: str) -> dict | None:
        """Grant/revoke platform-admin. role in {'admin','user'}. The DB is the
        source of truth for platform-admin (ADMIN_EMAILS only seeds the first one
        at startup). Returns the updated public row plus 'old_role' (the prior
        role, captured atomically for the audit floor's from/to detail), or None
        if no user matched."""
        if role not in ("admin", "user"):
            raise ValueError(f"invalid platform role: {role!r}")
        with get_pool().connection() as conn:
            # Read the prior role under a row lock and update in the same
            # statement so the captured 'from' can't go stale between a separate
            # read and write under concurrent admin updates to the same user.
            row = conn.execute(
                "WITH prev AS ("
                "  SELECT id, role FROM users WHERE id = %s FOR UPDATE"
                ") "
                "UPDATE users u SET role = %s FROM prev WHERE u.id = prev.id "
                "RETURNING prev.role AS old_role, "
                "u.id, u.email, u.display_name, u.role",
                (user_id, role),
            ).fetchone()
            conn.commit()
        return row

    def _sync_role_to_allowlist(self, row: dict) -> dict:
        """Promote an allowlisted signer-in to platform-admin AND force them active.
        PROMOTE-ONLY on role (removing an email never demotes; the DB is the source
        of truth for platform-admin — revoke via set_platform_role). Also self-heals
        any admin row still marked non-active, so the owner can never be stuck
        pending (defence in depth for a legacy/raced bootstrap row)."""
        if role_for_email(row["email"]) != "admin":
            return row
        if row.get("role") == "admin" and row.get("status") == "active":
            return row  # already fully bootstrapped — no write
        with get_pool().connection() as conn:
            conn.execute(
                "UPDATE users SET role = 'admin', status = 'active', is_active = TRUE "
                "WHERE id = %s",
                (row["id"],),
            )
            conn.commit()
        logger.info("[AUTH] normalised %s to active platform-admin (ADMIN_EMAILS seed)", row["email"])
        row = dict(row)
        row["role"] = "admin"
        row["status"] = "active"
        row["is_active"] = True
        return row

    def verify_credentials(self, email: str, password: str) -> dict | None:
        """Return the user row if email+password match and the account is active.

        Returns None for: unknown email, inactive account, google-only account
        (no password set), or a wrong password. Touches last_login and syncs
        the role to ADMIN_EMAILS on success.
        """
        user = self.get_by_email(email)
        can_login = user and user.get("status") in ("pending", "active")
        if not user or not can_login or not user.get("hashed_password"):
            # Burn a bcrypt verify against a throwaway hash so unknown emails,
            # disabled accounts, and google-only accounts (no password hash —
            # verify_password would return instantly) all answer in the same
            # time as a wrong password: no timing oracle on account existence
            # or type. The result is deliberately ignored — always deny.
            verify_password(password, _timing_equalizer_hash())
            return None
        if not verify_password(password, user["hashed_password"]):
            return None
        self.touch_last_login(user["id"])
        return self._sync_role_to_allowlist(user)

    def get_or_create_google_user(
        self, google_sub: str, email: str, display_name: str = ""
    ) -> tuple[dict, bool]:
        """Find a user by google_sub; create one if absent.

        Lookup is by google_sub ONLY — an email match alone is not proof of
        account ownership, so an existing account with the same email is never
        auto-linked (raises EmailAlreadyExists; the owner signs in with their
        password instead). Disabled accounts raise AccountInactive. Updates
        last_login and syncs the role to ADMIN_EMAILS on success.

        Returns (row, created) where created is True only when a brand-new
        user row was inserted (not on an existing-user match or a raced
        concurrent-signup match).
        """
        email = email.strip().lower()
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE google_sub = %s",
                (google_sub,),
            ).fetchone()
            if row:
                if row.get("status") not in ("pending", "active"):
                    raise AccountInactive(email)
                conn.execute(
                    "UPDATE users SET last_login = now() WHERE id = %s",
                    (row["id"],),
                )
                conn.commit()
            else:
                if conn.execute(
                    "SELECT 1 FROM users WHERE email = %s", (email,)
                ).fetchone():
                    raise EmailAlreadyExists(email)
                # Allowlisted (owner) Google signups are usable immediately; everyone
                # else lands pending until approved. Mirror is_active off status.
                role = role_for_email(email)
                is_admin = role == "admin"
                status = "active" if is_admin else "pending"
                try:
                    new_row = conn.execute(
                        """
                        INSERT INTO users (email, display_name, role, auth_provider,
                                           google_sub, last_login, status, is_active)
                        VALUES (%s, %s, %s, 'google', %s, now(), %s, %s)
                        RETURNING id, email, display_name, role, status
                        """,
                        (email, display_name, role, google_sub, status, is_admin),
                    ).fetchone()
                except psycopg.errors.UniqueViolation as exc:
                    conn.rollback()
                    # Two unique constraints can fire here. google_sub: a concurrent
                    # callback for the SAME Google account (double-click on sign-in)
                    # just created the row — that is a success, return it instead of
                    # bouncing the user with "email exists". email: lost a race with
                    # a same-email signup from someone else — no auto-link, surface it.
                    raced = conn.execute(
                        "SELECT * FROM users WHERE google_sub = %s", (google_sub,)
                    ).fetchone()
                    if raced and raced.get("status") in ("pending", "active"):
                        return raced, False
                    raise EmailAlreadyExists(email) from exc
                conn.commit()
                return new_row, True
        # Existing-user path, AFTER the pool borrow above is released:
        # _sync_role_to_allowlist takes its own connection, and nesting two
        # borrows can deadlock a saturated pool. Sync against the stored email
        # (Google may report a different one than we keep — ownership was
        # proven for the stored row).
        return self._sync_role_to_allowlist(row), False

    def touch_last_login(self, user_id) -> None:
        with get_pool().connection() as conn:
            conn.execute(
                "UPDATE users SET last_login = now() WHERE id = %s",
                (user_id,),
            )
            conn.commit()

    def bump_token_version(self, user_id) -> int | None:
        """Increment token_version, invalidating every previously-minted token for
        this user (logout-all / compromise response). Returns the new version, or
        None if no user matched."""
        with get_pool().connection() as conn:
            row = conn.execute(
                "UPDATE users SET token_version = token_version + 1 WHERE id = %s "
                "RETURNING token_version",
                (user_id,),
            ).fetchone()
            conn.commit()
        return row["token_version"] if row else None

    def reset_password(self, email: str, new_password: str) -> dict | None:
        """Set a new password for *email* and revoke every token minted before
        the reset (token_version bump — same revocation as logout-all). Works for
        password and Google-SSO accounts alike (it just sets a password hash).

        The token_version bump is the security-critical half: when a reset is the
        response to a compromise, a stolen token must stop working immediately
        instead of riding out its remaining JWT_EXPIRY_HOURS. Returns the updated
        public row, or None if no user matched. Raises PasswordTooLong via
        hash_password when the password exceeds bcrypt's byte limit.
        """
        with get_pool().connection() as conn:
            row = conn.execute(
                "UPDATE users SET hashed_password = %s, "
                "token_version = token_version + 1 WHERE email = %s "
                "RETURNING id, email, role, auth_provider",
                (hash_password(new_password), email.strip().lower()),
            ).fetchone()
            conn.commit()
        return row

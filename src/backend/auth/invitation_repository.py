"""InvitationRepository — all SQL for the invitations table.

Stateless; borrows short-lived pooled connections, mirroring UserRepository.
An invitation is matched at signup by normalized email and consumed; the new
user is then tagged to the inviter's org at approval time.

Referenced by: auth/endpoints.py (signup match), auth/org_endpoints.py.
Depends on: auth/db.py (pool).
"""
import logging
import psycopg
from src.backend.auth.db import get_pool

logger = logging.getLogger(__name__)


class InvitationExists(Exception):
    """Raised when an open invite for the same (email, org) already exists."""


class InvitationRepository:
    def create(self, email: str, org_id: str, invited_by: str) -> dict:
        email = email.strip().lower()
        try:
            with get_pool().connection() as conn:
                row = conn.execute(
                    "INSERT INTO invitations (email, org_id, invited_by) "
                    "VALUES (%s, %s, %s) "
                    "RETURNING id, email, org_id, invited_by, status, created_at",
                    (email, org_id, invited_by),
                ).fetchone()
                conn.commit()
                return row
        except psycopg.errors.UniqueViolation as exc:
            raise InvitationExists(email) from exc

    def find_open_by_email(self, email: str) -> dict | None:
        with get_pool().connection() as conn:
            return conn.execute(
                "SELECT * FROM invitations WHERE lower(email) = %s AND status = 'open' "
                "ORDER BY created_at DESC LIMIT 1",
                (email.strip().lower(),),
            ).fetchone()

    def consume(self, invitation_id: str, user_id: str) -> bool:
        """Consume an OPEN invite. Returns True if a row was actually matched and
        updated; False if the invite was already consumed/revoked or gone (the
        UPDATE's status='open' guard matched nothing). The caller uses this to
        avoid recording a match on a zero-row no-op."""
        with get_pool().connection() as conn:
            cur = conn.execute(
                "UPDATE invitations SET status = 'consumed', consumed_by = %s, "
                "consumed_at = now() WHERE id = %s AND status = 'open'",
                (user_id, invitation_id),
            )
            conn.commit()
        return cur.rowcount > 0

    def find_consumed_by_user(self, user_id: str) -> dict | None:
        """The org-tagging invite a user consumed at signup (for approval-time
        membership provisioning)."""
        with get_pool().connection() as conn:
            return conn.execute(
                "SELECT * FROM invitations WHERE consumed_by = %s "
                "ORDER BY consumed_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()

    def list_for_org(self, org_id: str) -> list[dict]:
        with get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT id, email, status, created_at, consumed_by "
                "FROM invitations WHERE org_id = %s ORDER BY created_at DESC",
                (org_id,),
            ).fetchall()
        return [
            {
                "id": str(r["id"]),
                "email": r["email"],
                "status": r["status"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]

    def revoke(self, invitation_id: str, org_id: str) -> dict | None:
        """Revoke an OPEN invite owned by org_id. Returns the row or None if it
        was not open / not in this org (prevents cross-org revocation)."""
        with get_pool().connection() as conn:
            row = conn.execute(
                "UPDATE invitations SET status = 'revoked' "
                "WHERE id = %s AND org_id = %s AND status = 'open' "
                "RETURNING id, email, status",
                (invitation_id, org_id),
            ).fetchone()
            conn.commit()
        return row

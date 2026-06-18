"""OrgRepository — all SQL for organizations + org_members (the tenancy seam).

Mirrors UserRepository: stateless, borrows short-lived pooled connections.
Keeping org SQL behind one class localises a future storage swap. Phase 1a only
provisions personal orgs; org-scoped reads land in a later sub-plan.

Referenced by: auth/endpoints.py (register, Google callback), auth/org_db.py
(backfill), main.py, tests/test_auth.
Depends on: auth/db.py (pool).
"""

import logging

from src.backend.auth.db import get_pool

logger = logging.getLogger(__name__)


class OrgRepository:
    """CRUD for organizations + org_members. Stateless — share one instance."""

    def ensure_personal_org(self, user_id: str, name: str) -> str:
        """Return the user's org id, creating a personal org + org_admin
        membership if they belong to none. Idempotent.

        A FOR UPDATE lock on the user row serialises concurrent calls for the
        same brand-new user (register + a lazy ensure racing), so a user can
        never end up with two personal orgs.
        """
        with get_pool().connection() as conn:
            # Serialise per-user so the existence check + insert is atomic.
            conn.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (user_id,))
            existing = conn.execute(
                "SELECT org_id FROM org_members WHERE user_id = %s "
                "ORDER BY created_at LIMIT 1",
                (user_id,),
            ).fetchone()
            if existing:
                conn.commit()
                return str(existing["org_id"])
            org = conn.execute(
                "INSERT INTO organizations (name, kind) VALUES (%s, 'personal') "
                "RETURNING id",
                (name,),
            ).fetchone()
            conn.execute(
                "INSERT INTO org_members (org_id, user_id, org_role) "
                "VALUES (%s, %s, 'org_admin')",
                (org["id"], user_id),
            )
            conn.commit()
            logger.info("[AUTH] provisioned personal org for user %s", user_id)
            return str(org["id"])

    def get_orgs_for_user(self, user_id: str) -> list[dict]:
        """Org memberships for a user, oldest first. Empty list if none."""
        with get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT m.org_id, m.org_role, o.name, o.kind "
                "FROM org_members m JOIN organizations o ON o.id = m.org_id "
                "WHERE m.user_id = %s ORDER BY m.created_at",
                (user_id,),
            ).fetchall()
        return [
            {
                "org_id": str(r["org_id"]),
                "org_role": r["org_role"],
                "name": r["name"],
                "kind": r["kind"],
            }
            for r in rows
        ]

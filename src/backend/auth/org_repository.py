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
            # Only a PERSONAL org counts as "already provisioned". A user may
            # belong to a team org (kind='team') yet still need their own
            # personal org — keying off any membership would thread the team
            # org as their personal one.
            existing = conn.execute(
                "SELECT m.org_id FROM org_members m "
                "JOIN organizations o ON o.id = m.org_id "
                "WHERE m.user_id = %s AND o.kind = 'personal' "
                "ORDER BY m.created_at LIMIT 1",
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

    def backfill_personal_orgs(self) -> int:
        """Provision a personal org for every user that has no membership.

        Idempotent: re-running provisions only users still missing an org.
        Returns the number of users newly provisioned. Called once at startup
        from init_org_db(); safe to call repeatedly.
        """
        with get_pool().connection() as conn:
            orphans = conn.execute(
                "SELECT u.id, u.email FROM users u "
                "LEFT JOIN org_members m ON m.user_id = u.id "
                "WHERE m.user_id IS NULL",
            ).fetchall()
        for row in orphans:
            self.ensure_personal_org(str(row["id"]), row["email"])
        if orphans:
            logger.info("[AUTH] backfilled %d user(s) into personal orgs", len(orphans))
        return len(orphans)

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

    def list_orgs(self) -> list[dict]:
        """Every org with its member count for the admin Orgs tab, ordered by
        (created_at, id). Single SELECT: LEFT JOIN org_members + COUNT + GROUP BY
        (no N+1). GROUP BY the PK lets Postgres order by created_at safely."""
        with get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT o.id, o.name, o.kind, COUNT(m.user_id) AS member_count "
                "FROM organizations o "
                "LEFT JOIN org_members m ON m.org_id = o.id "
                "GROUP BY o.id "
                "ORDER BY o.created_at, o.id",
            ).fetchall()
        return [
            {"id": str(r["id"]), "name": r["name"], "kind": r["kind"],
             "member_count": r["member_count"]}
            for r in rows
        ]

    def create_team_org(self, name: str, owner_user_id: str) -> str:
        """Create a kind='team' org and seat owner_user_id as its org_admin.
        Returns the new org id."""
        with get_pool().connection() as conn:
            org = conn.execute(
                "INSERT INTO organizations (name, kind) VALUES (%s, 'team') RETURNING id",
                (name,),
            ).fetchone()
            conn.execute(
                "INSERT INTO org_members (org_id, user_id, org_role) "
                "VALUES (%s, %s, 'org_admin')",
                (org["id"], owner_user_id),
            )
            conn.commit()
            return str(org["id"])

    def add_member(self, org_id: str, user_id: str, org_role: str = "org_member") -> None:
        """Idempotent: add a user to an org, or update their role if already a
        member."""
        if org_role not in ("org_admin", "org_member"):
            raise ValueError(f"invalid org_role: {org_role!r}")
        with get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO org_members (org_id, user_id, org_role) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (org_id, user_id) DO UPDATE SET org_role = EXCLUDED.org_role",
                (org_id, user_id, org_role),
            )
            conn.commit()

    def set_org_owner(self, org_id: str, user_id: str, org_role: str) -> None:
        """Promote/demote a member's org_role (org_admin/org_member)."""
        self.add_member(org_id, user_id, org_role)

    def is_team_admin(self, user_id: str) -> bool:
        """True iff the user is org_admin of at least one TEAM org (personal-org
        admin does NOT count — every user owns their personal org)."""
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM org_members m JOIN organizations o ON o.id = m.org_id "
                "WHERE m.user_id = %s AND m.org_role = 'org_admin' AND o.kind = 'team' "
                "LIMIT 1",
                (user_id,),
            ).fetchone()
        return row is not None

    def get_members(self, org_id: str) -> list[dict]:
        """Members of an org joined to users, oldest first."""
        with get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT m.user_id, u.email, u.display_name, m.org_role, u.status "
                "FROM org_members m JOIN users u ON u.id = m.user_id "
                "WHERE m.org_id = %s ORDER BY m.created_at, m.user_id",
                (org_id,),
            ).fetchall()
        return [
            {
                "user_id": str(r["user_id"]),
                "email": r["email"],
                "display_name": r["display_name"],
                "org_role": r["org_role"],
                "status": r["status"],
            }
            for r in rows
        ]

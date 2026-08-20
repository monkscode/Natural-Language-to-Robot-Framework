"""OrgRepository — all SQL for organizations + org_members (the tenancy seam).

Mirrors UserRepository: stateless, borrows short-lived pooled connections.
Keeping org SQL behind one class localises a future storage swap. Phase 1a only
provisions personal orgs; org-scoped reads land in a later sub-plan.

Referenced by: auth/endpoints.py (register, Google callback), auth/org_db.py
(backfill), main.py, tests/test_auth.
Depends on: auth/db.py (pool).
"""

import logging

import psycopg

from src.backend.auth.db import get_pool
from src.backend.config.logging_config import sanitize_for_log

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
            # user_id reaches this log from an HTTP path param (e.g. admin remove
            # member), so scrub CR/LF before logging (Sonar S5145 / CWE-117).
            logger.info("[AUTH] provisioned personal org for user %s", sanitize_for_log(user_id))
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
        """Create a kind='team' org and seat owner_user_id as its org_admin, moving
        the owner out of any prior org (single-active-org). Returns the new org id."""
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
            self._collapse_to_single(conn, owner_user_id, str(org["id"]))
            conn.commit()
            return str(org["id"])

    def _is_team_org(self, conn, org_id: str) -> bool:
        """True iff org_id exists and is a team org. Membership assignment applies
        to team orgs only — a personal org is 1:1 with its user."""
        row = conn.execute(
            "SELECT kind FROM organizations WHERE id = %s", (org_id,)
        ).fetchone()
        return row is not None and row["kind"] == "team"

    def _delete_personal_org_if_empty(self, conn, org_id: str) -> None:
        """Delete org_id iff it is a personal org with zero remaining members.
        Personal orgs are 1:1 with a user and meaningless once vacated; team orgs
        are never auto-deleted. Safe: the only hard FKs to organizations are
        org_members (none here) and invitations (personal orgs are never invited
        into), both ON DELETE CASCADE; history/learning org_id is a soft column
        with no FK, so this can neither be blocked nor cascade user data."""
        conn.execute(
            "DELETE FROM organizations o "
            "WHERE o.id = %s AND o.kind = 'personal' "
            "AND NOT EXISTS (SELECT 1 FROM org_members m WHERE m.org_id = o.id)",
            (org_id,),
        )

    def _collapse_to_single(self, conn, user_id: str, keep_org_id: str) -> None:
        """Single-active-org invariant: remove every membership of user_id except
        the one in keep_org_id, deleting any personal org thereby vacated. Runs in
        the caller's transaction."""
        vacated = conn.execute(
            "DELETE FROM org_members WHERE user_id = %s AND org_id <> %s "
            "RETURNING org_id",
            (user_id, keep_org_id),
        ).fetchall()
        for row in vacated:
            org_id = str(row["org_id"])
            self._release_private_groups(conn, user_id, org_id)
            self._delete_personal_org_if_empty(conn, org_id)

    def _release_private_groups(self, conn, user_id: str, org_id: str) -> None:
        """F10: flip every PRIVATE run_groups folder user_id created in org_id
        to 'org' visibility, so it does not strand out of every remaining
        member's reach once user_id departs org_id. Called once per vacated
        org from _collapse_to_single, and directly from remove_member (which
        bypasses _collapse_to_single entirely).

        Runs inside the caller's own transaction; never lets a failure here
        abort that transaction — an org move must succeed regardless of the
        state of anyone's folders. Guarded by to_regclass('run_groups')
        (NULL when absent): RunRegistry is lazy and main.py never constructs
        it at startup, so a fresh database can see an org move before the
        table exists — a bare SELECT/UPDATE would raise UndefinedTable and,
        without this guard, take the caller's whole transaction down with
        it. The unqualified name resolves through the connection's
        search_path, which matters for the isolated-schema test suites
        (search_path=auth_test only, no ,public fallback) — hard-coding
        public.run_groups would make the flip invisible to them there and a
        silent write to public in the real app.
        """
        try:
            with conn.transaction():
                has_table = conn.execute(
                    "SELECT to_regclass('run_groups') AS reg"
                ).fetchone()["reg"]
                if has_table is None:
                    return
                folders = conn.execute(
                    "SELECT group_id, name FROM run_groups "
                    "WHERE created_by = %s AND org_id = %s AND visibility = 'private'",
                    (user_id, org_id),
                ).fetchall()
        except Exception as exc:  # noqa: BLE001 — must never abort the org move
            logger.warning(
                "[AUTH] private-folder lookup failed releasing user %s from org %s: %s",
                sanitize_for_log(user_id), sanitize_for_log(org_id), exc)
            return
        for folder in folders:
            self._flip_private_group(conn, str(folder["group_id"]), folder["name"])

    def _flip_private_group(self, conn, group_id: str, name: str) -> None:
        """Flip one folder to 'org' visibility. The two run_groups partial
        unique indexes (org-scoped for 'org' folders, creator-scoped for
        'private' ones — see run_registry.py) let a private folder share a
        name with an existing org folder right up until this flip, so the
        UPDATE can raise UniqueViolation. Retried once under a deterministic
        renamed name derived from the folder's own group_id, which cannot
        collide because group_id is the primary key. If that still fails,
        the folder is left private and the collision logged — an org move
        must never fail because of a folder name.

        Per-folder, not one bulk UPDATE for every folder in the org: a
        collision is a per-folder event, and a single bulk statement would
        fail as a unit, stranding every one of the user's folders because
        ONE of them collided. Each attempt is its own savepoint so one
        folder's failure cannot undo another folder's already-applied flip
        earlier in the same release.
        """
        try:
            with conn.transaction():
                conn.execute(
                    "UPDATE run_groups SET visibility = 'org', updated_at = now() "
                    "WHERE group_id = %s AND visibility = 'private'",
                    (group_id,),
                )
            return
        except psycopg.errors.UniqueViolation:
            pass
        except Exception as exc:  # noqa: BLE001 — must never abort the org move
            logger.warning(
                "[AUTH] folder %s failed to release from private on org move: %s",
                sanitize_for_log(group_id), exc)
            return
        renamed = f"{name} ({group_id[:8]})"
        try:
            with conn.transaction():
                conn.execute(
                    "UPDATE run_groups SET visibility = 'org', name = %s, updated_at = now() "
                    "WHERE group_id = %s AND visibility = 'private'",
                    (renamed, group_id),
                )
        except Exception as exc:  # noqa: BLE001 — must never abort the org move
            logger.warning(
                "[AUTH] folder %s left private after a name collision on org move: %s",
                sanitize_for_log(group_id), exc)

    def _email_for(self, user_id: str) -> str | None:
        """The user's email (used as their personal-org name), or None if absent."""
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT email FROM users WHERE id = %s", (user_id,)
            ).fetchone()
        return row["email"] if row else None

    def add_member(self, org_id: str, user_id: str, org_role: str = "org_member") -> None:
        """Move a user into a TEAM org (single-active-org): upsert their membership,
        then remove every other membership and prune any emptied personal org.
        Membership changes apply to team orgs only (Finding #6)."""
        if org_role not in ("org_admin", "org_member"):
            raise ValueError(f"invalid org_role: {org_role!r}")
        with get_pool().connection() as conn:
            if not self._is_team_org(conn, org_id):
                raise ValueError("membership changes apply to team orgs only")
            conn.execute(
                "INSERT INTO org_members (org_id, user_id, org_role) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (org_id, user_id) DO UPDATE SET org_role = EXCLUDED.org_role",
                (org_id, user_id, org_role),
            )
            self._collapse_to_single(conn, user_id, org_id)
            conn.commit()

    def set_org_owner(self, org_id: str, user_id: str, org_role: str) -> None:
        """Promote/demote a member's org_role (org_admin/org_member). Team orgs
        only — delegates to add_member, which enforces the guard."""
        self.add_member(org_id, user_id, org_role)

    def remove_member(self, org_id: str, user_id: str) -> bool:
        """Remove a membership from a TEAM org. Returns True if a row was deleted.
        If this was the user's last membership, provision a personal org so they
        stay usable. Team orgs only (mirrors add_member / reassign_user_org): a
        personal org is 1:1 with its user, and removing its sole membership would
        orphan the org row and churn a replacement personal org — so a personal
        (or unknown) org raises ValueError."""
        with get_pool().connection() as conn:
            if not self._is_team_org(conn, org_id):
                raise ValueError("membership changes apply to team orgs only")
            deleted = conn.execute(
                "DELETE FROM org_members WHERE org_id = %s AND user_id = %s RETURNING user_id",
                (org_id, user_id),
            ).fetchone()
            if deleted:
                # remove_member deletes the membership directly — it never
                # goes through _collapse_to_single, so F10's flip needs its
                # own call here or a member removed this way strands their
                # private folders in the org they just left (Ruling 3).
                self._release_private_groups(conn, user_id, org_id)
            remaining = conn.execute(
                "SELECT 1 FROM org_members WHERE user_id = %s LIMIT 1", (user_id,)
            ).fetchone()
            conn.commit()
        # ensure_personal_org borrows its own connection — call it only AFTER the
        # borrow above is released, so a saturated pool can't deadlock on a nested
        # borrow (mirrors get_or_create_google_user's pattern).
        if deleted and remaining is None:
            self.ensure_personal_org(user_id, self._email_for(user_id) or user_id)
        return deleted is not None

    def reassign_user_org(self, user_id: str, from_org_id: str, to_org_id: str,
                          org_role: str = "org_member") -> None:
        """Atomically move a user so they are in EXACTLY to_org_id (single-active-org).
        Raises ValueError if to_org is not a team org (a personal org is never a move
        target). from_org_id is advisory — the collapse removes every other membership,
        so a stale from_org_id cannot leave a second membership behind."""
        if org_role not in ("org_admin", "org_member"):
            raise ValueError(f"invalid org_role: {org_role!r}")
        with get_pool().connection() as conn:
            if not self._is_team_org(conn, to_org_id):
                raise ValueError("target org must be a team org")
            conn.execute(
                "INSERT INTO org_members (org_id, user_id, org_role) VALUES (%s, %s, %s) "
                "ON CONFLICT (org_id, user_id) DO UPDATE SET org_role = EXCLUDED.org_role",
                (to_org_id, user_id, org_role),
            )
            self._collapse_to_single(conn, user_id, to_org_id)
            conn.commit()

    def collapse_all_to_single_org(self) -> int:
        """One-time cleanup: reduce every user with more than one membership to a
        single active org. Keep the most recently joined TEAM org if the user is in
        any team org (reflects the latest assignment intent), else their personal
        org; delete the rest and prune vacated personal orgs. Bumps token_version
        for each collapsed user so their stale token refreshes. Returns the number
        of users collapsed. Idempotent: a no-op once everyone is single-org."""
        collapsed_uids: list[str] = []
        with get_pool().connection() as conn:
            multi = conn.execute(
                "SELECT user_id FROM org_members GROUP BY user_id HAVING COUNT(*) > 1"
            ).fetchall()
            for row in multi:
                uid = str(row["user_id"])
                keeper = conn.execute(
                    "SELECT m.org_id FROM org_members m "
                    "JOIN organizations o ON o.id = m.org_id "
                    "WHERE m.user_id = %s "
                    "ORDER BY (o.kind = 'team') DESC, m.created_at DESC LIMIT 1",
                    (uid,),
                ).fetchone()
                self._collapse_to_single(conn, uid, str(keeper["org_id"]))
                collapsed_uids.append(uid)
            # Bump token_version in the SAME transaction as the membership cleanup so
            # the two commit atomically: a crash between the two can never leave a
            # collapsed user holding a live token that still carries a now-removed
            # org_id. Executing on THIS conn (not a fresh UserRepository borrow) keeps
            # the method single-connection, so the nested-borrow deadlock the previous
            # post-commit loop guarded against cannot arise. (SQL mirrors
            # UserRepository.bump_token_version.)
            for uid in collapsed_uids:
                conn.execute(
                    "UPDATE users SET token_version = token_version + 1 WHERE id = %s",
                    (uid,),
                )
            conn.commit()
        return len(collapsed_uids)

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

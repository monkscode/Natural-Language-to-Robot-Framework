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
from src.backend.config.logging_config import sanitize_for_log

logger = logging.getLogger(__name__)


class OrgRepository:
    """CRUD for organizations + org_members. Stateless — share one instance."""

    def ensure_personal_org(self, user_id: str, name: str) -> str:
        """Return the user's personal org id. Idempotent, and now a RECLAIM
        before a create: a personal org row is permanent (see
        _collapse_to_single's docstring), so a user who owned one before —
        vacated by a team join, then handed back by remove_member — is
        reunited with that SAME org and its learning history, never handed a
        fresh empty one.

        A FOR UPDATE lock on the user row serialises concurrent calls for the
        same user (register + a lazy ensure racing, or two logins), so a user
        can never end up with two personal orgs.

        Order, all inside that lock:
          1. Reachable via org_members (kind='personal') -> return it. Only a
             PERSONAL org counts as "already provisioned" — a user may belong
             to a team org (kind='team') yet still need their own personal
             org, so keying off any membership would thread the team org as
             their personal one. If its owner_user_id is still NULL (legacy
             data predating this column, or a backfill hole), opportunistically
             stamp it with this user first — the last moment the link is
             knowable, before a future team join deletes the membership and
             the link with it. See _stamp_owner_if_unclaimed for why that
             stamp can never raise.
          2. Else, reachable via organizations.owner_user_id with NO current
             membership (e.g. after remove_member's last-membership cleanup)
             -> re-seat the org_admin membership (ON CONFLICT DO NOTHING:
             never skip this insert, or the caller is left with zero
             memberships, which mints an org-less, unscoped-learning token at
             their next login) and return it.
          3. Else -> create a fresh personal org with owner_user_id set at
             INSERT time, seat the membership, return it. The INSERT is
             ON CONFLICT, not bare: see there for the writer the FOR UPDATE
             does not serialise.

        Returns an org id or raises nothing it can avoid: this method is on the
        login self-heal, on remove_member and on provision_on_approval, so a
        raise here is a 500 on login. That is the same reason
        _stamp_owner_if_unclaimed is guarded rather than allowed to collide.
        """
        with get_pool().connection() as conn:
            # Serialise per-user so the existence check + insert is atomic.
            conn.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (user_id,))
            existing = conn.execute(
                "SELECT m.org_id, o.owner_user_id FROM org_members m "
                "JOIN organizations o ON o.id = m.org_id "
                "WHERE m.user_id = %s AND o.kind = 'personal' "
                "ORDER BY m.created_at LIMIT 1",
                (user_id,),
            ).fetchone()
            if existing:
                if existing["owner_user_id"] is None:
                    self._stamp_owner_if_unclaimed(conn, str(existing["org_id"]), user_id)
                conn.commit()
                return str(existing["org_id"])

            # No current membership in a personal org. Before minting a new
            # one, check whether this user already OWNS a personal org they
            # vacated (team join, then remove_member's last-membership
            # cleanup) — reclaim it instead of orphaning its learning history
            # under a brand new org.
            reclaimed = conn.execute(
                "SELECT id FROM organizations WHERE kind = 'personal' AND owner_user_id = %s",
                (user_id,),
            ).fetchone()
            if reclaimed:
                conn.execute(
                    "INSERT INTO org_members (org_id, user_id, org_role) "
                    "VALUES (%s, %s, 'org_admin') "
                    "ON CONFLICT (org_id, user_id) DO NOTHING",
                    (reclaimed["id"], user_id),
                )
                conn.commit()
                logger.info("[AUTH] reclaimed personal org for user %s", sanitize_for_log(user_id))
                return str(reclaimed["id"])

            # ON CONFLICT, not a bare INSERT. uq_org_owner_personal is a
            # partial unique index on (owner_user_id) WHERE kind='personal',
            # and the FOR UPDATE above does not serialise every writer that can
            # land in it. It serialises MOST of them by accident:
            # organizations.owner_user_id carries an FK to users(id), so any
            # writer SETTING it takes a FOR KEY SHARE lock on that users row,
            # which conflicts with this transaction's FOR UPDATE — the
            # backfill_personal_org_owners interleaving (another instance's
            # boot claiming the org step 2 just failed to find) is blocked by
            # that lock, measured. What is NOT blocked is a writer that leaves
            # owner_user_id alone and changes only `kind`: Postgres skips the
            # FK re-check when the referencing value is unchanged, so such a
            # statement takes no users lock, is invisible to step 2 until it
            # commits, and enters the index the moment it does.
            #
            # Relying on that FK-plus-row-lock coupling to keep this method
            # from raising would be a guarantee nobody could see: dropping the
            # FK, or widening the index, would silently reopen a 500 on login.
            # So the statement itself absorbs the collision.
            #
            # DO UPDATE rather than DO NOTHING because a row must always come
            # back: DO NOTHING returns nothing, and in READ COMMITTED it may
            # skip on a row this transaction still cannot SELECT. The SET is a
            # no-op by value (same owner, and `name` is deliberately untouched,
            # so a reclaimed org keeps its own); it exists only so RETURNING
            # yields the existing row's id.
            #
            # (xmax <> 0) distinguishes the two outcomes for the log line ONLY
            # — never for control flow. It is the standard "was this an update"
            # RETURNING idiom; a wrong answer costs one imprecise log line.
            org = conn.execute(
                "INSERT INTO organizations (name, kind, owner_user_id) "
                "VALUES (%s, 'personal', %s) "
                "ON CONFLICT (owner_user_id) WHERE kind = 'personal' "
                "DO UPDATE SET owner_user_id = EXCLUDED.owner_user_id "
                "RETURNING id, (xmax <> 0) AS collided",
                (name, user_id),
            ).fetchone()
            conn.execute(
                # ON CONFLICT DO NOTHING for the same reason step 2 has it: on
                # the collision branch the org already existed, so it may
                # already carry this membership. Never skip the insert itself,
                # or the caller is left with zero memberships, which mints an
                # org-less, unscoped-learning token at their next login.
                "INSERT INTO org_members (org_id, user_id, org_role) "
                "VALUES (%s, %s, 'org_admin') "
                "ON CONFLICT (org_id, user_id) DO NOTHING",
                (org["id"], user_id),
            )
            conn.commit()
            # user_id reaches this log from an HTTP path param (e.g. admin remove
            # member), so scrub CR/LF before logging (Sonar S5145 / CWE-117).
            logger.info(
                "[AUTH] %s personal org for user %s",
                "adopted concurrently claimed" if org["collided"] else "provisioned",
                sanitize_for_log(user_id),
            )
            return str(org["id"])

    def _stamp_owner_if_unclaimed(self, conn, org_id: str, user_id: str) -> None:
        """Best-effort claim of org_id for user_id when owner_user_id is NULL.
        Runs in the caller's transaction (ensure_personal_org); does not
        commit.

        Guards the UPDATE instead of catching a uq_org_owner_personal
        violation: a legacy user can already OWN a different personal org
        (e.g. the name-based backfill claimed org B while this user's live
        membership is in org A, whose name was never an email) — updating
        org A's owner in that case would collide with org B's claim. The
        NOT EXISTS clause makes the collision structurally impossible rather
        than raising and recovering from it, so this never needs a savepoint.

        ensure_personal_org sits on the login/provisioning path: a failed
        stamp must never raise and strand the user, and with this guard it
        never does — the WHERE clause simply matches zero rows and the
        caller returns org_id regardless of whether the stamp took.
        """
        conn.execute(
            "UPDATE organizations SET owner_user_id = %s "
            "WHERE id = %s AND owner_user_id IS NULL "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM organizations o2 "
            "  WHERE o2.kind = 'personal' AND o2.owner_user_id = %s"
            ")",
            (user_id, org_id, user_id),
        )

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

    def backfill_personal_org_owners(self) -> int:
        """One-time repair: stamp owner_user_id on personal orgs that predate
        the column (created before ensure_personal_org started setting it at
        INSERT time). Two passes, primary then fallback, matching the two
        ways an old org can be traced back to its user:

          1. Sole remaining member: org_members has exactly one row for that
             org, and that row's user takes ownership.
          2. Name is the user's email: every ensure_personal_org caller
             passes the user's email as `name` (register(), remove_member,
             provision_on_approval), so a personal org's name identifies its
             user even after it is vacated (member removed) and pass 1 no
             longer applies.

        Both passes SKIP an ambiguous match rather than raising: two personal
        orgs sharing a name (one user would own both), or a user this backfill
        (or a concurrent stamp — see below) already gave a different org to.
        uq_org_owner_personal forbids two personal orgs with the same owner,
        and this backfill must never abort partway and leave later rows
        unprocessed — an ambiguous org is simply left owner_user_id NULL.

        The primary pass carries TWO guards the task-2 brief's SQL has
        neither of, and they cover different shapes.

        The first is per-user ambiguity: a user who is the sole member of two
        still-unclaimed personal orgs matches both rows of this one statement.
        The "already owned" NOT EXISTS cannot see a row the same statement is
        updating (it reads the statement snapshot), so both rows take the same
        owner and uq_org_owner_personal raises. Resolved the way the fallback
        resolves a duplicate name: claim NEITHER. Picking one arbitrarily would
        decide which org's learning history the user reclaims.

        The second is "already owned": run_migration_once is
        advisory-lock-gated per migration NAME, not per row, and this table
        has a second writer — ensure_personal_org's own opportunistic stamp
        (step 1) — that a concurrent instance's live login traffic can run at
        any time, including mid-rolling-deploy while another instance is
        still starting up. A user who is the sole member of TWO legacy
        personal orgs (a real pre-single-active-org shape;
        collapse_all_to_single_org exists because double membership
        happened) can have one of them stamped by that live traffic before
        this migration's primary pass reaches the other — without the guard
        that collision raises UniqueViolation and aborts the WHOLE migration
        (both passes, every other row, in this one transaction), a boot-time
        crash from a data race the module's own advisory-lock comment already
        treats as expected. Caught by test_org_db.py's collision test.

        Known holes, left deliberately (see task-2 brief): a vacated personal
        org whose name is not an email matches neither pass and stays NULL
        forever (its hints become unreachable); two personal orgs sharing a
        name leave BOTH NULL.

        Idempotent: only touches rows still NULL. Called once from
        init_org_db() under run_migration_once("personal_org_owner_backfill").
        Returns the total number of org rows updated across both passes.
        """
        with get_pool().connection() as conn:
            primary = conn.execute(
                "UPDATE organizations o SET owner_user_id = m.user_id "
                "FROM org_members m "
                "WHERE o.kind = 'personal' AND o.owner_user_id IS NULL "
                "AND m.org_id = o.id "
                "AND (SELECT count(*) FROM org_members x WHERE x.org_id = o.id) = 1 "
                # ...and that member is in exactly ONE unclaimed personal org.
                # Without this, a user in two of them matches BOTH rows: the
                # NOT EXISTS below reads the statement snapshot, so neither row
                # sees the other being set, both take the same owner, and
                # uq_org_owner_personal aborts the whole migration.
                "AND (SELECT count(*) FROM org_members y "
                "     JOIN organizations o4 ON o4.id = y.org_id "
                "     WHERE y.user_id = m.user_id AND o4.kind = 'personal' "
                "       AND o4.owner_user_id IS NULL) = 1 "
                "AND NOT EXISTS ("
                "    SELECT 1 FROM organizations o2 "
                "    WHERE o2.kind = 'personal' AND o2.owner_user_id = m.user_id"
                ")"
            )
            fallback = conn.execute(
                "UPDATE organizations o SET owner_user_id = u.id "
                "FROM users u "
                "WHERE o.kind = 'personal' AND o.owner_user_id IS NULL "
                "AND o.name = u.email "
                "AND (SELECT count(*) FROM organizations o2 "
                "     WHERE o2.kind = 'personal' AND o2.name = o.name) = 1 "
                "AND NOT EXISTS ("
                "    SELECT 1 FROM organizations o3 "
                "    WHERE o3.kind = 'personal' AND o3.owner_user_id = u.id"
                ")"
            )
            conn.commit()
            updated = (primary.rowcount or 0) + (fallback.rowcount or 0)
        if updated:
            logger.info("[AUTH] backfilled owner_user_id for %d personal org(s)", updated)
        return updated

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
        the owner out of any prior org (single-active-org). Returns the new org id.

        The `owner_user_id` parameter names the MEMBERSHIP to seat and is
        deliberately NOT written to the organizations.owner_user_id column it
        now shares a name with: that column exists so a user can reclaim their
        own PERSONAL org, and uq_org_owner_personal is partial on
        kind='personal'. A team org has no 1:1 owner and is left unconstrained
        on purpose (org_db._ORG_OWNER_INDEX_DDL) — writing it here would make
        one user's team org and their personal org compete for that column."""
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

    def _collapse_to_single(self, conn, user_id: str, keep_org_id: str) -> None:
        """Single-active-org invariant: remove every membership of user_id
        except the one in keep_org_id. Runs in the caller's transaction.

        A personal org vacated by this removal is kept, not deleted: org
        rows are permanent. No FK exists from the learning tables to
        organizations, so deleting a vacated org would strand the learning
        rows naming it. The token's org claim is minted from org_members
        JOIN organizations, so a deleted org can never be claimed again and
        every org-scoped learning read for those rows would match zero
        rows. Cost: one leftover organizations row per user who joins a
        team; invisible to the user (get_orgs_for_user joins org_members),
        it shows in the admin Orgs tab with 0 members.
        """
        conn.execute(
            "DELETE FROM org_members WHERE user_id = %s AND org_id <> %s "
            "RETURNING org_id",
            (user_id, keep_org_id),
        )

    def _email_for(self, user_id: str) -> str | None:
        """The user's email (used as their personal-org name), or None if absent."""
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT email FROM users WHERE id = %s", (user_id,)
            ).fetchone()
        return row["email"] if row else None

    def add_member(self, org_id: str, user_id: str, org_role: str = "org_member") -> None:
        """Move a user into a TEAM org (single-active-org): upsert their membership,
        then remove every other membership; an emptied personal org row is kept,
        not pruned (org rows are permanent, see _collapse_to_single).
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
        org; delete the rest of their memberships (a vacated personal org row is
        kept, not pruned). Bumps token_version for each collapsed user so their
        stale token refreshes. Returns the number of users collapsed. Idempotent:
        a no-op once everyone is single-org."""
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

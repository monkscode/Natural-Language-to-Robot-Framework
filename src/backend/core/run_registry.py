"""
Per-run history registry — who ran what, with what outcome.

One `test_runs` row per workflow/run id, written from the SSE streaming
generators in workflow_service:
- record_start() when a run id becomes known (generation complete or Docker
  execution starting) — an upsert that NEVER steals ownership: the first
  writer's user/query attribution wins, later calls only advance the status.
- set_status() when the Docker result (passed/failed) or an error is known.

Read by:
- /api/history (api/history_endpoints.py) — regular users see only their own
  rows; admins see everything.
- authorize_report_access (auth/jwt_utils.py) — per-owner authorization for the
  /reports/{run_id}/ files.

Registry writes must never break the pipeline: every method swallows its own
exceptions (mirrors the WorkflowMetricsCollector / learning-store discipline).
get_owner() fails CLOSED — on any error it returns None, which the reports
guard treats as "not yours".

Referenced by: services/workflow_service.py, api/history_endpoints.py,
auth/jwt_utils.py (lazy import).
Depends on: core/config.py (DATABASE_URL).
"""

import logging
import uuid
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from src.backend.core.config import PG_CONNECT_TIMEOUT_S, settings

logger = logging.getLogger(__name__)

_SCHEMA_DDL = (
    """
    CREATE TABLE IF NOT EXISTS test_runs (
        run_id     TEXT PRIMARY KEY,
        user_id    TEXT,
        user_email TEXT,
        user_query TEXT,
        robot_code TEXT,
        rerun_of   TEXT,
        status     TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Upgrade path for tables created before these columns existed.
    "ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS robot_code TEXT",
    "ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS rerun_of TEXT",
    "ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS org_id TEXT",
    # Why a run ended in status 'error'. Generation failures write no metrics
    # row (the metrics block runs after the gate), so this is their only record.
    "ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS error_message TEXT",
    "CREATE INDEX IF NOT EXISTS idx_test_runs_org_created"
    " ON test_runs (org_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_test_runs_user_created"
    " ON test_runs (user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_test_runs_created"
    " ON test_runs (created_at DESC)",
    # --- run groups (History page folders) ---
    # Keyed to the ORG, with a per-folder visibility flag. No DROP may ever
    # appear here: this tuple runs on EVERY RunRegistry() construction (see
    # __init__), so a DROP would delete every folder on each process start
    # and each test fixture, and would fail outright once T4's foreign key
    # references the table. The pre-release per-user table (0 rows, never
    # shipped) is dropped by hand instead — CREATE TABLE IF NOT EXISTS would
    # otherwise silently keep the old column set.
    """
    CREATE TABLE IF NOT EXISTS run_groups (
        group_id   TEXT PRIMARY KEY,
        org_id     TEXT NOT NULL,
        created_by TEXT NOT NULL,
        name       TEXT NOT NULL,
        visibility TEXT NOT NULL DEFAULT 'org'
                   CHECK (visibility IN ('private','org')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Two PARTIAL unique indexes, not one. A single (org_id, lower(name))
    # index would let a member's private folder block the org from creating
    # a folder of that name, and the 409 would reveal that a private folder
    # by that name exists — an existence leak in the one feature whose whole
    # point is privacy. Cost: a private "Checkout" and an org "Checkout" can
    # coexist in one list, so the UI marks private folders (T5).
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_run_groups_org_name"
    " ON run_groups (org_id, lower(name)) WHERE visibility = 'org'",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_run_groups_private_name"
    " ON run_groups (created_by, lower(name)) WHERE visibility = 'private'",
    "ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS group_id TEXT",
    "CREATE INDEX IF NOT EXISTS idx_test_runs_group ON test_runs (group_id)",
    # ON DELETE SET NULL is what returns a deleted folder's runs to
    # Ungrouped, and it is what stops assign_runs writing a group_id that a
    # concurrent delete_group has just removed. ADD CONSTRAINT is NOT
    # idempotent, and this tuple runs on every construction, so it needs the
    # guard. conrelid is load-bearing: the suite runs on isolated schemas
    # whose search_path ends in public, and a conname-only guard would find
    # public's constraint and skip the ALTER, leaving every test schema
    # without it. 'test_runs'::regclass resolves through search_path, so the
    # guard is per-schema.
    """
    DO $$
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_constraint
                     WHERE conrelid = 'test_runs'::regclass
                       AND contype = 'f' AND conname = 'fk_test_runs_group') THEN
        ALTER TABLE test_runs ADD CONSTRAINT fk_test_runs_group
          FOREIGN KEY (group_id) REFERENCES run_groups(group_id)
          ON DELETE SET NULL;
      END IF;
    END $$
    """,
)


_VISIBILITIES = ("org", "private")


class DuplicateGroupName(Exception):
    """A group with this name already exists in the scope that owns the name
    (case-insensitive): the org for an 'org' folder, the creator for a
    'private' one."""


class GroupVisibilityConflict(Exception):
    """'org' -> 'private' refused: the folder still holds runs owned by other
    members, who would lose sight of their own runs the moment it turns
    private. Carries the COUNT only — never the owners, which would leak who
    else works in the folder."""

    def __init__(self, count: int):
        self.count = count
        super().__init__(
            "1 run by another member is in this folder" if count == 1
            else f"{count} runs by other members are in this folder"
        )


class RunRegistry:
    """Postgres-backed registry of test runs for history + report ownership."""

    def __init__(self, dsn: str = None):
        self.dsn = dsn or settings.DATABASE_URL
        setup = psycopg.connect(
            self.dsn, autocommit=True, connect_timeout=PG_CONNECT_TIMEOUT_S)
        try:
            for ddl in _SCHEMA_DDL:
                setup.execute(ddl)
        finally:
            setup.close()
        self._pool = ConnectionPool(
            conninfo=self.dsn, min_size=1, max_size=4,
            kwargs={"connect_timeout": PG_CONNECT_TIMEOUT_S,
                    "row_factory": dict_row},
            open=True)
        logger.info("[RUN_REGISTRY] Postgres test-runs registry ready")

    def close(self) -> None:
        """Close the pool (tests; the app singleton lives for the process)."""
        global _run_registry
        self._pool.close()
        with _run_registry_init_lock:
            if _run_registry is self:
                _run_registry = None

    def _lookup_org_id(self, user_id: str) -> str | None:
        """Resolve org_id for user_id from org_members. No org_role filter —
        a team org_member has exactly one membership too (the single-active-
        org invariant), and filtering on org_role='org_admin' silently drops
        every team member's runs. ORDER BY created_at LIMIT 1 so a schema
        that permits more than one row per user (org_members' PK is
        (org_id, user_id), not user_id alone — only application code enforces
        single membership) resolves the same way production's
        get_orgs_for_user()/_token_payload does (oldest first, [0]).

        org_members.user_id is uuid while test_runs.user_id is text, so a
        non-UUID user_id would raise InvalidTextRepresentation if it reached
        the query — checked up front instead, so that expected synthetic
        traffic (dev fixtures, AUTH_ENFORCED=false runs) short-circuits
        without a doomed round trip, and the WARNING below stays reserved for
        genuinely unexpected lookup failures (pool exhausted, org_members
        missing from the search path, DB unreachable) rather than being
        drowned out by routine non-UUID ids. The query itself still runs on
        its own pool connection, isolated from record_start's INSERT, so any
        surprise failure here can never poison the INSERT's transaction
        (which would otherwise fail the row write entirely — worse than the
        NULL org_id being fixed)."""
        try:
            uuid.UUID(user_id)
        except ValueError:
            return None
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "SELECT org_id FROM org_members WHERE user_id = %s "
                    "ORDER BY created_at LIMIT 1",
                    (user_id,),
                ).fetchone()
            return str(row["org_id"]) if row else None
        except Exception as e:
            logger.warning(
                "[RUN_REGISTRY] org_id lookup failed for user %s: %s", user_id, e)
            return None

    def _fileable_group_id(
        self, group_id: str, org_id: Optional[str], user_id: Optional[str]
    ) -> Optional[str]:
        """group_id when a run owned by user_id in org_id may be FILED into
        that folder, else None — the same predicate assign_runs enforces
        (folder in the org, and a private folder takes only its creator's
        runs), applied at the write instead of at the read.

        It has to be the whole predicate, and it has to be keyed on the NEW
        ROW'S OWNER rather than on whoever read the source. A re-run inherits
        the folder of the run it was cloned from, and a validated platform
        admin reads that source row through the UNFILTERED group join (see
        _group_join, and history_scope, which hands them org_id None), so the
        id arriving here can name ANY org's folder and any user's private one.
        An org-only check catches the cross-org half and misses the rest: a
        platform admin inside the member's own org would file their own new
        run into that member's private folder — a run sitting where its owner
        cannot see it, which is exactly what this feature forbids.

        Only record_start can make this decision, because only it knows the
        org and owner actually written on the new row — _lookup_org_id can
        supply the org when the token did not. An org-less row (AUTH_ENFORCED
        off) matches no folder at all: run_groups.org_id is NOT NULL, so there
        is nothing for it to equal, and filing an unowned run into someone's
        folder is the worse answer.

        Runs on its OWN pool connection and swallows its own errors, exactly
        like _lookup_org_id: this decides a folder tag, and nothing about a
        folder tag may cost the history row it decorates."""
        if org_id is None:
            return None
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "SELECT 1 FROM run_groups WHERE group_id = %s AND org_id = %s "
                    "AND (visibility = 'org' OR created_by = %s)",
                    (group_id, org_id, user_id),
                ).fetchone()
            return group_id if row else None
        except Exception as e:
            logger.warning(
                "[RUN_REGISTRY] group visibility check failed for %s: %s",
                group_id, e)
            return None

    def record_start(
        self,
        run_id: str,
        user: Optional[Dict[str, Any]],
        user_query: Optional[str],
        status: str,
        robot_code: Optional[str] = None,
        rerun_of: Optional[str] = None,
        error_message: Optional[str] = None,
        group_id: Optional[str] = None,
    ) -> None:
        """Upsert a run row. Ownership/query/lineage are write-once (COALESCE
        keeps the first non-NULL value); status and updated_at always advance.
        robot_code is newest-non-NULL-wins: when the user edits generated code
        and then executes, the execute upsert must replace the generation-time
        code so the row holds what actually ran ("Run again" re-executes it
        verbatim). rerun_of links a re-run to its ORIGINAL run (root-flattened
        by the caller) — the run whose learning record user feedback should
        update, since re-run executions skip learning. error_message follows the
        same newest-non-NULL-wins rule as robot_code: a run that fails, is
        retried and succeeds keeps the reason it failed the first time.

        org_id is used verbatim when the caller supplies one; when it is
        absent and a user_id is present, _lookup_org_id derives it before the
        INSERT runs — insurance against a login-time failure that mints an
        org-less token (see _lookup_org_id for why this must not share the
        INSERT's connection).

        group_id files the new run into a folder. Only the History "Run
        again" path sets it, inheriting the folder of the run it cloned; it
        is write-once like ownership, so a later record_start cannot drag a
        run the user moved mid-flight back to the source folder."""
        try:
            user_id = (user or {}).get("user_id")
            org_id = (user or {}).get("org_id")
            if org_id is None and user_id:
                org_id = self._lookup_org_id(user_id)
            if group_id is not None:
                group_id = self._fileable_group_id(group_id, org_id, user_id)
            sql = """
                INSERT INTO test_runs
                    (run_id, user_id, user_email, user_query, robot_code, rerun_of, status, org_id, error_message, group_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id) DO UPDATE SET
                    status     = EXCLUDED.status,
                    updated_at = now(),
                    user_id    = COALESCE(test_runs.user_id, EXCLUDED.user_id),
                    user_email = COALESCE(test_runs.user_email, EXCLUDED.user_email),
                    user_query = COALESCE(test_runs.user_query, EXCLUDED.user_query),
                    robot_code = COALESCE(EXCLUDED.robot_code, test_runs.robot_code),
                    rerun_of   = COALESCE(test_runs.rerun_of, EXCLUDED.rerun_of),
                    org_id     = COALESCE(test_runs.org_id, EXCLUDED.org_id),
                    error_message = COALESCE(EXCLUDED.error_message, test_runs.error_message),
                    group_id   = COALESCE(test_runs.group_id, EXCLUDED.group_id)
                """

            def _params(gid: Optional[str]) -> tuple:
                return (
                    run_id,
                    user_id,
                    (user or {}).get("email"),
                    user_query,
                    robot_code,
                    rerun_of,
                    status,
                    org_id,
                    error_message,
                    gid,
                )

            with self._pool.connection() as conn:
                try:
                    conn.execute(sql, _params(group_id))
                except psycopg.errors.ForeignKeyViolation:
                    # delete_group removed the folder between the check above
                    # and this INSERT. fk_test_runs_group turns that into a
                    # violation the outer except would swallow, leaving NO
                    # history row at all — strictly worse than a missing
                    # folder tag. Retry ungrouped, reusing the org_id already
                    # derived above rather than looking it up again.
                    # rollback() first: the pool's context manager COMMITs on
                    # exit, and the aborted transaction would take the retry
                    # down with it (same reason assign_runs rolls back).
                    conn.rollback()
                    logger.warning(
                        "[RUN_REGISTRY] folder %s vanished mid-write; "
                        "recording run %s ungrouped", group_id, run_id)
                    conn.execute(sql, _params(None))
        except Exception as e:
            logger.error(f"[RUN_REGISTRY] record_start failed for {run_id}: {e}")

    def set_status(self, run_id: str, status: str) -> None:
        """Advance a run's status (no-op if the row was never recorded)."""
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    "UPDATE test_runs SET status = %s, updated_at = now() "
                    "WHERE run_id = %s",
                    (status, run_id),
                )
        except Exception as e:
            logger.error(f"[RUN_REGISTRY] set_status failed for {run_id}: {e}")

    # ------------------------------------------------------------------
    # Run groups (History page folders). Keyed to the ORG, each folder
    # carrying a visibility: 'org' (everyone in the org sees it) or
    # 'private' (only its creator). User-facing CRUD: these methods
    # PROPAGATE storage errors (the swallow-everything discipline above
    # exists to protect the generation pipeline, not this UI path) —
    # DuplicateGroupName, GroupVisibilityConflict and every 409/404 depend
    # on the exception escaping.
    #
    # Authority:
    #   create                    anyone in the org
    #   rename / flip / delete    the creator, or an org_admin on an 'org'
    #                             folder — never on a private one, which
    #                             they cannot see and whose existence
    #                             acting on it would leak
    #   file a run                the folder must be visible to the caller
    #                             AND the run must be the caller's own, or
    #                             the caller is org_admin and the run is in
    #                             their org; a PRIVATE folder additionally
    #                             takes only its creator's runs, so a run
    #                             can never land where its owner cannot see
    #                             it
    # A caller who may not act gets False (the endpoints render that as 404,
    # never 403), so no refusal reveals that a folder exists.
    # ------------------------------------------------------------------

    def list_groups(
        self,
        org_id: Optional[str],
        user_id: Optional[str],
        scope_user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Folders visible to the caller, name-sorted, each with its
        member-run count: the caller's org's 'org' folders plus their own
        private ones. org_id None is the unscoped caller (platform admin, or
        token-less with AUTH_ENFORCED off) — they already see every org's
        runs, so no visibility filter applies; filtering on a NULL org would
        evaluate to NULL for every row and hide the lot.

        user_id is the caller's IDENTITY (which private folders are theirs);
        scope_user_id is the run-list scope run_count is counted within —
        None means the whole org, exactly as list_runs(user_id=None) does for
        an org_admin. The two differ for an org_admin, whose filter user_id
        is None while their identity is not, and run_count MUST use the same
        predicates list_runs uses or the chip disagrees with the table.

        The run scope goes in the JOIN condition, not the WHERE clause: in
        the WHERE it would turn the LEFT JOIN into an inner one and drop
        every folder that holds none of the caller's runs."""
        join, join_params = "", []
        if scope_user_id is not None:
            join += " AND t.user_id = %s"
            join_params.append(scope_user_id)
        if org_id is not None:
            join += " AND t.org_id = %s"
            join_params.append(org_id)
        where, where_params = "", []
        if org_id is not None:
            where = ("WHERE g.org_id = %s "
                     "  AND (g.visibility = 'org' OR g.created_by = %s) ")
            where_params = [org_id, user_id]
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT g.group_id, g.name, g.visibility, g.created_by, "
                "       g.created_at, g.updated_at, "
                "       COUNT(t.run_id) AS run_count "
                "FROM run_groups g "
                f"LEFT JOIN test_runs t ON t.group_id = g.group_id{join} "
                f"{where}"
                "GROUP BY g.group_id ORDER BY lower(g.name)",
                join_params + where_params,
            ).fetchall()
        out = []
        for r in rows:
            r = dict(r)
            r["created_at"] = r["created_at"].isoformat()
            r["updated_at"] = r["updated_at"].isoformat()
            out.append(r)
        return out

    def _mutable_group(
        self,
        conn,
        org_id: Optional[str],
        user_id: str,
        is_org_admin: bool,
        group_id: str,
    ) -> Optional[Dict[str, Any]]:
        """The folder row when this caller may MUTATE it, else None (callers
        turn None into a 404, never a 403). Mutating is stricter than seeing:
        the creator always may, an org_admin only on an 'org' folder inside a
        concrete org — never on a private one, which _visible_group has
        already hidden from them."""
        row = self._visible_group(conn, org_id, user_id, group_id)
        if row is None:
            return None
        if row["created_by"] == user_id:
            return row
        if is_org_admin and org_id is not None and row["visibility"] == "org":
            return row
        return None

    def _visible_group(
        self,
        conn,
        org_id: Optional[str],
        user_id: str,
        group_id: str,
    ) -> Optional[Dict[str, Any]]:
        """The folder row when this caller can SEE it, else None: it is in
        their org and is either an 'org' folder or their own private one.
        Seeing a folder is what lets a caller file runs into it — mutating
        it is a stricter test (_mutable_group)."""
        row = conn.execute(
            "SELECT group_id, org_id, created_by, name, visibility "
            "FROM run_groups WHERE group_id = %s",
            (group_id,),
        ).fetchone()
        if row is None:
            return None
        if org_id is not None and row["org_id"] != org_id:
            return None
        if row["visibility"] != "org" and row["created_by"] != user_id:
            return None
        return row

    def create_group(
        self,
        org_id: str,
        user_id: str,
        name: str,
        visibility: str = "org",
    ) -> Dict[str, Any]:
        """Create a folder in the caller's org — anyone in the org may.
        Raises DuplicateGroupName on a case-insensitive name collision in
        whichever scope owns the name: the org for an 'org' folder, the
        creator for a 'private' one."""
        if visibility not in _VISIBILITIES:
            raise ValueError(f"invalid visibility: {visibility!r}")
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "INSERT INTO run_groups "
                    "    (group_id, org_id, created_by, name, visibility) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "RETURNING group_id, name, visibility, created_by, "
                    "          created_at, updated_at",
                    (str(uuid.uuid4()), org_id, user_id, name, visibility),
                ).fetchone()
        except psycopg.errors.UniqueViolation:
            raise DuplicateGroupName(name)
        row = dict(row)
        row["run_count"] = 0
        row["created_at"] = row["created_at"].isoformat()
        row["updated_at"] = row["updated_at"].isoformat()
        return row

    def rename_group(
        self,
        org_id: Optional[str],
        user_id: str,
        is_org_admin: bool,
        group_id: str,
        name: Optional[str] = None,
        visibility: Optional[str] = None,
    ) -> bool:
        """Rename a folder and/or change its visibility (at least one).

        False when the folder doesn't exist or the caller may not mutate it.
        Raises DuplicateGroupName when the new name collides — rename needs
        its OWN guard, create's does not cover it. Raises
        GroupVisibilityConflict on 'org' -> 'private' while the folder still
        holds runs owned by anyone other than its creator: those members
        would silently lose sight of their own runs, and a refusal they can
        act on beats quiet data movement behind a settings toggle.
        'private' -> 'org' only widens visibility and is always allowed."""
        if name is None and visibility is None:
            raise ValueError("rename_group needs a name or a visibility")
        if visibility is not None and visibility not in _VISIBILITIES:
            raise ValueError(f"invalid visibility: {visibility!r}")
        collision_name = name
        try:
            with self._pool.connection() as conn:
                row = self._mutable_group(
                    conn, org_id, user_id, is_org_admin, group_id)
                if row is None:
                    return False
                if collision_name is None:
                    collision_name = row["name"]
                if visibility == "private" and row["visibility"] == "org":
                    others = conn.execute(
                        "SELECT COUNT(*) AS n FROM test_runs "
                        "WHERE group_id = %s AND user_id IS DISTINCT FROM %s",
                        (group_id, row["created_by"]),
                    ).fetchone()["n"]
                    if others:
                        raise GroupVisibilityConflict(others)
                sets, params = [], []
                if name is not None:
                    sets.append("name = %s")
                    params.append(name)
                if visibility is not None:
                    sets.append("visibility = %s")
                    params.append(visibility)
                params.append(group_id)
                cur = conn.execute(
                    f"UPDATE run_groups SET {', '.join(sets)}, updated_at = now() "
                    "WHERE group_id = %s",
                    params,
                )
                return cur.rowcount == 1
        except psycopg.errors.UniqueViolation:
            raise DuplicateGroupName(collision_name)

    def delete_group(
        self,
        org_id: Optional[str],
        user_id: str,
        is_org_admin: bool,
        group_id: str,
    ) -> bool:
        """Delete a folder and return its member runs to Ungrouped. Runs are
        NEVER deleted: fk_test_runs_group is ON DELETE SET NULL, so Postgres
        ungroups the members as part of the DELETE — no second statement,
        and no window in which a run points at a folder that is gone. False
        when the folder doesn't exist or the caller may not mutate it."""
        with self._pool.connection() as conn:
            if self._mutable_group(
                    conn, org_id, user_id, is_org_admin, group_id) is None:
                return False
            cur = conn.execute(
                "DELETE FROM run_groups WHERE group_id = %s", (group_id,)
            )
            if cur.rowcount != 1:
                return False
            return True

    def count_ungrouped(
        self,
        user_id: Optional[str] = None,
        org_id: Optional[str] = None,
        caller_user_id: Optional[str] = None,
    ) -> int:
        """The Ungrouped chip's live count on the History page.

        Takes the caller's whole (user_id, org_id) scope and the same
        visibility join list_runs uses, so the chip always equals the total
        of list_runs(group="ungrouped") for that caller. A bare per-user
        count disagreed with the table for an org_admin, whose History spans
        the org. "Ungrouped" is g.group_id IS NULL — in no folder the caller
        can SEE — not t.group_id IS NULL, or a run inside someone else's
        private folder would belong to no filter at all and vanish."""
        join, params = self._group_join(org_id, caller_user_id)
        clauses = ["g.group_id IS NULL"]
        if user_id is not None:
            clauses.append("t.user_id = %s")
            params = params + [user_id]
        if org_id is not None:
            clauses.append("t.org_id = %s")
            params = params + [org_id]
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM test_runs t {join} "
                f"WHERE {' AND '.join(clauses)}",
                params,
            ).fetchone()
        return row["n"]

    def assign_runs(
        self,
        org_id: Optional[str],
        user_id: str,
        is_org_admin: bool,
        run_ids: List[str],
        group_id: Optional[str],
    ) -> bool:
        """Atomically file runs into a folder (group_id None = ungroup).

        The folder must be visible to the caller, and every run must be one
        the caller may file: their own, or — for an org_admin — any run in
        their org. A PRIVATE folder takes only its creator's runs, so a run
        can never land in a folder its own owner cannot see; that also makes
        an org_admin unable to sweep a member's run into their own private
        folder.

        All-or-nothing: filing is now a per-row authority decision, which is
        exactly when partial writes appear, so a batch containing one run the
        caller may not file writes NOTHING and returns False — the caller's
        own runs stay ungrouped too."""
        with self._pool.connection() as conn:
            owner_only = None  # set => only this user's runs may be filed
            if group_id is not None:
                g = self._visible_group(conn, org_id, user_id, group_id)
                if g is None:
                    return False
                if g["visibility"] != "org":
                    owner_only = g["created_by"]
            params: list = [group_id, list(run_ids)]
            if owner_only is not None:
                allowed = "user_id = %s"
                params.append(owner_only)
            elif is_org_admin and org_id is not None:
                allowed = "(user_id = %s OR org_id = %s)"
                params.extend([user_id, org_id])
            else:
                allowed = "user_id = %s"
                params.append(user_id)
            try:
                cur = conn.execute(
                    f"UPDATE test_runs SET group_id = %s "
                    f"WHERE run_id = ANY(%s) AND {allowed}",
                    params,
                )
            except psycopg.errors.ForeignKeyViolation:
                # Lost the race: delete_group removed the folder between the
                # visibility check and this write. Fail closed like any other
                # unusable folder — the endpoint turns False into a 404.
                conn.rollback()
                return False
            if cur.rowcount != len(set(run_ids)):
                conn.rollback()
                return False
            return True

    @staticmethod
    def _group_join(
        org_id: Optional[str], caller_user_id: Optional[str]
    ) -> Tuple[str, list]:
        """(SQL, params) for the ONE visibility-filtered LEFT JOIN every read
        derives its folder answers from: a row's folder tag is g.name, its
        folder id is g.group_id, and "in no folder I can see" is
        g.group_id IS NULL.

        Two forms, and both matter. org_id None is the unscoped caller — a
        platform admin, or token-less with AUTH_ENFORCED off — who already
        sees every org's runs, so the join carries no filter: binding their
        NULL org would make g.org_id = NULL evaluate to NULL for every row,
        hide every folder name and collapse the whole page into Ungrouped.
        Otherwise the caller's org plus their own identity, which is
        caller_user_id and never the scoping user_id — an org_admin's filter
        user_id is None while their identity is not, and a None there would
        silently hide their own private folders.

        The returned params bind BEFORE any WHERE-clause params, because
        these placeholders sit earlier in the SQL text and psycopg binds %s
        strictly by position."""
        if org_id is None:
            return "LEFT JOIN run_groups g ON g.group_id = t.group_id", []
        return (
            "LEFT JOIN run_groups g ON g.group_id = t.group_id"
            " AND g.org_id = %s"
            " AND (g.visibility = 'org' OR g.created_by = %s)",
            [org_id, caller_user_id],
        )

    def list_runs(
        self,
        user_id: Optional[str] = None,
        org_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        status: Optional[str] = None,
        q: Optional[str] = None,
        group: Optional[str] = None,
        caller_user_id: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Most-recent-first run rows + total count. user_id=None lists all
        users' runs (admin scope); otherwise only that user's rows. status
        narrows both the rows and the total to one run status, so the History
        tabs paginate and count within their own filter. q is a case-insensitive
        substring match over the description, owner email and run id — applied
        server-side so it spans ALL of a user's runs, not just the loaded page,
        and so rows/total/pagination stay consistent with the active search.
        group narrows to one folder (a run_groups id) or, with the literal
        "ungrouped", to rows in no folder the caller can SEE; rows carry
        group_id and group_name from _group_join so the History table renders
        folder tags without extra requests. caller_user_id is the caller's own
        identity for that join — see _group_join for why it is not user_id."""
        join, join_params = self._group_join(org_id, caller_user_id)
        clauses: list = []
        params: list = []
        if user_id is not None:
            clauses.append("t.user_id = %s")
            params.append(user_id)
        if org_id is not None:
            clauses.append("t.org_id = %s")
            params.append(org_id)
        if status is not None:
            clauses.append("t.status = %s")
            params.append(status)
        if group == "ungrouped":
            clauses.append("g.group_id IS NULL")
        elif group is not None:
            clauses.append("g.group_id = %s")
            params.append(group)
        if q:
            # Escape LIKE wildcards so a typed % / _ matches literally (default
            # ESCAPE is backslash); the search box is substring, not glob.
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            clauses.append(
                "(t.user_query ILIKE %s OR t.user_email ILIKE %s OR t.run_id ILIKE %s)"
            )
            params.extend([like, like, like])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        # Join params FIRST: their placeholders come earlier in the SQL text
        # than the WHERE clause's, and psycopg binds %s strictly by position —
        # get this backwards and the query filters on the wrong values without
        # raising. The COUNT carries the same join, so it takes them too.
        params = join_params + params
        try:
            with self._pool.connection() as conn:
                total = conn.execute(
                    f"SELECT COUNT(*) AS n FROM test_runs t {join}{where}", params
                ).fetchone()["n"]
                rows = conn.execute(
                    f"SELECT t.run_id, t.user_id, t.user_email, t.user_query, "
                    f"       t.rerun_of, t.status, t.created_at, t.updated_at, "
                    f"       g.group_id, g.name AS group_name "
                    f"FROM test_runs t "
                    f"{join}"
                    f"{where} "
                    f"ORDER BY t.created_at DESC LIMIT %s OFFSET %s",
                    params + [limit, offset],
                ).fetchall()
            out = []
            for r in rows:
                r = dict(r)
                r["created_at"] = r["created_at"].isoformat()
                r["updated_at"] = r["updated_at"].isoformat()
                out.append(r)
            return out, total
        except Exception as e:
            logger.error(f"[RUN_REGISTRY] list_runs failed: {e}")
            return [], 0

    def get_owner(self, run_id: str) -> Optional[str]:
        """The user_id that owns run_id, or None (unknown run, unattributed
        legacy run, or storage error — all treated as 'not yours')."""
        user_id, _ = self.get_run_owner(run_id)
        return user_id

    def get_run_owner(self, run_id: str) -> Tuple[Optional[str], Optional[str]]:
        """(user_id, org_id) for the ownership predicate, or (None, None) on
        unknown run / storage error — both treated as 'not yours'."""
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "SELECT user_id, org_id FROM test_runs WHERE run_id = %s",
                    (run_id,),
                ).fetchone()
            return (row["user_id"], row["org_id"]) if row else (None, None)
        except Exception as e:
            logger.error(f"[RUN_REGISTRY] get_run_owner failed for {run_id}: {e}")
            return (None, None)

    def backfill_org_ids(self) -> int:
        """Set org_id on rows that have a user_id but no org_id, from that
        user's org_members row. Idempotent; returns rows updated.

        No org_role filter — a team org_member has exactly one membership too
        (the single-active-org invariant), and filtering on org_role=
        'org_admin' silently drops every team member's rows (measured: the
        predicate returned [] for a real org_member on the dev DB). Assumes
        the single-active-org invariant, so the join resolves to one org per
        user — more exposed than before, now that dropping the role filter
        means ANY org_members row for that user satisfies the join. When
        many-to-many org membership lands, this must target the user's
        actual active org explicitly (e.g. a dedicated lookup ordered like
        get_orgs_for_user, not a bare join) instead of trusting org_members
        to return exactly one row, to stay deterministic."""
        try:
            with self._pool.connection() as conn:
                cur = conn.execute(
                    "UPDATE test_runs t SET org_id = m.org_id "
                    "FROM org_members m "
                    "WHERE t.org_id IS NULL AND t.user_id IS NOT NULL "
                    "  AND m.user_id::text = t.user_id"
                )
                n = cur.rowcount
                conn.commit()
            if n:
                logger.info("[RUN_REGISTRY] backfilled org_id on %d run(s)", n)
            return n
        except Exception as e:
            logger.error(f"[RUN_REGISTRY] backfill_org_ids failed: {e}")
            return 0

    def get_run(
        self,
        run_id: str,
        org_id: Optional[str] = None,
        caller_user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Full row for one run (including robot_code), or None if unknown
        or on storage error (callers treat both as 'not found').

        org_id/caller_user_id are the CALLER's scope, not the row's, and feed
        _group_join: a run sitting in a folder this caller cannot see reports
        NULL for both group_id and group_name, so the drawer never shows a
        private folder's name nor a folder id the caller cannot resolve. They
        default to the unscoped form, which is what the feedback path in
        api/endpoints.py wants — it reads ownership and lineage, never the
        group fields. The rerun path DOES pass a scope: a re-run inherits its
        source run's folder, so reading that folder unscoped would file the
        new run somewhere its own owner cannot see it."""
        join, params = self._group_join(org_id, caller_user_id)
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "SELECT t.run_id, t.user_id, t.user_email, t.user_query, "
                    "       t.robot_code, t.rerun_of, t.status, t.org_id, "
                    "       t.created_at, t.updated_at, "
                    "       g.group_id, g.name AS group_name "
                    "FROM test_runs t "
                    f"{join} "
                    "WHERE t.run_id = %s",
                    params + [run_id],
                ).fetchone()
            if not row:
                return None
            row = dict(row)
            row["created_at"] = row["created_at"].isoformat()
            row["updated_at"] = row["updated_at"].isoformat()
            return row
        except Exception as e:
            logger.error(f"[RUN_REGISTRY] get_run failed for {run_id}: {e}")
            return None


_run_registry: Optional[RunRegistry] = None
_run_registry_init_lock = Lock()


def get_run_registry() -> RunRegistry:
    """Process-wide registry instance (thread-safe double-checked init)."""
    global _run_registry
    if _run_registry is None:
        with _run_registry_init_lock:
            if _run_registry is None:
                _run_registry = RunRegistry()
    return _run_registry

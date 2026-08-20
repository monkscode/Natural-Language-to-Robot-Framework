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
    # --- personal run groups (History page folders) ---
    """
    CREATE TABLE IF NOT EXISTS run_groups (
        group_id   TEXT PRIMARY KEY,
        user_id    TEXT NOT NULL,
        name       TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_run_groups_user_name"
    " ON run_groups (user_id, lower(name))",
    "ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS group_id TEXT",
    "CREATE INDEX IF NOT EXISTS idx_test_runs_group ON test_runs (group_id)",
)


class DuplicateGroupName(Exception):
    """The user already has a group with this name (case-insensitive)."""


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

    def _lookup_org_id(self, user_id: str) -> Optional[str]:
        """Resolve org_id for user_id from org_members. No org_role filter —
        a team org_member has exactly one membership too (the single-active-
        org invariant), and filtering on org_role='org_admin' silently drops
        every team member's runs.

        Runs on its own pool connection, isolated from record_start's INSERT:
        org_members.user_id is uuid while test_runs.user_id is text, so a
        non-UUID user_id raises InvalidTextRepresentation here — caught
        locally so it can never poison the INSERT's transaction (which would
        otherwise fail the row write entirely, worse than the NULL org_id
        being fixed)."""
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "SELECT org_id FROM org_members WHERE user_id = %s",
                    (user_id,),
                ).fetchone()
            return str(row["org_id"]) if row else None
        except Exception as e:
            logger.warning(
                "[RUN_REGISTRY] org_id lookup failed for user %s: %s", user_id, e)
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
        INSERT's connection)."""
        try:
            user_id = (user or {}).get("user_id")
            org_id = (user or {}).get("org_id")
            if org_id is None and user_id:
                org_id = self._lookup_org_id(user_id)
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO test_runs
                        (run_id, user_id, user_email, user_query, robot_code, rerun_of, status, org_id, error_message)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (run_id) DO UPDATE SET
                        status     = EXCLUDED.status,
                        updated_at = now(),
                        user_id    = COALESCE(test_runs.user_id, EXCLUDED.user_id),
                        user_email = COALESCE(test_runs.user_email, EXCLUDED.user_email),
                        user_query = COALESCE(test_runs.user_query, EXCLUDED.user_query),
                        robot_code = COALESCE(EXCLUDED.robot_code, test_runs.robot_code),
                        rerun_of   = COALESCE(test_runs.rerun_of, EXCLUDED.rerun_of),
                        org_id     = COALESCE(test_runs.org_id, EXCLUDED.org_id),
                        error_message = COALESCE(EXCLUDED.error_message, test_runs.error_message)
                    """,
                    (
                        run_id,
                        user_id,
                        (user or {}).get("email"),
                        user_query,
                        robot_code,
                        rerun_of,
                        status,
                        org_id,
                        error_message,
                    ),
                )
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
    # Personal run groups (History page folders). User-facing CRUD: these
    # methods PROPAGATE storage errors (the swallow-everything discipline
    # above exists to protect the generation pipeline, not this UI path).
    # ------------------------------------------------------------------

    def list_groups(self, user_id: str) -> List[Dict[str, Any]]:
        """The user's groups, name-sorted, each with its member-run count."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT g.group_id, g.name, g.created_at, g.updated_at, "
                "       COUNT(t.run_id) AS run_count "
                "FROM run_groups g "
                "LEFT JOIN test_runs t ON t.group_id = g.group_id "
                "WHERE g.user_id = %s "
                "GROUP BY g.group_id ORDER BY lower(g.name)",
                (user_id,),
            ).fetchall()
        out = []
        for r in rows:
            r = dict(r)
            r["created_at"] = r["created_at"].isoformat()
            r["updated_at"] = r["updated_at"].isoformat()
            out.append(r)
        return out

    def create_group(self, user_id: str, name: str) -> Dict[str, Any]:
        """Create a group; raises DuplicateGroupName on a per-user,
        case-insensitive name collision."""
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "INSERT INTO run_groups (group_id, user_id, name) "
                    "VALUES (%s, %s, %s) "
                    "RETURNING group_id, name, created_at, updated_at",
                    (str(uuid.uuid4()), user_id, name),
                ).fetchone()
        except psycopg.errors.UniqueViolation:
            raise DuplicateGroupName(name)
        row = dict(row)
        row["run_count"] = 0
        row["created_at"] = row["created_at"].isoformat()
        row["updated_at"] = row["updated_at"].isoformat()
        return row

    def rename_group(self, user_id: str, group_id: str, name: str) -> bool:
        """False when the group doesn't exist or isn't the caller's; raises
        DuplicateGroupName when the new name collides with another group."""
        try:
            with self._pool.connection() as conn:
                cur = conn.execute(
                    "UPDATE run_groups SET name = %s, updated_at = now() "
                    "WHERE group_id = %s AND user_id = %s",
                    (name, group_id, user_id),
                )
                return cur.rowcount == 1
        except psycopg.errors.UniqueViolation:
            raise DuplicateGroupName(name)

    def delete_group(self, user_id: str, group_id: str) -> bool:
        """Delete a group and return its member runs to Ungrouped — one
        transaction, so a failure leaves both tables untouched. Runs are
        NEVER deleted. False when the group doesn't exist / isn't the
        caller's."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                "DELETE FROM run_groups WHERE group_id = %s AND user_id = %s",
                (group_id, user_id),
            )
            if cur.rowcount != 1:
                return False
            conn.execute(
                "UPDATE test_runs SET group_id = NULL WHERE group_id = %s",
                (group_id,),
            )
            return True

    def count_ungrouped(self, user_id: str) -> int:
        """How many of the user's runs are in no group — the Ungrouped chip's
        live count on the History page."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM test_runs "
                "WHERE user_id = %s AND group_id IS NULL",
                (user_id,),
            ).fetchone()
        return row["n"]

    def assign_runs(
        self, user_id: str, run_ids: List[str], group_id: Optional[str]
    ) -> bool:
        """Atomically set group_id on the caller's runs (None = ungroup).
        All-or-nothing: unless the target group (when non-null) AND every
        run id belong to user_id, nothing is written and False is returned —
        an org-admin can SEE members' runs in history but can never file
        someone else's run into a group."""
        with self._pool.connection() as conn:
            if group_id is not None:
                owned = conn.execute(
                    "SELECT 1 FROM run_groups WHERE group_id = %s AND user_id = %s",
                    (group_id, user_id),
                ).fetchone()
                if not owned:
                    return False
            cur = conn.execute(
                "UPDATE test_runs SET group_id = %s "
                "WHERE run_id = ANY(%s) AND user_id = %s",
                (group_id, run_ids, user_id),
            )
            if cur.rowcount != len(set(run_ids)):
                conn.rollback()
                return False
            return True

    def list_runs(
        self,
        user_id: Optional[str] = None,
        org_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        status: Optional[str] = None,
        q: Optional[str] = None,
        group: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Most-recent-first run rows + total count. user_id=None lists all
        users' runs (admin scope); otherwise only that user's rows. status
        narrows both the rows and the total to one run status, so the History
        tabs paginate and count within their own filter. q is a case-insensitive
        substring match over the description, owner email and run id — applied
        server-side so it spans ALL of a user's runs, not just the loaded page,
        and so rows/total/pagination stay consistent with the active search.
        group narrows to one personal group (a run_groups id) or, with the
        literal "ungrouped", to rows with no group; rows carry group_id and
        group_name (LEFT JOIN) so the History table renders folder tags
        without extra requests."""
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
            clauses.append("t.group_id IS NULL")
        elif group is not None:
            clauses.append("t.group_id = %s")
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
        try:
            with self._pool.connection() as conn:
                total = conn.execute(
                    f"SELECT COUNT(*) AS n FROM test_runs t{where}", params
                ).fetchone()["n"]
                rows = conn.execute(
                    f"SELECT t.run_id, t.user_id, t.user_email, t.user_query, "
                    f"       t.rerun_of, t.status, t.created_at, t.updated_at, "
                    f"       t.group_id, g.name AS group_name "
                    f"FROM test_runs t "
                    f"LEFT JOIN run_groups g ON g.group_id = t.group_id"
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
        user."""
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

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Full row for one run (including robot_code), or None if unknown
        or on storage error (callers treat both as 'not found')."""
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "SELECT t.run_id, t.user_id, t.user_email, t.user_query, "
                    "       t.robot_code, t.rerun_of, t.status, t.org_id, "
                    "       t.created_at, t.updated_at, "
                    "       t.group_id, g.name AS group_name "
                    "FROM test_runs t "
                    "LEFT JOIN run_groups g ON g.group_id = t.group_id "
                    "WHERE t.run_id = %s",
                    (run_id,),
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

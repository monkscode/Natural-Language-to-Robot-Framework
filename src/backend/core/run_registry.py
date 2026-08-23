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
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

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
    # Keyed to the ORG, and to nothing finer: a folder is the org's, and the
    # runs inside it are the org's. No DROP of this TABLE may ever appear
    # here: this tuple runs on EVERY RunRegistry() construction (see
    # __init__), so one would delete every folder on each process start and
    # each test fixture, and would fail outright once the foreign key below
    # references the table. There are exactly TWO exceptions, both below, and
    # neither can destroy anything a user made: the pre-release per-user
    # TABLE (0 rows, never shipped) is dropped by the guarded block below,
    # because CREATE TABLE IF NOT EXISTS would otherwise silently keep the
    # old column set; and the superseded visibility INDEXES are dropped by
    # name inside the one-shot migration block, which carry no rows at all.
    #
    # ...and this is the ONE exception the comment above allows, because it
    # cannot delete anything anyone made. A database that ran PR #94's
    # pre-release branch still has the per-user run_groups
    # (group_id, name, user_id). CREATE TABLE IF NOT EXISTS is a no-op there,
    # so the partial indexes below fail with UndefinedColumn and
    # RunRegistry.__init__ raises. WHERE that raise lands depends on the
    # database, and both paths are real. On one whose data_migrations table
    # has no data_org_id_backfill row — every fresh deploy, every fresh CI
    # database — startup itself constructs the registry: main.py's
    # run_migration_once calls backfill_data_org_ids, which calls
    # get_run_registry() (core/org_backfill.py). The raise is caught by that
    # function's own per-table except, logged as "[ORG_BACKFILL] test_runs
    # backfill failed", and swallowed — so the migration is still marked
    # done and the process boots on a WARNING. On a database whose marker is
    # already set — which one carrying this pre-release table is, having
    # booted this app before — nothing constructs the registry at startup
    # and the raise surfaces in whichever request gets there first:
    # /api/history, /api/groups or a report-authorization check. Either way
    # nothing is cached on failure, so the next request retries construction
    # and raises again too. The caller sees a bare 500; the traceback naming
    # the cause reaches only the server log, never the response. The drop fires only
    # when the table carries the old column set AND holds zero rows — the
    # feature never shipped, so an old-shape table is always empty. A
    # non-empty one raises instead, and a human decides. Keyed on the
    # regclass, not on information_schema, so it can only ever see the
    # table search_path actually resolves — with a multi-schema
    # search_path, that can be a schema other than the first one listed.
    # The foreign key below cannot block this DROP: it arrived in 011bae5,
    # five commits after 1fc4d7d re-keyed the table to the org, so no
    # database can hold the old column set and the FK at once.
    """
    DO $$
    DECLARE
      t regclass := to_regclass('run_groups');
      n bigint;
    BEGIN
      IF t IS NULL THEN RETURN; END IF;
      IF NOT EXISTS (SELECT 1 FROM pg_attribute
                     WHERE attrelid = t AND attname = 'user_id'
                       AND NOT attisdropped) THEN
        RETURN;   -- already the org-keyed shape
      END IF;
      IF EXISTS (SELECT 1 FROM pg_attribute
                 WHERE attrelid = t AND attname = 'org_id'
                   AND NOT attisdropped) THEN
        RETURN;   -- neither shape we know; leave it alone
      END IF;
      EXECUTE format('SELECT count(*) FROM %s', t) INTO n;
      IF n <> 0 THEN
        RAISE EXCEPTION
          'run_groups has the pre-release per-user shape and % row(s); refusing to drop it automatically', n;
      END IF;
      EXECUTE format('DROP TABLE %s', t);
      RAISE NOTICE 'run_groups: dropped the empty pre-release per-user table';
    END $$;
    """,
    """
    CREATE TABLE IF NOT EXISTS run_groups (
        group_id   TEXT PRIMARY KEY,
        org_id     TEXT NOT NULL,
        created_by TEXT NOT NULL,
        name       TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # A folder name belongs to the ORG, case-insensitively: one name, one
    # folder, whoever created it, so a testcase can only ever carry one
    # folder name. There is no per-folder visibility any more, so this is a
    # single full unique index rather than the pair of partial ones the
    # private/org model needed. Owner decision D1/D2, 2026-08-23.
    #
    # Created here for a fresh database and, identically, inside the
    # migration block below for one that still carries `visibility` — so a
    # migrating database is never left even momentarily without folder-name
    # uniqueness. The IF NOT EXISTS then makes this statement a no-op there.
    #
    # --- migration off the private/org model -------------------------------
    # Only reachable while a `visibility` column is still present, which is
    # every database that ran this branch before 2026-08-23 and none that
    # ever ran a release: the feature has not shipped. Guarded on the column,
    # so it runs exactly ONCE per schema and then returns at the first IF.
    # That is what lets it use plain DROP INDEX IF EXISTS: the "skipping"
    # notice it can emit is a one-time event, not the every-process-start
    # WARNING the pre-2026-08-23 code contorted itself to avoid.
    #
    # Names are resolved BEFORE the unique index is built, because a private
    # "Login" and a shared "Login" could legitimately coexist under the old
    # pair of partial indexes and would now collide. The SHARED one keeps the
    # name — it is the one the org already knows — and any other row is
    # renamed with its own group_id, which cannot collide because it is the
    # primary key. Nothing is deleted and no folder loses its runs.
    #
    # Every object is schema-qualified through to_regclass: six registry
    # fixtures run on a `<isolated>,public` search_path, where a bare DROP
    # INDEX would walk past the isolated schema and delete public's index
    # from under a live app.
    """
    DO $$
    DECLARE
      t  regclass := to_regclass('run_groups');
      ns text;
      r  record;
      n  bigint := 0;
    BEGIN
      IF t IS NULL THEN RETURN; END IF;
      IF NOT EXISTS (SELECT 1 FROM pg_attribute
                     WHERE attrelid = t AND attname = 'visibility'
                       AND NOT attisdropped) THEN
        RETURN;   -- already the single-visibility shape
      END IF;
      SELECT n2.nspname INTO ns
        FROM pg_class c JOIN pg_namespace n2 ON n2.oid = c.relnamespace
       WHERE c.oid = t;

      FOR r IN
        SELECT group_id, name FROM (
          SELECT group_id, name,
                 row_number() OVER (PARTITION BY org_id, lower(name)
                                    ORDER BY (visibility = 'org') DESC,
                                             created_at, group_id) AS rn
            FROM run_groups
        ) ranked WHERE rn > 1
      LOOP
        UPDATE run_groups
           SET name = left(r.name, 47) || ' (' || left(r.group_id, 8) || ')',
               updated_at = now()
         WHERE group_id = r.group_id;
        n := n + 1;
      END LOOP;
      IF n > 0 THEN
        RAISE NOTICE 'run_groups: renamed % folder(s) whose name collided once folder visibility was removed', n;
      END IF;

      EXECUTE format('DROP INDEX IF EXISTS %I.idx_run_groups_org_name', ns);
      EXECUTE format('DROP INDEX IF EXISTS %I.idx_run_groups_private_org_name', ns);
      EXECUTE format('DROP INDEX IF EXISTS %I.idx_run_groups_private_name', ns);
      EXECUTE format('ALTER TABLE %s DROP COLUMN visibility', t);
      EXECUTE format(
        'CREATE UNIQUE INDEX idx_run_groups_org_name ON %s (org_id, lower(name))', t);
      RAISE NOTICE 'run_groups: folders are now org-wide; per-folder visibility removed';
    END $$;
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_run_groups_org_name"
    " ON run_groups (org_id, lower(name))",
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
    # The repair inside the branch is owner decision 6 (2026-08-21) — it runs
    # once per schema, not on every construction, which is what T4's parked
    # objection was about.
    """
    DO $$
    DECLARE
      n bigint;
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_constraint
                     WHERE conrelid = 'test_runs'::regclass
                       AND contype = 'f' AND conname = 'fk_test_runs_group') THEN
        -- Runs at most ONCE per schema: after the constraint lands, a
        -- dangling group_id is impossible by construction. A run whose
        -- folder is gone already reads as Ungrouped through the visibility
        -- join, so nothing is lost here — but without it ADD CONSTRAINT
        -- raises and __init__ raises. On a database that has not yet run
        -- the org_id backfill migration, that happens during startup,
        -- inside backfill_data_org_ids, which swallows it as a WARNING
        -- and boots anyway; on one that has, nothing constructs the
        -- registry at startup and the raise surfaces in whichever
        -- request gets there first: history, groups or a
        -- report-authorization check. Either way it is a bare 500 whose
        -- cause reaches only the server log, never the response.
        UPDATE test_runs SET group_id = NULL
         WHERE group_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM run_groups g
                           WHERE g.group_id = test_runs.group_id);
        GET DIAGNOSTICS n = ROW_COUNT;
        IF n > 0 THEN
          RAISE NOTICE 'test_runs: ungrouped % row(s) pointing at a folder that no longer exists', n;
        END IF;
        ALTER TABLE test_runs ADD CONSTRAINT fk_test_runs_group
          FOREIGN KEY (group_id) REFERENCES run_groups(group_id)
          ON DELETE SET NULL;
      END IF;
    END $$;
    """,
)


# The ONE run-visibility predicate, shared verbatim by list_runs and
# count_ungrouped so the table and the chips can never describe different
# sets. Binds exactly one parameter: the caller's user_id.
#
#     you see a run if you own it, OR the org published it into a folder
#
# So work in progress stays with whoever is doing it, and moving a run into a
# folder is the act of handing it to the team (owner decision, 2026-08-23).
#
# `t.user_id IS NOT NULL` on the second half is not decoration. An
# unattributed run (AUTH_ENFORCED off, or a legacy row) is readable only by a
# platform admin — a standing fail-closed rule, because /reports serves the
# credentials somebody typed into the script and no user owns that row. This
# predicate has to agree with auth/ownership.py exactly or the list would
# offer rows whose drawer and report then answer 403.
#
# "In a folder" is g.group_id — the folder _group_join RESOLVED for this
# caller — never the raw t.group_id column, and that is where the whole org
# term of the second half lives. The join binds the caller's own org, so a
# folder belonging to any other org resolves to NULL and publishes nothing
# (ownership.py rule 3 wants the caller in the folder's org), and a caller
# with an identity but no org resolves no folder at all, which collapses this
# to the bare owner check rule 4 asks for. Reading t.group_id here carried no
# org term whatsoever, and the `t.org_id = %s` clause the call sites add is
# not a substitute: it is absent for exactly the org-less caller, and it
# constrains the ROW's org rather than the FOLDER's.
_VISIBLE_RUN_SQL = (
    "(t.user_id = %s"
    " OR (g.group_id IS NOT NULL AND t.user_id IS NOT NULL))"
)

# The same fail-closed rule for the ONE caller _VISIBLE_RUN_SQL does not
# cover: an org_admin, whose filter user_id is None because their view spans
# the org. Without it their table listed unowned rows whose drawer and report
# then answered 404 — a list offering something no other endpoint would
# honour. Predates the org-visibility work; surfaced by it, because the table
# now shows an author column and an unowned row renders blank and unopenable.
_OWNED_RUN_SQL = "t.user_id IS NOT NULL"


class RunOwnership(NamedTuple):
    """What the authorization gates need to know about one run.

    A NamedTuple, and read by ATTRIBUTE at every call site, because this is
    the shape that bit: it used to be a bare 2-tuple, adding group_id made
    every `a, b = get_run_owner(...)` raise ValueError, and all four call
    sites sat inside a broad `except` that swallowed it. The result was not a
    crash but silence — llm_traces simply stopped being attributed to an org.

    Attribute access cannot drift that way. A fifth field tomorrow breaks
    nobody, and tests/test_core/test_run_registry_org.py pins the field names
    so a RENAME still fails loudly.
    """

    user_id: Optional[str]
    org_id: Optional[str]
    group_id: Optional[str]


class DuplicateGroupName(Exception):
    """A folder of this name already exists in the org (case-insensitive).

    The org owns the name, so this is raised for a collision with ANY
    member's folder — the copy cannot say "you already have", which would be
    false for a folder a colleague created. One name, one folder, so a
    testcase can only ever carry one folder name (owner decision D2)."""


def _log_schema_notice(diag: psycopg.errors.Diagnostic) -> None:
    """_SCHEMA_DDL's upgrade DO-blocks (e.g. the run_groups drop above) are
    the only place this schema mutates itself outside a migration, and a
    RAISE NOTICE is their only way to say so — psycopg discards notices with
    no handler registered. Never let logging break construction.

    Only OUR notices reach WARNING. Postgres raises one of its own for every
    IF NOT EXISTS no-op it re-runs ('relation "test_runs" already exists,
    skipping'), and _SCHEMA_DDL runs on EVERY construction — every process
    start and every test fixture — so logging those at WARNING put 15 lines
    per construction on the dashboards and left the two notices this handler
    exists to surface as 2 lines in 17. The split is the SQLSTATE, not the
    message text: a bare PL/pgSQL RAISE NOTICE carries 00000
    (successful_completion), while the skips carry duplicate-object codes
    (42P07 relation, 42701 column, 42710 constraint). Measured on this
    Postgres 2026-08-21. The rest still reach DEBUG rather than being
    dropped."""
    try:
        if (diag.sqlstate or "") == "00000":
            logger.warning(
                "[RUN_REGISTRY] schema notice: %s", diag.message_primary or "")
        else:
            logger.debug(
                "[RUN_REGISTRY] schema notice: %s", diag.message_primary or "")
    except Exception:
        pass


class RunRegistry:
    """Postgres-backed registry of test runs for history + report ownership."""

    def __init__(self, dsn: str = None):
        self.dsn = dsn or settings.DATABASE_URL
        setup = psycopg.connect(
            self.dsn, autocommit=True, connect_timeout=PG_CONNECT_TIMEOUT_S)
        setup.add_notice_handler(_log_schema_notice)
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
        self, group_id: str, org_id: Optional[str]
    ) -> Optional[str]:
        """group_id when a run in org_id may be FILED into that folder, else
        None — the same predicate assign_runs enforces (the folder is in the
        run's org), applied at the write instead of at the read.

        It has to be keyed on the NEW ROW'S ORG rather than on whoever read
        the source. A re-run inherits the folder of the run it was cloned
        from, and the token-less dev caller reads that source row through the
        UNFILTERED group join (see _group_join), so the id arriving here can
        name ANY org's folder. Filing the new run there would put it in a
        folder its own org cannot see — and, now that a folder is what
        publishes a run, would show it to an org that never had access.

        Only record_start can make this decision, because only it knows the
        org actually written on the new row — _lookup_org_id can supply it
        when the token did not. An org-less row (AUTH_ENFORCED off) matches
        no folder at all: run_groups.org_id is NOT NULL, so there is nothing
        for it to equal.

        Runs on its OWN pool connection and swallows its own errors, exactly
        like _lookup_org_id: this decides a folder tag, and nothing about a
        folder tag may cost the history row it decorates."""
        if org_id is None:
            return None
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "SELECT 1 FROM run_groups "
                    "WHERE group_id = %s AND org_id = %s",
                    (group_id, org_id),
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
                group_id = self._fileable_group_id(group_id, org_id)
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
    # Run groups (History page folders). Keyed to the ORG and nothing finer:
    # a folder belongs to the org, and so does every run filed into it. That
    # is the whole model — moving a run into a folder is the act of
    # publishing it to the team, and "Ungrouped" is work in progress that
    # stays with whoever is doing it (owner decisions D1-D4, 2026-08-23).
    # User-facing CRUD: these methods PROPAGATE storage errors (the
    # swallow-everything discipline above exists to protect the generation
    # pipeline, not this UI path) — DuplicateGroupName and every 409/404
    # depend on the exception escaping.
    #
    # Authority:
    #   create                    anyone in the org
    #   rename                    the creator, or an org_admin
    #   delete                    an org_admin ONLY — deleting a folder
    #                             un-publishes every run in it, so it is an
    #                             org-level act even for the person who made
    #                             the folder (owner decision, 2026-08-23)
    #   file a run                the folder must be in the caller's org AND
    #                             the run must be the caller's own, or the
    #                             caller is org_admin and the run is in their
    #                             org. SEEING a shared run is not authority
    #                             over it: a peer reads it and re-runs it,
    #                             but only its owner or an org_admin files it
    # A caller who may not act gets False, which the endpoints render as 404,
    # never 403 — a folder's existence cannot be probed by id.

    def list_groups(
        self,
        folder_org_id: Optional[str],
        run_org_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """The org's folders, name-sorted, each with its run count.

        Two org arguments, because they are two different things for exactly
        one caller. folder_org_id is which FOLDERS are listed — always the
        caller's own org, whoever they are, so that read and write bind the
        same value and no control is drawn that a mutation would 404.
        run_org_id is which RUNS the count counts; it defaults to
        folder_org_id and differs only for a platform admin, whose runs span
        every org while their folders do not. None means no narrowing on that
        dimension and belongs to the token-less dev path alone — filtering on
        a NULL org would evaluate to NULL for every row and return nothing.

        There is no per-user narrowing here any more, and there must not be:
        a folder's contents are the org's, so the count is the org's. Scoping
        it to the caller was what made a member's chip read 1 beside a folder
        holding 2 — the same defect as their table listing one run out of two.

        The run scope goes in the JOIN condition, not the WHERE clause: in
        the WHERE it would turn the LEFT JOIN into an inner one and drop
        every folder that holds no runs.

        A concrete run scope with NO folder scope is not a caller shape and
        fails closed. groups_endpoints already refuses to call with it, but a
        guard at one call site and a fail-closed default are not the same
        protection: without this, that pair emitted no WHERE at all and
        listed every org's folders."""
        if run_org_id is None:
            run_org_id = folder_org_id
        elif folder_org_id is None:
            return []
        join, join_params = "", []
        if run_org_id is not None:
            join = " AND t.org_id = %s"
            join_params = [run_org_id]
        where, where_params = "", []
        if folder_org_id is not None:
            where = "WHERE g.org_id = %s "
            where_params = [folder_org_id]
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT g.group_id, g.name, g.created_by, "
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
        every member of the org sees every folder, but only the creator — or
        an org_admin inside a concrete org — RENAMES one. Deleting is
        stricter still and does not come through here; see delete_group."""
        row = self._visible_group(conn, org_id, group_id)
        if row is None:
            return None
        if row["created_by"] == user_id:
            return row
        if is_org_admin and org_id is not None:
            return row
        return None

    def _visible_group(
        self,
        conn,
        org_id: Optional[str],
        group_id: str,
    ) -> Optional[Dict[str, Any]]:
        """The folder row when this caller can SEE it, else None: it is in
        their org. Seeing a folder is what lets a caller file their own runs
        into it — mutating the folder itself is stricter (_mutable_group).

        Deliberately takes NO row lock. The private/org model needed one:
        rename counted a folder's foreign-owned runs and then flipped it to
        private, and without FOR UPDATE a concurrent assign_runs could slip a
        run in between the count and the write. With visibility gone there is
        no read-then-decide left to protect — every remaining race is closed
        by the database itself, except delete_group's own audit_run_ids
        read, which is a snapshot the database does not protect (see its
        docstring). A concurrent delete makes assign_runs' UPDATE
        raise ForeignKeyViolation (caught, answered 404) and makes
        rename_group's UPDATE match 0 rows (also 404).

        Dropping the lock also removes a real deadlock class rather than
        merely documenting it. The lock order was NOT uniform: rename, delete
        and assign all took run_groups first and test_runs second, while
        record_start goes the other way — it writes its test_runs row and the
        foreign key then takes a KEY SHARE lock on the run_groups parent.
        record_start swallows every exception, so it was the side that would
        lose quietly, and a deadlocked re-run would have produced a run with
        no history row at all."""
        row = conn.execute(
            "SELECT group_id, org_id, created_by, name "
            "FROM run_groups WHERE group_id = %s",
            (group_id,),
        ).fetchone()
        if row is None:
            return None
        if org_id is not None and row["org_id"] != org_id:
            return None
        return row

    def create_group(
        self,
        org_id: str,
        user_id: str,
        name: str,
    ) -> Dict[str, Any]:
        """Create a folder in the caller's org — anyone in the org may.
        Raises DuplicateGroupName on a case-insensitive name collision with
        ANY folder in that org, whoever created it: the org owns the name."""
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "INSERT INTO run_groups (group_id, org_id, created_by, name) "
                    "VALUES (%s, %s, %s, %s) "
                    "RETURNING group_id, name, created_by, created_at, updated_at",
                    (str(uuid.uuid4()), org_id, user_id, name),
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
        name: str,
        *,
        audit_old_name: list[str] | None = None,
    ) -> bool:
        """Rename a folder. False when it doesn't exist or the caller may not
        mutate it. Raises DuplicateGroupName when the new name collides —
        rename needs its OWN guard, create's does not cover it.

        audit_old_name, if given, is appended with the folder's name as it
        was BEFORE this call — _mutable_group's SELECT already reads it, so
        the audit trail can say what a folder used to be called, not just
        what it is now. Appended only once the UPDATE actually matches a
        row, so it stays empty on every False return — a caller that only
        records audit_detail after checking the return value can never be
        handed a name for a rename that did not happen."""
        try:
            with self._pool.connection() as conn:
                row = self._mutable_group(
                    conn, org_id, user_id, is_org_admin, group_id)
                if row is None:
                    return False
                cur = conn.execute(
                    "UPDATE run_groups SET name = %s, updated_at = now() "
                    "WHERE group_id = %s",
                    (name, group_id),
                )
                if cur.rowcount != 1:
                    return False
                if audit_old_name is not None:
                    audit_old_name.append(row["name"])
                return True
        except psycopg.errors.UniqueViolation:
            raise DuplicateGroupName(name)

    def delete_group(
        self,
        org_id: Optional[str],
        user_id: str,
        is_org_admin: bool,
        group_id: str,
        *,
        audit_run_ids: list[str] | None = None,
    ) -> bool:
        """Delete a folder and return its member runs to Ungrouped. Runs are
        NEVER deleted: fk_test_runs_group is ON DELETE SET NULL, so Postgres
        ungroups the members as part of the DELETE — no second statement,
        and no window in which a run points at a folder that is gone.

        ORG_ADMIN ONLY, which is stricter than rename: returning the runs to
        Ungrouped un-publishes every one of them, so the org loses sight of
        that work until someone re-files it. That is an org-level consequence
        whoever created the folder, and the folder is the org's anyway. A
        member who made a folder can still rename it and still move their own
        runs out of it; they ask an admin to remove it.

        No dead end for a solo user: ensure_personal_org seats them as
        org_admin of their own personal org, so they still delete their own
        folders. Only a plain member inside a TEAM org is narrowed. The SPA
        has to draw its Delete control on the SAME rule, which is the auth
        payload's can_manage_org_folders — NOT is_org_admin, which means
        is_team_admin() and so is False for every solo user. Gating the button
        on is_org_admin made this paragraph false in the product: the API
        answered 204 for a folder whose Delete control was never rendered.

        False when the folder doesn't exist, is outside the caller's org, or
        the caller is not an org_admin — the endpoint renders all three as
        404 alike, so nothing about the folder leaks either way.

        audit_run_ids, if given, is appended with the run ids the
        membership SELECT below sees, read BEFORE the DELETE:
        fk_test_runs_group's ON DELETE SET NULL erases each member's
        group_id as part of that same statement. That SELECT is a
        snapshot under READ COMMITTED, not a lock: a run assigned to this
        folder concurrently — after the snapshot but before the DELETE
        proceeds — is ungrouped by that same DELETE without ever
        appearing in this list (assign_runs' own ForeignKeyViolation
        handling is the other side of the same race). The list is
        therefore what this transaction's snapshot could see, not a
        guarantee of the folder's full membership at delete time.
        Appended only once the DELETE actually removes a row, so it stays
        empty on every False return, the same guarantee rename_group's
        audit_old_name makes."""
        if not (is_org_admin and org_id is not None):
            return False
        with self._pool.connection() as conn:
            if self._visible_group(conn, org_id, group_id) is None:
                return False
            run_ids = None
            if audit_run_ids is not None:
                rows = conn.execute(
                    "SELECT run_id FROM test_runs WHERE group_id = %s",
                    (group_id,),
                ).fetchall()
                run_ids = [r["run_id"] for r in rows]
            cur = conn.execute(
                "DELETE FROM run_groups WHERE group_id = %s", (group_id,)
            )
            if cur.rowcount != 1:
                return False
            if run_ids is not None:
                audit_run_ids.extend(run_ids)
            return True

    def count_ungrouped(
        self,
        user_id: Optional[str] = None,
        org_id: Optional[str] = None,
        folder_org_id: Optional[str] = None,
        include_unowned: bool = True,
    ) -> int:
        """The Ungrouped chip's live count on the History page.

        Takes the caller's whole (user_id, org_id) scope and the same
        visibility predicate list_runs uses, so the chip always equals the
        total of list_runs(group="ungrouped") for that caller.

        Ungrouped is g.group_id IS NULL — in no folder the caller can see —
        and so is the published half of _VISIBLE_RUN_SQL, both off the same
        join. That is what makes the two provably one set rather than two
        that happen to agree. A row pointing at a folder outside the org is
        in no folder this caller can see, so it is not published to them
        either: it reads as Ungrouped for its OWNER and is simply absent for
        a PEER, instead of being visible org-wide while displaying as
        Ungrouped. It still reads as Ungrouped for an org_admin and for a
        platform admin — rules 5 and 2 reach it without the folder, and this
        change does not narrow those.

        folder_org_id is the caller's OWN org and scopes the folder join
        alone, which org_id cannot do here: a platform admin's org_id is None
        because their runs span every org, and reusing that for the join
        handed them every org's folders. It defaults to org_id."""
        join, params = self._group_join(
            folder_org_id or org_id, identified=user_id is not None)
        clauses = ["g.group_id IS NULL"]
        if user_id is not None:
            # The shared-run predicate, written exactly as list_runs writes
            # it. Inside this count its second half is always false —
            # g.group_id IS NULL and g.group_id IS NOT NULL cannot both hold
            # — so it collapses to "my own work", which is what Ungrouped
            # means. Sharing one constant with list_runs is what keeps the
            # chip and the table one set.
            clauses.append(_VISIBLE_RUN_SQL)
            params = params + [user_id]
        elif not include_unowned:
            # Same rule as list_runs, for the same reason: the chip must count
            # exactly what the Ungrouped filter lists.
            clauses.append(_OWNED_RUN_SQL)
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

        The folder must be in the caller's org, and every run must be one the
        caller may file: their own, or — for an org_admin — any run in their
        org. Seeing a shared run does NOT confer this: a peer reads and
        re-runs another member's published test, but only its owner or an
        org_admin moves it (owner decision D4).

        Every run must ALSO be in the caller's org, org_admin or not. Without
        that clause a member could file a run they own in an org they have
        since left into a folder of the org they are in now, leaving a row
        whose org_id and folder disagree. That was harmless while grouping
        only decorated a row; now that grouping is what PUBLISHES a run, it
        would have shown the old org's members a run they never had access
        to. The clause also excludes a run with no org at all (AUTH_ENFORCED
        off), which no folder can legitimately hold — run_groups.org_id is
        NOT NULL, so there is nothing for it to equal.

        All-or-nothing: filing is a per-row authority decision, which is
        exactly when partial writes appear, so a batch containing one run the
        caller may not file writes NOTHING and returns False — the caller's
        own runs stay where they were."""
        if org_id is None:
            # No org: nothing to file into, and no org to test a run against.
            return False
        with self._pool.connection() as conn:
            if group_id is not None:
                if self._visible_group(conn, org_id, group_id) is None:
                    return False
            params: list = [group_id, list(run_ids), org_id]
            if is_org_admin:
                allowed = "org_id = %s"
                params.append(org_id)
            else:
                allowed = "user_id = %s"
                params.append(user_id)
            try:
                cur = conn.execute(
                    f"UPDATE test_runs SET group_id = %s "
                    f"WHERE run_id = ANY(%s) AND org_id = %s AND {allowed}",
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
        org_id: Optional[str], *, identified: bool = False
    ) -> Tuple[str, list]:
        """(SQL, params) for the ONE folder join every read derives its
        folder answers from: a row's folder tag is g.name, its folder id is
        g.group_id, and "in no folder I can see" is g.group_id IS NULL.
        _VISIBLE_RUN_SQL reads "published to me" off this same alias, so this
        is also the only place the published half of visibility is expressed.

        Three forms. With an org it is that org's folders, which is the
        ordinary caller. Without one the answer depends on whether there is a
        CALLER at all, and that is what `identified` says:

          * identified=False, org None — the token-less dev path with
            AUTH_ENFORCED off, which ownership.py rule 1 exempts from
            everything. No filter: binding a NULL org would make
            g.org_id = NULL evaluate to NULL for every row, hide every folder
            name and collapse the whole page into Ungrouped.
          * identified=True, org None — a caller who has an identity but no
            org (rule 4). They own no org, so no folder is theirs to resolve
            and nothing is published to them; ON FALSE says exactly that,
            leaving the visibility predicate as the bare owner check rule 4
            requires and keeping foreign folder names out of their rows. The
            unfiltered form would have handed them every org's folders.

        No per-user term any more. Every folder in an org is visible to every
        member of it, so the caller's identity does not enter this join —
        `identified` is about whether there is a caller, not about who.

        The returned params bind BEFORE any WHERE-clause params, because
        these placeholders sit earlier in the SQL text and psycopg binds %s
        strictly by position.

        ONE other query expresses publication in SQL and cannot call this:
        get_run_owner, which has no caller to bind and anchors to the run's
        own org instead. Change what "published" means here and change it
        there too."""
        if org_id is None:
            if identified:
                return "LEFT JOIN run_groups g ON FALSE", []
            return "LEFT JOIN run_groups g ON g.group_id = t.group_id", []
        return (
            "LEFT JOIN run_groups g ON g.group_id = t.group_id"
            " AND g.org_id = %s",
            [org_id],
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
        folder_org_id: Optional[str] = None,
        include_unowned: bool = True,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Most-recent-first run rows + total count.

        user_id=None lists every user's runs (admin scope). Otherwise it is
        the caller's identity, and the rows they get are the ones
        _VISIBLE_RUN_SQL admits: their own work, plus every run the org has
        published into a folder. It is NOT a plain t.user_id filter any more
        — that was what made a shared folder cosmetic, showing a member their
        own slice of a folder the whole team was supposed to be reading.

        status narrows both the rows and the total to one run status, so the
        History tabs paginate and count within their own filter. q is a
        case-insensitive substring match over the description, owner email
        and run id — applied server-side so it spans the whole visible set,
        not just the loaded page. group narrows to one folder (a run_groups
        id) or, with the literal "ungrouped", to rows in no folder the caller
        can see; rows carry group_id and group_name from _group_join so the
        History table renders folder tags without extra requests.

        folder_org_id scopes that join to the caller's OWN org, which org_id
        cannot do: org_id is the ROW filter, and a platform admin's is None
        because their runs span every org — reusing it for the join tagged
        their rows with other orgs' folder names. Defaults to org_id, so a
        caller that passes nothing is unchanged.

        include_unowned=False drops rows no user owns. Only a platform admin
        (and the token-less dev caller) may read those, so only they may list
        them — auth/ownership fails them closed for everyone else, and a list
        that offers a row the drawer then refuses is a list that lies."""
        join, join_params = self._group_join(
            folder_org_id or org_id, identified=user_id is not None)
        clauses: list = []
        params: list = []
        if user_id is not None:
            clauses.append(_VISIBLE_RUN_SQL)
            params.append(user_id)
        elif not include_unowned:
            clauses.append(_OWNED_RUN_SQL)
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
                    f"SELECT t.run_id, t.user_id, t.user_email, t.org_id, "
                    f"       t.user_query, "
                    f"       t.rerun_of, t.status, t.created_at, t.updated_at, "
                    f"       g.group_id, g.name AS group_name "
                    f"FROM test_runs t "
                    f"{join}"
                    f"{where} "
                    f"ORDER BY t.created_at DESC, t.run_id LIMIT %s OFFSET %s",
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
        return self.get_run_owner(run_id).user_id

    def get_run_owner(self, run_id: str) -> RunOwnership:
        """Ownership + publication state for the authorization gates, or an
        all-None RunOwnership on unknown run / storage error — both treated
        as 'not yours'. Read it by ATTRIBUTE, never by unpacking.

        group_id joined the tuple when a folder became what publishes a run:
        the /reports gate has to know whether the org published this one.
        It is the folder resolved against the RUN's OWN org, not the raw
        column — a run pointing at some other org's folder is published to
        nobody, and reporting the raw id let ownership.py rule 3 fire on it
        for every member of the run's org. This method takes no caller, so
        the caller half of rule 3 stays where it is (org_id must equal the
        caller's); anchoring to t.org_id here makes the pair mean exactly
        what list_runs' _group_join means for the same caller.

        This is the ONE publication join that is not _group_join, and nothing
        couples them but this sentence: _group_join binds a CALLER's org and
        this binds the row's, so it cannot literally reuse it. Change either
        notion of "published" and change both."""
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "SELECT t.user_id, t.org_id, g.group_id FROM test_runs t "
                    "LEFT JOIN run_groups g ON g.group_id = t.group_id "
                    "                      AND g.org_id = t.org_id "
                    "WHERE t.run_id = %s",
                    (run_id,),
                ).fetchone()
            return (RunOwnership(row["user_id"], row["org_id"], row["group_id"])
                    if row else RunOwnership(None, None, None))
        except Exception as e:
            logger.error(f"[RUN_REGISTRY] get_run_owner failed for {run_id}: {e}")
            return RunOwnership(None, None, None)

    def get_run_owners_for_caller(
        self,
        run_ids: list[str],
        org_id: str | None = None,
        *,
        identified: bool = False,
    ) -> dict[str, RunOwnership]:
        """{run_id: RunOwnership} for the runs that exist, in ONE query.

        The three facts an access decision needs, for many runs at once, read
        through the SAME _group_join every other caller-scoped read uses — so
        the group_id reported here means "in a folder of THIS caller's org",
        exactly as it does in get_run and list_runs. That is the whole point:
        the History page has to answer "could this caller open run X" for the
        originals its re-run rows point at, and re-expressing the published
        rule inline is precisely how the two halves of it drifted apart
        before. org_id and identified mean what they mean in get_run.

        Unknown ids are simply absent from the mapping, which every caller
        must read as "not reachable" — the same answer /api/history/{id} gives
        for a run that does not exist.

        A storage error returns {}, which is fail-closed and matches the rest
        of this registry's reads. It is NOT symmetric with the detail
        endpoint, and the difference is worth stating plainly: that endpoint
        is a separate query on a separate pooled connection, so a transient
        failure HERE can mark every re-run row on a page "no longer
        available" while clicking one through would in fact have answered
        200. Erring that way is the deliberate side — a badge that overstates
        the loss is recoverable by reloading, whereas a live-looking link that
        404s is the exact defect this field exists to remove.

        An empty run_ids costs NO query at all: most History pages carry no
        re-run row, and they must not pay for the ones that do (owner ruling
        R4). De-duplication belongs to the caller, which passes distinct ids;
        ANY() would tolerate repeats, but the caller knows the page.

        Deliberately NOT get_run_owner's batch form: that one anchors the
        folder to the RUN's org because it has no caller, and this one anchors
        it to the CALLER's. Two different questions that happen to select the
        same three columns."""
        if not run_ids:
            return {}
        join, params = self._group_join(org_id, identified=identified)
        try:
            with self._pool.connection() as conn:
                rows = conn.execute(
                    "SELECT t.run_id, t.user_id, t.org_id, g.group_id "
                    "FROM test_runs t "
                    f"{join} "
                    "WHERE t.run_id = ANY(%s)",
                    params + [list(run_ids)],
                ).fetchall()
            return {
                r["run_id"]: RunOwnership(r["user_id"], r["org_id"], r["group_id"])
                for r in rows
            }
        except Exception as e:
            logger.error(
                "[RUN_REGISTRY] get_run_owners_for_caller failed: %s", e)
            return {}

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
        *,
        identified: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Full row for one run (including robot_code), or None if unknown
        or on storage error (callers treat both as 'not found').

        org_id is the CALLER's org, not the row's, and feeds _group_join: a
        run sitting in a folder outside that org reports NULL for both
        group_id and group_name, so the drawer never shows a folder name the
        caller cannot resolve. It defaults to the unscoped form, which is
        what the feedback path in api/endpoints.py wants — that reads
        ownership and lineage, never the group fields.

        identified goes with it, and the two together mean the same thing
        they mean in list_runs: an org_id of None is the token-less dev
        caller when identified is False, and a caller who has an identity but
        no org when it is True — for whom no folder resolves at all. Without
        it the drawer answered with a foreign org's folder name for a row the
        LIST reports as Ungrouped to the same caller.

        The rerun path DOES pass a scope, and reads the authorization
        decision itself off the group_id this join returns: a re-run inherits
        its source's folder, and "is this run published to my org" is exactly
        "did the folder resolve"."""
        join, params = self._group_join(org_id, identified=identified)
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

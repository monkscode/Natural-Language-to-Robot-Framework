"""Integration tests for the org-tenancy schema bootstrap.

Skipped when Postgres is unreachable. Uses the auth_test isolated schema.
"""

import uuid

import psycopg
import pytest

from src.backend.auth import db as auth_db
from src.backend.auth.org_db import init_org_db
from src.backend.auth.org_repository import OrgRepository
from src.backend.auth.repository import UserRepository
from tests.test_auth.conftest import ensure_stub_learning_tables

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


def _table_exists(table: str) -> bool:
    with auth_db.get_pool().connection() as conn:
        row = conn.execute("SELECT to_regclass(%s) AS t", (table,)).fetchone()
        return row["t"] is not None


def test_init_org_db_creates_tables():
    init_org_db()
    assert _table_exists("organizations")
    assert _table_exists("org_members")


def test_init_org_db_is_idempotent():
    init_org_db()
    init_org_db()  # second call must not raise
    assert _table_exists("org_members")


def test_init_org_db_adds_owner_column_and_unique_index():
    """Change 1: owner_user_id + uq_org_owner_personal, both idempotent
    (CREATE TABLE IF NOT EXISTS ... / ADD COLUMN IF NOT EXISTS / CREATE
    UNIQUE INDEX IF NOT EXISTS all apply on a fresh DB and on a rerun)."""
    init_org_db()
    with auth_db.get_pool().connection() as conn:
        col = conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'organizations' AND column_name = 'owner_user_id'"
        ).fetchone()
    assert col is not None
    assert _table_exists("uq_org_owner_personal")


def test_owner_backfill_handles_sole_member_email_fallback_and_duplicate_names():
    """Test 9 — the backfill. Three required cases plus one hardening case,
    all in ONE migration run (proves the ambiguous/colliding cases do not
    abort the others in the same statement):
      (a) sole remaining member of a personal org owns it;
      (b) a personal org's name IS a user's email and gets claimed by the
          fallback when no sole member exists (org vacated);
      (c) two personal orgs sharing a name are ambiguous — the migration
          leaves BOTH owner_user_id NULL rather than aborting;
      (d) a user who is the sole member of one personal org already OWNS a
          different one (simulates a concurrent ensure_personal_org stamp —
          step 1 — landing on a sibling instance mid-rolling-deploy, before
          this migration's primary pass reaches the org in (d); see
          backfill_personal_org_owners' docstring for why the primary pass
          needs this guard even though the task-2 brief's SQL doesn't carry
          one). (a)/(b) still land in this same run."""
    users, orgs = UserRepository(), OrgRepository()
    email_a = f"bfa-{uuid.uuid4().hex[:8]}@x.com"
    email_b = f"bfb-{uuid.uuid4().hex[:8]}@x.com"
    email_c = f"bfc-{uuid.uuid4().hex[:8]}@x.com"
    email_d = f"bfd-{uuid.uuid4().hex[:8]}@x.com"
    org_a = org_b = org_c1 = org_c2 = org_d_sole = org_d_owned = None
    try:
        user_a = users.create_user(email_a, "password123", "A")
        user_b = users.create_user(email_b, "password123", "B")
        users.create_user(email_c, "password123", "C")
        user_d = users.create_user(email_d, "password123", "D")

        with auth_db.get_pool().connection() as conn:
            # (a) sole remaining member: name is NOT an email, one member.
            org_a = conn.execute(
                "INSERT INTO organizations (name, kind) VALUES (%s, 'personal') "
                "RETURNING id",
                (f"Sole Member Org {uuid.uuid4().hex[:8]}",),
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO org_members (org_id, user_id, org_role) "
                "VALUES (%s, %s, 'org_admin')",
                (org_a, str(user_a["id"])),
            )
            # (b) name-fallback: name IS the user's email, vacated (0
            # members) so the sole-member rule can't claim it — isolates
            # the fallback from the primary pass.
            org_b = conn.execute(
                "INSERT INTO organizations (name, kind) VALUES (%s, 'personal') "
                "RETURNING id",
                (email_b,),
            ).fetchone()["id"]
            # (c) ambiguous duplicate name: two vacated personal orgs share
            # a name that matches user_c's email — neither can be claimed
            # unambiguously.
            org_c1 = conn.execute(
                "INSERT INTO organizations (name, kind) VALUES (%s, 'personal') "
                "RETURNING id",
                (email_c,),
            ).fetchone()["id"]
            org_c2 = conn.execute(
                "INSERT INTO organizations (name, kind) VALUES (%s, 'personal') "
                "RETURNING id",
                (email_c,),
            ).fetchone()["id"]
            # (d) primary-pass collision: user_d already OWNS org_d_owned
            # (set directly, standing in for a concurrent stamp) AND is the
            # sole member of a SEPARATE org_d_sole whose owner is still NULL.
            org_d_owned = conn.execute(
                "INSERT INTO organizations (name, kind, owner_user_id) "
                "VALUES (%s, 'personal', %s) RETURNING id",
                (f"Already Owned {uuid.uuid4().hex[:8]}", str(user_d["id"])),
            ).fetchone()["id"]
            org_d_sole = conn.execute(
                "INSERT INTO organizations (name, kind) VALUES (%s, 'personal') "
                "RETURNING id",
                (f"Sole But Already Owns {uuid.uuid4().hex[:8]}",),
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO org_members (org_id, user_id, org_role) "
                "VALUES (%s, %s, 'org_admin')",
                (org_d_sole, str(user_d["id"])),
            )
            conn.commit()

            # run_migration_once is a one-shot; the package-scoped fixture
            # already ran this migration (as a no-op) at schema setup, so
            # clear its marker to let THIS call actually execute it.
            conn.execute(
                "DELETE FROM data_migrations WHERE name = 'personal_org_owner_backfill'"
            )
            conn.commit()

        init_org_db()  # re-entrant: only the cleared marker's migration runs; must not raise

        with auth_db.get_pool().connection() as conn:
            owners = {
                oid: conn.execute(
                    "SELECT owner_user_id FROM organizations WHERE id = %s", (oid,)
                ).fetchone()["owner_user_id"]
                for oid in (org_a, org_b, org_c1, org_c2, org_d_sole, org_d_owned)
            }
        assert str(owners[org_a]) == str(user_a["id"]), "sole member must be claimed"
        assert str(owners[org_b]) == str(user_b["id"]), "name==email fallback must claim it"
        assert owners[org_c1] is None, "ambiguous duplicate name must stay NULL"
        assert owners[org_c2] is None, "ambiguous duplicate name must stay NULL"
        assert owners[org_d_sole] is None, "already-owned collision must stay NULL, not raise"
        assert str(owners[org_d_owned]) == str(user_d["id"]), "the pre-existing claim is untouched"
    finally:
        with auth_db.get_pool().connection() as conn:
            for oid in (org_a, org_b, org_c1, org_c2, org_d_sole, org_d_owned):
                if oid:
                    conn.execute("DELETE FROM organizations WHERE id = %s", (oid,))
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([email_a, email_b, email_c, email_d],))
            conn.commit()


def test_owner_backfill_survives_a_user_in_two_unclaimed_personal_orgs():
    """The primary pass must not abort when ONE user is the sole member of TWO
    still-unclaimed personal orgs.

    The `NOT EXISTS (... owner_user_id = m.user_id)` guard is evaluated against
    the statement's snapshot, so rows updated by the SAME statement are
    invisible to it: both orgs pass the guard, both are set to the same owner,
    and uq_org_owner_personal raises UniqueViolation. That aborts the whole
    migration — every other row in the statement rolls back with it, and
    because main.py wraps init_org_db() in one try/except with
    init_invitations_db(), the failure surfaces as "auth unavailable until
    Postgres is reachable", which names the wrong cause.

    Ambiguity is resolved the same way the fallback resolves a duplicate name:
    claim NEITHER. Picking one arbitrarily would decide which org's learning
    history the user reclaims, which is not a coin-flip decision.

    The well-formed user in the same run proves the abort is gone rather than
    merely relocated — before the fix their org is rolled back too.
    """
    users = UserRepository()
    email_x = f"dbl-{uuid.uuid4().hex[:8]}@x.com"
    email_y = f"one-{uuid.uuid4().hex[:8]}@x.com"
    org_x1 = org_x2 = org_y = None
    try:
        user_x = users.create_user(email_x, "password123", "X")
        user_y = users.create_user(email_y, "password123", "Y")

        with auth_db.get_pool().connection() as conn:
            def _personal(name: str) -> str:
                return conn.execute(
                    "INSERT INTO organizations (name, kind) VALUES (%s, 'personal') "
                    "RETURNING id", (name,),
                ).fetchone()["id"]

            # user_x: sole member of two unclaimed personal orgs (a legacy
            # pre-single-active-org shape; collapse_all_to_single_org exists
            # because double membership happened).
            org_x1 = _personal(f"Double One {uuid.uuid4().hex[:8]}")
            org_x2 = _personal(f"Double Two {uuid.uuid4().hex[:8]}")
            # user_y: the well-formed case, in the SAME migration run.
            org_y = _personal(f"Single {uuid.uuid4().hex[:8]}")
            for oid, uid in ((org_x1, user_x["id"]), (org_x2, user_x["id"]),
                             (org_y, user_y["id"])):
                conn.execute(
                    "INSERT INTO org_members (org_id, user_id, org_role) "
                    "VALUES (%s, %s, 'org_admin')", (oid, str(uid)),
                )
            conn.execute(
                "DELETE FROM data_migrations WHERE name = 'personal_org_owner_backfill'"
            )
            conn.commit()

        init_org_db()  # must not raise

        with auth_db.get_pool().connection() as conn:
            owners = {
                oid: conn.execute(
                    "SELECT owner_user_id FROM organizations WHERE id = %s", (oid,)
                ).fetchone()["owner_user_id"]
                for oid in (org_x1, org_x2, org_y)
            }
        assert owners[org_x1] is None, "ambiguous double membership must stay NULL"
        assert owners[org_x2] is None, "ambiguous double membership must stay NULL"
        assert str(owners[org_y]) == str(user_y["id"]), (
            "the well-formed org in the same run must still be claimed"
        )
    finally:
        with auth_db.get_pool().connection() as conn:
            for oid in (org_x1, org_x2, org_y):
                if oid:
                    conn.execute("DELETE FROM organizations WHERE id = %s", (oid,))
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([email_x, email_y],))
            conn.commit()


# ---------------------------------------------------------------------------
# Task T3 — org deletion is structurally impossible while learning rows name it
#
# T1 removed the only DELETE FROM organizations in the codebase. This trigger
# stops a future one from returning. It is a guarded BEFORE DELETE trigger
# rather than a real foreign key: organizations.id is uuid while every
# learning org_id is text, and making an FK satisfiable would mean rewriting
# 152 synthetic org ids across 31 files — in the very tests that guard tenancy
# isolation, where a mechanical rewrite is how a guarantee quietly weakens.
# ---------------------------------------------------------------------------

def _make_personal_org(conn, name: str) -> str:
    return conn.execute(
        "INSERT INTO organizations (name, kind) VALUES (%s, 'personal') RETURNING id",
        (name,),
    ).fetchone()["id"]


def test_deleting_an_org_that_owns_learning_rows_is_refused():
    """Test 14a. The whole point of the guard: an org naming live learning
    rows cannot be deleted, so those rows can never be stranded again."""
    org_id = None
    try:
        init_org_db()
        with auth_db.get_pool().connection() as conn:
            ensure_stub_learning_tables(conn)
            org_id = _make_personal_org(conn, f"Referenced {uuid.uuid4().hex[:8]}")
            conn.execute(
                "INSERT INTO nl_feedback_corrections (org_id) VALUES (%s)", (str(org_id),)
            )
            conn.commit()

        with pytest.raises(psycopg.errors.ForeignKeyViolation) as exc:
            with auth_db.get_pool().connection() as conn:
                conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
                conn.commit()
        # The message must name the table, or an operator cannot act on it.
        assert "nl_feedback_corrections" in str(exc.value)
    finally:
        with auth_db.get_pool().connection() as conn:
            if org_id:
                conn.execute(
                    "DELETE FROM nl_feedback_corrections WHERE org_id = %s", (str(org_id),)
                )
                conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            conn.commit()


def test_deleting_an_unreferenced_org_still_works():
    """Test 14b — the known-negative. Without this, a trigger that refused
    EVERY delete would pass 14a and look correct."""
    with auth_db.get_pool().connection() as conn:
        init_org_db()
        ensure_stub_learning_tables(conn)
        org_id = _make_personal_org(conn, f"Unreferenced {uuid.uuid4().hex[:8]}")
        conn.commit()
        conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
        conn.commit()
        gone = conn.execute(
            "SELECT 1 FROM organizations WHERE id = %s", (org_id,)
        ).fetchone()
    assert gone is None


def test_trigger_degrades_when_a_learning_table_is_absent():
    """Test 14c. Learning tables live in per-test schemas while organizations
    lives wherever the session points; the to_regclass guard is what stops the
    trigger raising "relation does not exist" for a table this search_path
    cannot see. anti_patterns is in the trigger's list and is created by
    neither this suite's conftest nor its stub helper, so it is the absent
    case — asserted, not assumed, because the assertion is only meaningful
    while it stays absent."""
    org_id = None
    try:
        init_org_db()
        with auth_db.get_pool().connection() as conn:
            ensure_stub_learning_tables(conn)
            absent = conn.execute(
                "SELECT to_regclass('anti_patterns') AS t"
            ).fetchone()["t"]
            assert absent is None, (
                "anti_patterns became visible to this schema — pick another "
                "table from the trigger's list, or this test proves nothing"
            )
            org_id = _make_personal_org(conn, f"Degrade {uuid.uuid4().hex[:8]}")
            conn.commit()
            conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            conn.commit()
            gone = conn.execute(
                "SELECT 1 FROM organizations WHERE id = %s", (org_id,)
            ).fetchone()
        assert gone is None
        org_id = None
    finally:
        if org_id:
            with auth_db.get_pool().connection() as conn:
                conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
                conn.commit()


def test_audit_log_neither_blocks_a_delete_nor_is_repointed():
    """Test 15. audit_log is deliberately NOT in the trigger's list: audit
    rows must outlive the org they describe — that is the whole point of the
    actor/org snapshot. So an audit row naming an org must not block that
    org's deletion, and must still name it afterwards."""
    from src.backend.core.audit_log import init_audit_log

    org_id = None
    audit_id = None
    try:
        init_org_db()
        init_audit_log()
        with auth_db.get_pool().connection() as conn:
            org_id = _make_personal_org(conn, f"Audited {uuid.uuid4().hex[:8]}")
            audit_id = conn.execute(
                "INSERT INTO audit_log (actor_email, org_id, method, path, status_code) "
                "VALUES ('a@e.com', %s, 'POST', '/x', 200) RETURNING id",
                (str(org_id),),
            ).fetchone()["id"]
            conn.commit()
            conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            conn.commit()
            row = conn.execute(
                "SELECT org_id FROM audit_log WHERE id = %s", (audit_id,)
            ).fetchone()
        assert row is not None, "the audit row must outlive the org"
        assert row["org_id"] == str(org_id), "and must still name it"
        org_id = None
    finally:
        with auth_db.get_pool().connection() as conn:
            if audit_id:
                conn.execute("DELETE FROM audit_log WHERE id = %s", (audit_id,))
            if org_id:
                conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            conn.commit()

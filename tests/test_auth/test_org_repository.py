"""Integration tests for OrgRepository against a real PostgreSQL.

Skipped when Postgres is unreachable. Rows live in the throwaway auth_test
schema (dropped at session end).
"""

import uuid

import pytest

from src.backend.auth.db import get_pool
from src.backend.auth.repository import UserRepository
from src.backend.auth.org_repository import OrgRepository

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


def _unique_email() -> str:
    return f"org-{uuid.uuid4().hex[:12]}@example.com"


@pytest.fixture(scope="module")
def repos():
    yield UserRepository(), OrgRepository()


def test_ensure_creates_personal_org_with_admin_role(repos):
    users, orgs = repos
    email = _unique_email()
    user = users.create_user(email, "S3cretpw!")
    org_id = orgs.ensure_personal_org(str(user["id"]), email)
    assert org_id
    memberships = orgs.get_orgs_for_user(str(user["id"]))
    assert len(memberships) == 1
    assert memberships[0]["org_id"] == org_id
    assert memberships[0]["org_role"] == "org_admin"
    assert memberships[0]["kind"] == "personal"
    assert memberships[0]["name"] == email


def test_ensure_is_idempotent(repos):
    users, orgs = repos
    email = _unique_email()
    user = users.create_user(email, "S3cretpw!")
    first = orgs.ensure_personal_org(str(user["id"]), email)
    second = orgs.ensure_personal_org(str(user["id"]), email)
    assert first == second
    assert len(orgs.get_orgs_for_user(str(user["id"]))) == 1


def test_get_orgs_empty_for_unprovisioned_user(repos):
    users, orgs = repos
    user = users.create_user(_unique_email(), "S3cretpw!")
    assert orgs.get_orgs_for_user(str(user["id"])) == []


def test_ensure_creates_personal_org_with_owner_stamped(repos):
    """Change 2 step 3 (fresh create): a brand new personal org carries
    owner_user_id from the INSERT itself — no legacy NULL-owner gap for any
    org minted from here on."""
    users, orgs = repos
    email = _unique_email()
    user = users.create_user(email, "S3cretpw!")
    org_id = orgs.ensure_personal_org(str(user["id"]), email)
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT owner_user_id FROM organizations WHERE id = %s", (org_id,)
        ).fetchone()
    assert str(row["owner_user_id"]) == str(user["id"])


def test_ensure_personal_org_stamps_legacy_null_owner(repos):
    """Change 2 step 1's opportunistic stamp: a personal org reachable
    through org_members whose owner_user_id is still NULL (legacy data
    predating this column, or a backfill hole) gets claimed on the very
    next call — the last moment the link is knowable, before a future team
    join deletes the membership and the link with it."""
    users, orgs = repos
    email = _unique_email()
    user = users.create_user(email, "S3cretpw!")
    with get_pool().connection() as conn:
        # Bypass ensure_personal_org to simulate a pre-Change-2 row: no
        # owner_user_id set at creation.
        org = conn.execute(
            "INSERT INTO organizations (name, kind) VALUES (%s, 'personal') "
            "RETURNING id",
            (f"Legacy {uuid.uuid4().hex[:8]}",),
        ).fetchone()
        conn.execute(
            "INSERT INTO org_members (org_id, user_id, org_role) "
            "VALUES (%s, %s, 'org_admin')",
            (org["id"], str(user["id"])),
        )
        conn.commit()

    result = orgs.ensure_personal_org(str(user["id"]), email)

    assert result == str(org["id"])
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT owner_user_id FROM organizations WHERE id = %s", (org["id"],)
        ).fetchone()
    assert str(row["owner_user_id"]) == str(user["id"])


def test_ensure_personal_org_stamp_tolerates_owner_collision(repos):
    """Corner case (task-2 brief): the opportunistic stamp must never raise
    even when it collides with uq_org_owner_personal. Legacy data can leave
    a user's LIVE membership in org A (owner_user_id NULL) while they
    already OWN a different personal org B — e.g. the name-based backfill
    claimed B while A, whose name was not an email, was never touched.
    ensure_personal_org sits on the login path and must return org A's id
    either way, never strand the user behind an exception."""
    users, orgs = repos
    email = _unique_email()
    user = users.create_user(email, "S3cretpw!")
    with get_pool().connection() as conn:
        # org B: already owned by this user (ownership alone is what
        # matters — no membership row needed to reproduce the collision).
        org_b = conn.execute(
            "INSERT INTO organizations (name, kind, owner_user_id) "
            "VALUES (%s, 'personal', %s) RETURNING id",
            (f"Legacy B {uuid.uuid4().hex[:8]}", str(user["id"])),
        ).fetchone()
        # org A: the user's LIVE membership, owner_user_id still NULL.
        org_a = conn.execute(
            "INSERT INTO organizations (name, kind) VALUES (%s, 'personal') "
            "RETURNING id",
            (f"Legacy A {uuid.uuid4().hex[:8]}",),
        ).fetchone()
        conn.execute(
            "INSERT INTO org_members (org_id, user_id, org_role) "
            "VALUES (%s, %s, 'org_admin')",
            (org_a["id"], str(user["id"])),
        )
        conn.commit()

    result = orgs.ensure_personal_org(str(user["id"]), email)  # must not raise

    assert result == str(org_a["id"]), "returns the org the user is a MEMBER of"
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT owner_user_id FROM organizations WHERE id = %s", (org_a["id"],)
        ).fetchone()
    assert row["owner_user_id"] is None, "guarded stamp must not have applied"
    with get_pool().connection() as conn:
        b_row = conn.execute(
            "SELECT owner_user_id FROM organizations WHERE id = %s", (org_b["id"],)
        ).fetchone()
    assert str(b_row["owner_user_id"]) == str(user["id"])  # untouched


# ---------------------------------------------------------------------------
# Step 3 (create) and uq_org_owner_personal.
#
# The partial unique index is on (owner_user_id) WHERE kind='personal', so any
# transaction that puts a second personal org under the same owner collides
# with step 3's INSERT. ensure_personal_org is on the login self-heal, on
# remove_member and on provision_on_approval, so a raise there is a 500 on
# login — the same reason _stamp_owner_if_unclaimed is guarded.
#
# Both interleavings below are REAL, not mocked: a second connection writes the
# colliding row and holds its transaction open, so step 2's SELECT cannot see
# it (READ COMMITTED) while the index can. Two connections, not two instances;
# the index does not know the difference.
# ---------------------------------------------------------------------------

def _claim_mid_call(call, claimer_stmt, params):
    """Run `claimer_stmt` on a second pooled connection, hold it open, invoke
    `call`, and commit the claimer 0.6s in — i.e. while the call is blocked.
    Returns (result, raised).

    `call` is a zero-arg callable rather than a hard-wired ensure_personal_org
    so the same interleaving can be aimed at the owner backfill, which enters
    the same partial unique index from the other side.
    """
    import threading

    pool = get_pool()
    claimer = pool.getconn()
    committed = threading.Event()
    try:
        claimer.execute(claimer_stmt, params)

        def _commit():
            claimer.commit()
            committed.set()

        timer = threading.Timer(0.6, _commit)
        timer.start()
        result = raised = None
        try:
            result = call()
        except Exception as exc:          # noqa: BLE001 — the thing under test
            raised = exc
        finally:
            timer.join(10)
            if not committed.is_set():
                claimer.commit()
        return result, raised
    finally:
        pool.putconn(claimer)


def test_a_concurrent_owner_stamp_is_serialised_by_the_users_row_lock(repos):
    """The interleaving the review named — another instance's
    backfill_personal_org_owners claiming the vacated org between step 2 and
    step 3 — cannot actually happen, and this pins WHY, because the reason is
    invisible at the call site.

    organizations.owner_user_id carries an FK to users(id), so a writer that
    SETS it takes a FOR KEY SHARE lock on that users row. Step 1's
    `SELECT ... FOR UPDATE` on the same row conflicts with it, so the claimer
    and this call cannot interleave in either order: whoever gets the users row
    first, the other one waits and then sees a settled world. Measured here as
    a ~0.6s block followed by the RECLAIM path (step 2), not the create path.

    This is a mechanism, not a coverage claim, and it is exactly why step 3
    still has to absorb a collision — see the test below for the writer this
    lock does not catch."""
    users, orgs = repos
    email = _unique_email()
    uid = str(users.create_user(email, "S3cretpw!")["id"])

    with get_pool().connection() as conn:
        vacated = conn.execute(
            "INSERT INTO organizations (name, kind) VALUES (%s, 'personal') "
            "RETURNING id",
            (email,),
        ).fetchone()["id"]

    result, raised = _claim_mid_call(
        lambda: orgs.ensure_personal_org(uid, email),
        "UPDATE organizations SET owner_user_id = %s WHERE id = %s",
        (uid, vacated),
    )

    assert raised is None, f"login path raised: {raised!r}"
    assert result == str(vacated), (
        "the concurrently claimed org must be the one handed back, not a "
        "second personal org for the same user"
    )


def test_step_3_absorbs_a_collision_the_users_row_lock_cannot_stop(repos):
    """The writer the FOR UPDATE does NOT serialise: one that leaves
    owner_user_id alone and changes only `kind`. Postgres skips the FK
    re-check when the referencing value is unchanged, so `UPDATE organizations
    SET kind='personal'` on a row already owned by this user takes no users
    lock at all. Step 1 sails past it, step 2 cannot see the uncommitted row,
    and step 3's INSERT lands straight on uq_org_owner_personal.

    Before the fix this raised UniqueViolation out of ensure_personal_org —
    reproduced, 0.62s in, on the branch as it stood. A team org flipping to
    personal is a contrived product story; the point is that the method's
    "never raise" property must hold on the statement itself and not on a
    coupling between an FK and a row lock that a future schema edit could drop
    without anyone noticing."""
    users, orgs = repos
    email = _unique_email()
    uid = str(users.create_user(email, "S3cretpw!")["id"])

    with get_pool().connection() as conn:
        # Owned by this user already, but kind='team' — outside the partial
        # index, and invisible to step 2's `kind = 'personal'` predicate.
        pending = conn.execute(
            "INSERT INTO organizations (name, kind, owner_user_id) "
            "VALUES (%s, 'team', %s) RETURNING id",
            (email, uid),
        ).fetchone()["id"]

    result, raised = _claim_mid_call(
        lambda: orgs.ensure_personal_org(uid, email),
        "UPDATE organizations SET kind = 'personal' WHERE id = %s",
        (pending,),
    )

    assert raised is None, f"ensure_personal_org must never raise, got {raised!r}"
    assert result == str(pending), (
        "the colliding row is the user's personal org now — hand that back "
        "rather than inventing a second one"
    )

    with get_pool().connection() as conn:
        owned = conn.execute(
            "SELECT id FROM organizations WHERE kind = 'personal' "
            "AND owner_user_id = %s",
            (uid,),
        ).fetchall()
        seat = conn.execute(
            "SELECT org_role FROM org_members WHERE org_id = %s AND user_id = %s",
            (pending, uid),
        ).fetchone()
    assert len(owned) == 1, "uq_org_owner_personal must still hold"
    assert seat is not None and seat["org_role"] == "org_admin", (
        "the caller must be left with a membership — zero memberships mints an "
        "org-less, unscoped-learning token at their next login"
    )


# ---------------------------------------------------------------------------
# backfill_personal_org_owners and uq_org_owner_personal.
#
# Both passes end in `NOT EXISTS (... owner_user_id = <candidate>)`, evaluated
# against the UPDATE's own statement snapshot. A concurrent ensure_personal_org
# that has WRITTEN its claim but not committed is invisible to that snapshot,
# and the row the backfill is updating was not touched by that other
# transaction, so there is no EvalPlanQual re-check to correct the verdict: the
# UPDATE writes, blocks on the partial unique index, and raises UniqueViolation
# the moment the other transaction commits.
#
# That abort takes the WHOLE migration with it — both passes run in one
# transaction — so it is a boot-time crash out of a data race the module's own
# advisory-lock comment already treats as expected. Same interleaving as the
# two tests above, aimed the other way: there the backfill is the claimer and
# ensure_personal_org is under test; here ensure_personal_org's own step-3
# INSERT is the claimer and the backfill is under test.
#
# The claimer org's name is deliberately NOT the user's email: a duplicate name
# would make pass 2 skip on its OWN ambiguity guard, and the test would pass
# without ever exercising the "already owned" one.
# ---------------------------------------------------------------------------

def _personal_orgs_owned_by(uid: str) -> list:
    with get_pool().connection() as conn:
        return conn.execute(
            "SELECT id FROM organizations WHERE kind = 'personal' "
            "AND owner_user_id = %s",
            (uid,),
        ).fetchall()


def _owner_of(org_id) -> str | None:
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT owner_user_id FROM organizations WHERE id = %s", (org_id,)
        ).fetchone()
    return None if row["owner_user_id"] is None else str(row["owner_user_id"])


def test_backfill_pass_1_survives_a_concurrent_uncommitted_claim(repos):
    """Pass 1 (sole remaining member) against a live login that has already
    INSERTed its own personal org for the same user and not yet committed.

    Before the users-row lock this raised UniqueViolation out of
    backfill_personal_org_owners, aborting the migration and — because
    init_org_db had no per-migration guard — skipping init_invitations_db and
    logging "auth unavailable until Postgres is reachable" for what is a data
    race.

    The correct outcome is the one the docstring already promises for an
    ambiguous row: claim NEITHER, never raise. The user keeps exactly one
    personal org (the one the concurrent login just made), and the org this
    pass was looking at is left NULL for a later boot to reconsider.
    """
    users, orgs = repos
    email = _unique_email()
    uid = str(users.create_user(email, "S3cretpw!")["id"])

    with get_pool().connection() as conn:
        sole = conn.execute(
            "INSERT INTO organizations (name, kind) VALUES (%s, 'personal') "
            "RETURNING id",
            (f"Sole Member {uuid.uuid4().hex[:8]}",),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO org_members (org_id, user_id, org_role) "
            "VALUES (%s, %s, 'org_admin')",
            (sole, uid),
        )
        conn.commit()

    _result, raised = _claim_mid_call(
        orgs.backfill_personal_org_owners,
        "INSERT INTO organizations (name, kind, owner_user_id) "
        "VALUES (%s, 'personal', %s)",
        (f"Concurrent Login {uuid.uuid4().hex[:8]}", uid),
    )

    assert raised is None, f"the boot path raised: {raised!r}"
    assert _owner_of(sole) is None, (
        "the concurrently claimed user must be skipped, not double-claimed"
    )
    assert len(_personal_orgs_owned_by(uid)) == 1, (
        "uq_org_owner_personal must still hold"
    )


def test_backfill_pass_2_survives_a_concurrent_uncommitted_claim(repos):
    """Pass 2 (name == email) against the same interleaving — the shape this
    branch exists for: a user who cycled team -> solo, whose vacated personal
    org still carries their email as its name, logging in on another instance
    while this migration runs.

    Pass 2 is reached only when pass 1 cannot claim the row, so the org here
    has NO members; that is what makes this a second, independent site rather
    than a repeat of the test above.
    """
    users, orgs = repos
    email = _unique_email()
    uid = str(users.create_user(email, "S3cretpw!")["id"])

    with get_pool().connection() as conn:
        vacated = conn.execute(
            "INSERT INTO organizations (name, kind) VALUES (%s, 'personal') "
            "RETURNING id",
            (email,),
        ).fetchone()["id"]
        conn.commit()

    _result, raised = _claim_mid_call(
        orgs.backfill_personal_org_owners,
        "INSERT INTO organizations (name, kind, owner_user_id) "
        "VALUES (%s, 'personal', %s)",
        (f"Concurrent Login {uuid.uuid4().hex[:8]}", uid),
    )

    assert raised is None, f"the boot path raised: {raised!r}"
    assert _owner_of(vacated) is None, (
        "the concurrently claimed user must be skipped, not double-claimed"
    )
    assert len(_personal_orgs_owned_by(uid)) == 1, (
        "uq_org_owner_personal must still hold"
    )

"""test_runs gains org_id: written at record_start, exposed on read, backfilled."""

import uuid
from unittest.mock import patch

import psycopg
import pytest

from src.backend.auth import db as auth_db
from src.backend.auth.repository import UserRepository
from src.backend.auth.org_repository import OrgRepository
from src.backend.core.run_registry import RunRegistry

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


def _run_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture(scope="module")
def registry():
    # The isolated auth_test schema already holds users/organizations/org_members
    # (Phase 1a conftest). RunRegistry creates test_runs in the same DB; point it
    # at the same DSN the isolated pool uses.
    reg = RunRegistry(dsn=auth_db.get_pool().conninfo)
    yield reg
    reg.close()


def test_record_start_persists_org_id(registry):
    users, orgs = UserRepository(), OrgRepository()
    user = users.create_user(f"rr-{uuid.uuid4().hex[:8]}@e.com", "S3cretpw!")
    org_id = orgs.ensure_personal_org(str(user["id"]), user["email"])
    rid = _run_id()
    registry.record_start(
        rid,
        {"user_id": str(user["id"]), "email": user["email"], "org_id": org_id},
        "do a thing", "generated",
    )
    assert registry.get_run(rid)["org_id"] == org_id
    assert registry.get_run_owner(rid) == (str(user["id"]), org_id)


def test_list_runs_preserves_unattributed_error_rows(registry):
    """Auth-off (AUTH_ENFORCED=false) dev runs have no owner, and a direct
    paste-execute carries no NL query — so a real failure can be NULL user_id +
    NULL user_query + status='error'. History MUST still show it: that shape is
    indistinguishable from a real failure, so dropping it would hide the very
    errors a developer needs to see. Junk rows are prevented at the source (the
    /execute-test endpoint rejects an empty body with 400 before record_start),
    not filtered out at read time. This guards against re-introducing such a
    read-time filter."""
    queryless_rid = _run_id()
    queried_rid = _run_id()
    # Paste-execute failure, no NL query typed (the shape a read-time filter ate).
    registry.record_start(queryless_rid, None, None, "error", robot_code="*** Test Cases ***")
    # Paste-execute failure with a query supplied for learning.
    registry.record_start(queried_rid, None, "click the login button", "error")

    rows, _total = registry.list_runs(limit=200)
    ids = {r["run_id"] for r in rows}
    assert queryless_rid in ids  # real failure preserved despite no owner/query
    assert queried_rid in ids


def test_backfill_maps_existing_rows_to_owner_org(registry):
    users, orgs = UserRepository(), OrgRepository()
    user = users.create_user(f"bf-{uuid.uuid4().hex[:8]}@e.com", "S3cretpw!")
    rid = _run_id()
    # Simulate a pre-tenancy row: user attributed, org_id NULL. The org is
    # provisioned AFTER record_start so record_start's own org_members
    # fallback (Part B) has no membership to find yet and genuinely leaves
    # org_id NULL -- the state backfill_org_ids exists to repair.
    registry.record_start(
        rid, {"user_id": str(user["id"]), "email": user["email"]}, "legacy", "passed"
    )
    assert registry.get_run(rid)["org_id"] is None

    org_id = orgs.ensure_personal_org(str(user["id"]), user["email"])
    updated = registry.backfill_org_ids()
    assert updated >= 1
    assert registry.get_run(rid)["org_id"] == org_id
    # Idempotent: a second pass does not change THIS row. Asserting a global
    # count of 0 instead would depend on no sibling in this shared schema
    # having left an unattributed row behind:
    # test_backfill_attributes_org_member_rows_too briefly creates that exact
    # state, then backfills it itself, so it leaves nothing behind on a clean
    # run -- but a global count is still needlessly fragile, since it would
    # break if that sibling ever failed between creating and backfilling its
    # row, or if a future test left the same shape unbackfilled.
    registry.backfill_org_ids()
    assert registry.get_run(rid)["org_id"] == org_id


def test_record_start_derives_org_id_for_org_member(registry):
    """record_start with org_id=None resolves it via org_members when the
    user's single membership is org_role='org_member' -- the case the old
    org_admin-only predicate silently dropped (Part B). This is the test
    that fails against the previous design."""
    users, orgs = UserRepository(), OrgRepository()
    owner = users.create_user(f"rr-owner-{uuid.uuid4().hex[:8]}@e.com", "S3cretpw!")
    member = users.create_user(f"rr-member-{uuid.uuid4().hex[:8]}@e.com", "S3cretpw!")
    team_org_id = orgs.create_team_org("Team RR Member", str(owner["id"]))
    orgs.add_member(team_org_id, str(member["id"]), "org_member")

    rid = _run_id()
    registry.record_start(
        rid, {"user_id": str(member["id"]), "email": member["email"]}, "do a thing", "generated",
    )
    assert registry.get_run(rid)["org_id"] == team_org_id


def test_record_start_derives_org_id_for_org_admin(registry):
    """Same fallback, org_role='org_admin' -- must keep working."""
    users, orgs = UserRepository(), OrgRepository()
    owner = users.create_user(f"rr-admin-{uuid.uuid4().hex[:8]}@e.com", "S3cretpw!")
    team_org_id = orgs.create_team_org("Team RR Admin", str(owner["id"]))

    rid = _run_id()
    registry.record_start(
        rid, {"user_id": str(owner["id"]), "email": owner["email"]}, "do a thing", "generated",
    )
    assert registry.get_run(rid)["org_id"] == team_org_id


def test_record_start_leaves_org_id_null_without_membership(registry):
    """A user_id with no org_members row at all -- org_id stays NULL, nothing raises."""
    users = UserRepository()
    user = users.create_user(f"rr-none-{uuid.uuid4().hex[:8]}@e.com", "S3cretpw!")

    rid = _run_id()
    registry.record_start(
        rid, {"user_id": str(user["id"]), "email": user["email"]}, "do a thing", "generated",
    )
    assert registry.get_run(rid)["org_id"] is None


def test_record_start_survives_non_uuid_user_id(registry):
    """A non-UUID user_id is rejected by shape in _lookup_org_id's UUID
    pre-check, before it ever reaches the pool -- org_id stays NULL AND the
    row is still written. This guards the pre-check itself, not the
    separate-connection isolation (see
    test_record_start_survives_lookup_failure_at_db_level below for that)."""
    rid = _run_id()
    registry.record_start(
        rid, {"user_id": "not-a-uuid", "email": "x@e.com"}, "do a thing", "generated",
    )
    row = registry.get_run(rid)
    assert row is not None
    assert row["org_id"] is None


def test_record_start_survives_lookup_failure_at_db_level(registry):
    """A user_id that IS a valid UUID, but whose org_members lookup fails at
    the DB level (here: the pool cannot hand out a connection) -- org_id
    stays NULL AND the row is still written. This is the case the UUID
    pre-check does NOT intercept, and it is what actually proves the lookup
    runs on its own connection: the failure is injected at the boundary
    _lookup_org_id borrows from (_pool.connection()), not inside
    _lookup_org_id itself, so a failure that shared the INSERT's connection/
    transaction would poison it and the row would never be written at all
    -- the transaction-poisoning mode this task exists to guard against."""
    real_connection = registry._pool.connection
    calls = {"n": 0}

    def flaky_connection(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise psycopg.OperationalError("simulated connection failure")
        return real_connection(*args, **kwargs)

    rid = _run_id()
    user_id = str(uuid.uuid4())  # valid UUID; no org_members row needed
    with patch.object(registry._pool, "connection", side_effect=flaky_connection):
        registry.record_start(
            rid, {"user_id": user_id, "email": "x@e.com"}, "do a thing", "generated",
        )
    row = registry.get_run(rid)
    assert row is not None
    assert row["org_id"] is None


def test_record_start_uses_supplied_org_id_verbatim(registry):
    """A token-supplied org_id is used as-is, even when it disagrees with the
    user's real org_members row -- proving the fallback never overrides an
    explicit value, AND never runs the lookup at all (no extra query)."""
    users, orgs = UserRepository(), OrgRepository()
    user = users.create_user(f"rr-verbatim-{uuid.uuid4().hex[:8]}@e.com", "S3cretpw!")
    real_org_id = orgs.ensure_personal_org(str(user["id"]), user["email"])
    supplied_org_id = str(uuid.uuid4())
    assert supplied_org_id != real_org_id

    rid = _run_id()
    with patch.object(registry, "_lookup_org_id") as mock_lookup:
        registry.record_start(
            rid,
            {"user_id": str(user["id"]), "email": user["email"], "org_id": supplied_org_id},
            "do a thing", "generated",
        )
        mock_lookup.assert_not_called()
    assert registry.get_run(rid)["org_id"] == supplied_org_id


def test_record_start_with_no_user_leaves_org_id_null(registry):
    """user=None (unattributed / auth-off run) -- unchanged behaviour, nothing raises."""
    rid = _run_id()
    registry.record_start(rid, None, None, "error")
    row = registry.get_run(rid)
    assert row is not None
    assert row["org_id"] is None


def test_backfill_attributes_org_member_rows_too(registry):
    """backfill_org_ids must attribute an org_member's rows, not only an
    org_admin's -- the identical predicate defect Part B's lookup fixes at
    write time, fixed here at backfill time too."""
    users, orgs = UserRepository(), OrgRepository()
    owner = users.create_user(f"bf-owner-{uuid.uuid4().hex[:8]}@e.com", "S3cretpw!")
    member = users.create_user(f"bf-member-{uuid.uuid4().hex[:8]}@e.com", "S3cretpw!")

    rid = _run_id()
    # Pre-tenancy row for a user who is not yet a member of anything, so
    # record_start's own fallback finds nothing and org_id stays NULL.
    registry.record_start(
        rid, {"user_id": str(member["id"]), "email": member["email"]}, "legacy", "passed",
    )
    assert registry.get_run(rid)["org_id"] is None

    team_org_id = orgs.create_team_org("Team BF Member", str(owner["id"]))
    orgs.add_member(team_org_id, str(member["id"]), "org_member")

    updated = registry.backfill_org_ids()
    assert updated >= 1
    assert registry.get_run(rid)["org_id"] == team_org_id

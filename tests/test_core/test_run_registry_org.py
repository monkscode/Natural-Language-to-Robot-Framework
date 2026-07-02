"""test_runs gains org_id: written at record_start, exposed on read, backfilled."""

import uuid

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
    org_id = orgs.ensure_personal_org(str(user["id"]), user["email"])
    rid = _run_id()
    # Simulate a pre-tenancy row: user attributed, org_id NULL.
    registry.record_start(
        rid, {"user_id": str(user["id"]), "email": user["email"]}, "legacy", "passed"
    )
    assert registry.get_run(rid)["org_id"] is None

    updated = registry.backfill_org_ids()
    assert updated >= 1
    assert registry.get_run(rid)["org_id"] == org_id
    # Idempotent: a second pass changes nothing.
    assert registry.backfill_org_ids() == 0

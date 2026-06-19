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

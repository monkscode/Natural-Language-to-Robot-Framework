"""A re-run lands in the folder of the run it was cloned from.

The inheritance is read at the endpoint and written at the registry, so this
covers both ends of the wire plus the service hop between them:

- POST /execute-test {rerun_of}: the source row is now read through the
  caller's OWN visibility scope, so the folder id handed to stream_execute_only
  is one the caller can see — an org_admin re-running a member's run never
  learns that member's private folder, let alone files a run into it.
- stream_execute_only -> _record_run -> record_start: the id reaches the new
  run's history row, and record_start drops it when the folder belongs to a
  different org than the row.

The inheritance follows the IMMEDIATE source, never rerun_of: rerun_of is
root-flattened at creation, so a re-run of a re-run that has since been moved
would otherwise be filed back into the original's folder.

Runs on its own Postgres schema — never the live public one. No Docker.

Referenced by: src/backend/api/endpoints.py (_rerun_from_history),
               src/backend/services/workflow_service.py (stream_execute_only).
Depends on: core/config.py (DATABASE_URL).
"""

import asyncio
import uuid
from unittest.mock import patch

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.backend.auth.jwt_utils import require_user
from src.backend.core.config import settings
from src.backend.core.run_registry import RunRegistry

_SCHEMA = "rerun_group_test"

_ORG_A = "org-a"
_ORG_B = "org-b"

_MEMBER = {"user_id": "u-member", "email": "member@test.local",
           "org_id": _ORG_A, "org_role": "org_member"}
_ORG_ADMIN = {"user_id": "u-orgadmin", "email": "orgadmin@test.local",
              "org_id": _ORG_A, "org_role": "org_admin"}
# A platform admin whose own org is B — the cross-org case.
_PLATFORM = {"user_id": "u-platform", "email": "platform@test.local",
             "org_id": _ORG_B, "org_role": "org_member"}

_CODE = "*** Settings ***\n*** Test Cases ***\nDummy\n    Log    hi\n"


def _rid() -> str:
    return str(uuid.uuid4())


@pytest.fixture(scope="module")
def admin_conn():
    conn = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def reg(admin_conn):
    admin_conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    admin_conn.execute(f"CREATE SCHEMA {_SCHEMA}")
    sep = "&" if "?" in settings.DATABASE_URL else "?"
    dsn = settings.DATABASE_URL + f"{sep}options=-c%20search_path%3D{_SCHEMA},public"
    r = RunRegistry(dsn=dsn)
    yield r
    r.close()
    admin_conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")


@pytest.fixture(autouse=True)
def _clean(reg, admin_conn):
    # ONE statement, not two: test_runs' foreign key makes run_groups
    # untruncatable on its own.
    admin_conn.execute(f"TRUNCATE {_SCHEMA}.test_runs, {_SCHEMA}.run_groups")


@pytest.fixture
def stored_group_id(admin_conn):
    """t.group_id as actually stored — the read paths report g.group_id, which
    the visibility join nulls out independently of what is on the row."""
    def _read(run_id):
        row = admin_conn.execute(
            f"SELECT group_id FROM {_SCHEMA}.test_runs WHERE run_id = %s",
            (run_id,),
        ).fetchone()
        assert row is not None, f"no test_runs row written for {run_id}"
        return row[0]

    return _read


def _seed_run(reg, user, rid, query="search for shoes", rerun_of=None):
    reg.record_start(rid, user, query, "passed", robot_code=_CODE,
                     rerun_of=rerun_of)
    return rid


def _file_run(reg, user, rid, group_id, is_org_admin=False):
    assert reg.assign_runs(user["org_id"], user["user_id"], is_org_admin,
                           [rid], group_id), "seed: assign_runs refused"


def _rerun_client(registry, user, validated_admin=False):
    """TestClient over the api router with stream_execute_only faked; returns
    (client, captured) where captured holds the kwargs the rerun passed in.

    is_validated_admin is patched where history_scope reads it: the rerun path
    now takes its admin flag off the scope, so one call answers both the
    ownership gate and the group-visibility join."""
    from src.backend.api.endpoints import router as api_router

    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[require_user] = lambda: user

    captured = {}

    async def fake_stream(robot_code, user_query=None, workflow_id=None,
                          user=None, history_query=None, rerun_of=None,
                          group_id=None):
        captured.update(robot_code=robot_code, user=user, rerun_of=rerun_of,
                        group_id=group_id)
        yield "data: {\"stage\": \"execution\", \"status\": \"complete\"}\n\n"

    patchers = [
        patch("src.backend.api.endpoints.get_run_registry", return_value=registry),
        patch("src.backend.api.history_scope.is_validated_admin",
              return_value=validated_admin),
        patch("src.backend.api.endpoints.stream_execute_only", fake_stream),
    ]
    for p in patchers:
        p.start()
    client = TestClient(app)
    client._patchers = patchers
    return client, captured


def _stop(client):
    for p in client._patchers:
        p.stop()
    client.close()


def _rerun(registry, user, source_run_id, validated_admin=False):
    client, captured = _rerun_client(registry, user, validated_admin)
    try:
        resp = client.post("/execute-test", json={"rerun_of": source_run_id})
    finally:
        _stop(client)
    return resp, captured


# ---------------------------------------------------------------------------
# Endpoint: which folder id reaches stream_execute_only
# ---------------------------------------------------------------------------

class TestRerunEndpointInheritsTheSourceFolder:
    def test_org_folder_is_inherited(self, reg):
        gid = reg.create_group(_ORG_A, _MEMBER["user_id"], "Checkout", "org")["group_id"]
        rid = _seed_run(reg, _MEMBER, _rid())
        _file_run(reg, _MEMBER, rid, gid)

        resp, captured = _rerun(reg, _MEMBER, rid)

        assert resp.status_code == 200
        assert captured["group_id"] == gid

    def test_own_private_folder_is_inherited(self, reg):
        gid = reg.create_group(_ORG_A, _MEMBER["user_id"], "My Drafts",
                               "private")["group_id"]
        rid = _seed_run(reg, _MEMBER, _rid())
        _file_run(reg, _MEMBER, rid, gid)

        resp, captured = _rerun(reg, _MEMBER, rid)

        assert resp.status_code == 200
        assert captured["group_id"] == gid

    def test_chain_rerun_follows_the_immediate_source_not_the_original(self, reg):
        """A in F -> re-run gives B -> B is moved to G -> re-run B lands in G.

        rerun_of still names A (root-flattened for feedback routing), so an
        implementation that inherited from rerun_of would answer F here."""
        f = reg.create_group(_ORG_A, _MEMBER["user_id"], "F", "org")["group_id"]
        g = reg.create_group(_ORG_A, _MEMBER["user_id"], "G", "org")["group_id"]
        rid_a = _seed_run(reg, _MEMBER, _rid())
        _file_run(reg, _MEMBER, rid_a, f)
        rid_b = _seed_run(reg, _MEMBER, _rid(), rerun_of=rid_a)
        _file_run(reg, _MEMBER, rid_b, g)

        resp, captured = _rerun(reg, _MEMBER, rid_b)

        assert resp.status_code == 200
        assert captured["rerun_of"] == rid_a   # lineage unchanged
        assert captured["group_id"] == g       # folder from B, not from A

    def test_org_admin_never_learns_a_members_private_folder(self, reg, stored_group_id):
        """caller_can_read rule 4 lets an org_admin re-run a member's run. The
        member's PRIVATE folder is invisible to them, so the new run must be
        ungrouped — a run in a folder its own owner cannot see is forbidden."""
        gid = reg.create_group(_ORG_A, _MEMBER["user_id"], "Member Private",
                               "private")["group_id"]
        rid = _seed_run(reg, _MEMBER, _rid())
        _file_run(reg, _MEMBER, rid, gid)
        # The source really is filed there — the None below is the visibility
        # filter, not a failed seed.
        assert stored_group_id(rid) == gid

        resp, captured = _rerun(reg, _ORG_ADMIN, rid)

        assert resp.status_code == 200
        assert captured["group_id"] is None
        # Contrast on the SAME source run: its owner does inherit the folder,
        # so the None above is the visibility join at work, not a group_id
        # that is None for everyone.
        _, owner_captured = _rerun(reg, _MEMBER, rid)
        assert owner_captured["group_id"] == gid

    def test_ungrouped_source_stays_ungrouped(self, reg, stored_group_id):
        rid = _seed_run(reg, _MEMBER, _rid())
        assert stored_group_id(rid) is None

        resp, captured = _rerun(reg, _MEMBER, rid)

        assert resp.status_code == 200
        assert captured["group_id"] is None

    def test_platform_admin_read_is_unscoped_so_the_org_guard_cannot_live_here(self, reg):
        """A platform admin sees every org, so the endpoint hands on a folder
        id from the SOURCE run's org. Only record_start knows the org actually
        written on the new row, which is why the cross-org guard lives there
        (see TestRerunServiceWritesTheFolder for the other half)."""
        gid = reg.create_group(_ORG_A, _MEMBER["user_id"], "Checkout", "org")["group_id"]
        rid = _seed_run(reg, _MEMBER, _rid())
        _file_run(reg, _MEMBER, rid, gid)

        resp, captured = _rerun(reg, _PLATFORM, rid, validated_admin=True)

        assert resp.status_code == 200
        assert captured["group_id"] == gid


# ---------------------------------------------------------------------------
# Service: stream_execute_only carries the folder id onto the new run's row
# ---------------------------------------------------------------------------

class TestRerunServiceWritesTheFolder:
    def _execute(self, reg, user, group_id):
        from src.backend.services import workflow_service as ws

        seen = {}

        async def fake_docker(run_id, robot_code, user_query, release_slot):
            seen["run_id"] = run_id
            release_slot()
            yield "data: ok\n\n"

        with patch.object(ws, "_stream_docker_execution", fake_docker), \
             patch.object(ws, "get_run_registry", return_value=reg):
            async def consume():
                async for _ in ws.stream_execute_only(
                    _CODE, None, None, user=user,
                    history_query="search for shoes", rerun_of="root-run-id",
                    group_id=group_id,
                ):
                    pass
            asyncio.run(consume())

        return seen["run_id"]

    def test_folder_lands_on_the_new_run_row(self, reg, stored_group_id):
        gid = reg.create_group(_ORG_A, _MEMBER["user_id"], "Checkout", "org")["group_id"]
        new_run_id = self._execute(reg, _MEMBER, gid)
        assert stored_group_id(new_run_id) == gid

    def test_cross_org_folder_is_dropped_but_the_row_survives(self, reg, stored_group_id):
        """The platform-admin case end to end: the endpoint forwarded an org A
        folder, the run is being written for org B, so the tag is dropped and
        the history row is written anyway."""
        gid = reg.create_group(_ORG_A, _MEMBER["user_id"], "Checkout", "org")["group_id"]
        new_run_id = self._execute(reg, _PLATFORM, gid)
        assert stored_group_id(new_run_id) is None
        assert reg.get_run(new_run_id)["user_query"] == "search for shoes"

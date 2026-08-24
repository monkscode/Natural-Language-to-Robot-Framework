"""record_start's folder write: the org guard, the FK retry, write-once.

T4b makes a re-run inherit the folder of the run it was cloned from, and
record_start is where the inherited id is actually written. Three things have
to hold there and nowhere else:

- the folder must belong to the org being written on the new run's row (only
  record_start knows that org — T1's _lookup_org_id can supply it when the
  token did not), or a platform admin's re-run files a run into another org's
  folder;
- the folder can be deleted between the read and the INSERT, and
  fk_test_runs_group turns that into a ForeignKeyViolation that the outer
  except swallows — losing the whole history row, not just the folder tag;
- the id is write-once like ownership, so a later record_start for the same
  run cannot drag a run the user moved mid-flight back to the source folder.

Runs on its own Postgres schema — never the live public one.

Referenced by: src/backend/core/run_registry.py (record_start).
Depends on: core/config.py (DATABASE_URL).
"""

import uuid
from unittest.mock import patch

import psycopg
import pytest

from src.backend.core.config import settings
from src.backend.core.run_registry import RunRegistry

_SCHEMA = "run_inherit_test"

_ORG_A = "org-a"
_ORG_B = "org-b"
_USER = {"user_id": "u1", "email": "u1@test.local", "org_id": _ORG_A}


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
    """t.group_id as actually stored — get_run reports g.group_id, which the
    visibility join (and a deleted folder) can null out independently."""
    def _read(run_id):
        row = admin_conn.execute(
            f"SELECT group_id FROM {_SCHEMA}.test_runs WHERE run_id = %s",
            (run_id,),
        ).fetchone()
        assert row is not None, f"no test_runs row written for {run_id}"
        return row[0]

    return _read


def test_folder_in_the_runs_own_org_is_written(reg, stored_group_id):
    gid = reg.create_group(_ORG_A, "u1", "Checkout")["group_id"]
    rid = _rid()
    reg.record_start(rid, _USER, "q", "running", group_id=gid)
    assert stored_group_id(rid) == gid


def test_folder_from_another_org_is_dropped(reg, stored_group_id):
    """A platform admin's re-run reads through the UNFILTERED group join, so
    the id handed to record_start can name a folder in a different org."""
    gid = reg.create_group(_ORG_B, "u-other", "Their Folder")["group_id"]
    rid = _rid()
    reg.record_start(rid, _USER, "q", "running", group_id=gid)
    assert stored_group_id(rid) is None


def test_a_colleagues_folder_in_the_same_org_is_inherited(reg, stored_group_id):
    """A folder belongs to the ORG, not to whoever made it, so who created it
    does not enter the predicate — only which org it is in. Re-running a
    colleague's published test keeps the new run beside its source, which is
    the behaviour a shared folder is FOR."""
    gid = reg.create_group(_ORG_A, "u-colleague", "Team Checkout")["group_id"]
    rid = _rid()
    reg.record_start(rid, _USER, "q", "running", group_id=gid)
    assert stored_group_id(rid) == gid


def test_folder_is_dropped_when_the_run_has_no_org(reg, stored_group_id):
    """AUTH_ENFORCED=false writes rows with no org at all. Every folder has a
    NOT NULL org_id, so there is no org such a row could match — inherit
    nothing rather than file an org-less run into someone's folder."""
    gid = reg.create_group(_ORG_A, "u1", "Checkout")["group_id"]
    rid = _rid()
    reg.record_start(rid, None, "q", "running", group_id=gid)
    assert stored_group_id(rid) is None


def test_group_id_is_write_once(reg, stored_group_id):
    """Matches ownership: a second record_start for the same run cannot move
    it — the user may have filed it somewhere else in the meantime."""
    first = reg.create_group(_ORG_A, "u1", "First")["group_id"]
    second = reg.create_group(_ORG_A, "u1", "Second")["group_id"]
    rid = _rid()
    reg.record_start(rid, _USER, "q", "running", group_id=first)
    reg.record_start(rid, _USER, "q", "passed", group_id=second)
    assert stored_group_id(rid) == first
    reg.record_start(rid, _USER, "q", "passed")
    assert stored_group_id(rid) == first


def test_folder_deleted_mid_write_still_writes_the_run(reg, admin_conn, stored_group_id):
    """The race the foreign key created: delete_group lands between the org
    guard's read and the INSERT. Without the retry the ForeignKeyViolation is
    swallowed by record_start's outer except and NO row is written at all —
    strictly worse than a missing folder tag."""
    gid = reg.create_group(_ORG_A, "u1", "Doomed")["group_id"]
    rid = _rid()

    real_guard = RunRegistry._fileable_group_id

    def _delete_after_reading(self, group_id, org_id):
        inherited = real_guard(self, group_id, org_id)
        admin_conn.execute(
            f"DELETE FROM {_SCHEMA}.run_groups WHERE group_id = %s", (group_id,)
        )
        return inherited

    with patch.object(RunRegistry, "_fileable_group_id", _delete_after_reading):
        reg.record_start(rid, _USER, "the run that must survive", "running",
                         group_id=gid)

    # stored_group_id asserts the row exists — that is the point of the retry.
    assert stored_group_id(rid) is None
    row = reg.get_run(rid)
    assert row["user_query"] == "the run that must survive"
    assert row["status"] == "running"


def test_retry_reuses_the_org_id_already_derived(reg, admin_conn, stored_group_id):
    """T1 settles org_id into a local BEFORE the connection block precisely so
    this retry cannot lose it or re-trigger the org_members lookup."""
    gid = reg.create_group(_ORG_A, "u1", "Doomed")["group_id"]
    rid = _rid()
    orgless_user = {"user_id": str(uuid.uuid4()), "email": "u@test.local"}
    real_guard = RunRegistry._fileable_group_id

    def _delete_after_reading(self, group_id, org_id):
        inherited = real_guard(self, group_id, org_id)
        admin_conn.execute(
            f"DELETE FROM {_SCHEMA}.run_groups WHERE group_id = %s", (group_id,)
        )
        return inherited

    with patch.object(RunRegistry, "_lookup_org_id",
                      return_value=_ORG_A) as lookup, \
         patch.object(RunRegistry, "_fileable_group_id", _delete_after_reading):
        reg.record_start(rid, orgless_user, "q", "running", group_id=gid)

    assert lookup.call_count == 1
    assert stored_group_id(rid) is None
    assert reg.get_run(rid)["org_id"] == _ORG_A


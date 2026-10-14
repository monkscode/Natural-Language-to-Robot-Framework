"""Activity rows name the test and the version the result ran (spec 7.5).

A result belongs to a test now, so the re-run pill this replaces was lineage
between two RESULTS and this is lineage to the TEST. Three facts have to reach
the row for that, and `list_runs` carried none of them before this task: the
run's test id, the test's own name/description, and the version number the
result recorded.

The version is the only real addition to the query — `_group_join` already
joins `tests` (a row's folder is COALESCE(te.group_id, t.group_id)), so the
test columns cost nothing, while `test_versions` is a new LEFT JOIN on
`version_id`, its primary key, so it cannot fan a row out.

Also here: D7's author rule. A result carrying `ran_as_platform_admin` has its
author's EMAIL withheld from any caller who is not themselves a platform
admin, and the flag shipped in its place — the ruling's own words are that the
email stays STORED so audit loses nothing, and that a customer org is not
handed a named individual's address. Withholding it on the SERVER rather than
in the SPA is what makes the second half true: the row's author cell is a
"filter by this user" button, so an email that reaches the client reaches the
search box too.

Both endpoints are covered, because the drawer reads the detail and the table
reads the list, and a field that disagreed between them would put two answers
on screen for one run.

The D7 caller here is an org_admin. That is not a convenience: a PLAIN member
cannot see a colleague's unfiled run at all (caller_can_access rule 5 ends
`owner_id == caller_uid`), so the smallest caller who can actually reach an
admin-authority row without also seeding a folder is the org's own admin —
and "not themselves a platform admin" is the population D7 names.

Referenced by: src/backend/api/history_endpoints.py,
               src/backend/core/run_registry.py.
Depends on: nothing outside this file (its own isolated Postgres schema).
"""

import uuid
from unittest.mock import patch

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.backend.api.history_endpoints import router as history_router
from src.backend.auth.jwt_utils import require_user
from src.backend.core.config import settings
from src.backend.core.run_registry import RunRegistry

_SCHEMA = "history_identity_test"

# run_detail parses its path segment as a UUID before it reads anything, so
# every id here has to be a real one.
_RID_1 = str(uuid.UUID(int=1))
_RID_2 = str(uuid.UUID(int=2))
_RID_ADMIN = str(uuid.UUID(int=3))
_RID_PLAIN = str(uuid.UUID(int=4))

_USER1 = {"user_id": "u1", "email": "user1@test.local", "role": "user",
          "org_id": "org-1", "org_role": "org_admin"}
_ADMIN = {"user_id": "adm", "email": "admin@test.local", "role": "admin",
          "org_id": "org-1"}

_CODE = "*** Test Cases ***\nT\n    Log    hi"


@pytest.fixture(scope="module")
def _admin_conn():
    try:
        conn = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    except Exception as exc:  # noqa: BLE001 — no database = skip, not fail
        pytest.skip(f"Postgres unavailable: {exc}")
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def _registry(_admin_conn):
    _admin_conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    _admin_conn.execute(f"CREATE SCHEMA {_SCHEMA}")
    dsn = settings.DATABASE_URL + f"?options=-c%20search_path%3D{_SCHEMA},public"
    reg = RunRegistry(dsn=dsn)
    yield reg
    reg.close()
    _admin_conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")


@pytest.fixture
def registry(_registry, _admin_conn):
    """Empty run/test/version tables per test — the tests table too, since
    key_n is allocated per org and a leftover row shifts every later key."""
    _admin_conn.execute(
        f"TRUNCATE {_SCHEMA}.test_runs, {_SCHEMA}.tests,"
        f" {_SCHEMA}.test_versions, {_SCHEMA}.run_groups CASCADE")
    return _registry


def _client(registry, user):
    app = FastAPI()
    app.include_router(history_router, prefix="/api")
    app.dependency_overrides[require_user] = lambda: user
    patcher = patch(
        "src.backend.api.history_endpoints.get_run_registry",
        return_value=registry)
    patcher.start()
    client = TestClient(app)
    client._registry_patcher = patcher
    return client


def _close(client):
    client._registry_patcher.stop()
    client.close()


def _row(registry, run_id):
    rows, _ = registry.list_runs(org_id="org-1")
    return next(r for r in rows if r["run_id"] == run_id)


# ---------------------------------------------------------------------------
# The registry — what the two reads carry
# ---------------------------------------------------------------------------

class TestRegistryCarriesTheTest:
    def test_list_runs_names_the_test_and_the_version_the_result_ran(
            self, registry):
        # A generation with code mints the test and its version 1, and the
        # run points at both (_attach_test's mint branch).
        registry.record_start(_RID_1, _USER1, "search for shoes", "passed",
                              robot_code=_CODE)

        row = _row(registry, _RID_1)

        assert row["test_id"]
        # D2 keeps tests.name NULL — the column is on the wire so the client
        # resolves the label through ONE definition, not because it is set.
        assert row["test_name"] is None
        assert row["test_query"] == "search for shoes"
        assert row["test_version_n"] == 1

    def test_the_version_climbs_with_the_test(self, registry):
        registry.record_start(_RID_1, _USER1, "search for shoes", "passed",
                              robot_code=_CODE)
        test_id = _row(registry, _RID_1)["test_id"]
        # A regeneration appends version 2, and its own run records it.
        registry.record_start(_RID_2, _USER1, "search for boots", "passed",
                              robot_code=_CODE + "\n    Log    again",
                              test_id=test_id, version_reason="regenerated")

        assert _row(registry, _RID_1)["test_version_n"] == 1
        assert _row(registry, _RID_2)["test_version_n"] == 2
        # The description travels WITH the version, so the older result's
        # test_query is the new one: this field names the test, not the run.
        assert _row(registry, _RID_1)["test_query"] == "search for boots"

    def test_a_failed_regeneration_names_its_test_and_no_version(
            self, registry):
        # Spec case 10 / D8(b): the run attaches to the test, no version is
        # written and current_version does not move.
        registry.record_start(_RID_1, _USER1, "search for shoes", "passed",
                              robot_code=_CODE)
        test_id = _row(registry, _RID_1)["test_id"]
        registry.record_start(_RID_2, _USER1, "search for shoes", "error",
                              test_id=test_id)

        row = _row(registry, _RID_2)
        assert row["test_id"] == test_id
        assert row["test_version_n"] is None

    def test_a_run_with_no_test_names_neither(self, registry):
        # D8(a): a generation that failed before any code existed. A
        # permanently legal row, not a migration artefact.
        registry.record_start(_RID_1, _USER1, "search for shoes", "error")

        row = _row(registry, _RID_1)
        assert row["test_id"] is None
        assert row["test_name"] is None
        assert row["test_query"] is None
        assert row["test_version_n"] is None

    def test_get_run_gives_the_drawer_the_same_answer_as_the_list(
            self, registry):
        # The table reads the list and the drawer reads the detail. Two
        # answers for one run is the defect this pins.
        registry.record_start(_RID_1, _USER1, "search for shoes", "passed",
                              robot_code=_CODE)

        listed = _row(registry, _RID_1)
        detail = registry.get_run(_RID_1, org_id="org-1", identified=True)

        for field in ("test_id", "test_name", "test_query", "test_version_n",
                      "ran_as_platform_admin"):
            assert detail[field] == listed[field], field

    def test_both_reads_report_the_authority_the_run_was_written_with(
            self, registry):
        registry.record_start(_RID_PLAIN, _USER1, "a member ran this",
                              "passed", robot_code=_CODE)
        registry.record_start(_RID_ADMIN, _ADMIN, "an admin ran this",
                              "passed", robot_code=_CODE,
                              is_platform_admin=True)

        rows, _ = registry.list_runs(org_id="org-1")
        by_id = {r["run_id"]: r for r in rows}
        assert by_id[_RID_PLAIN]["ran_as_platform_admin"] is False
        assert by_id[_RID_ADMIN]["ran_as_platform_admin"] is True
        assert registry.get_run(_RID_ADMIN)["ran_as_platform_admin"] is True


# ---------------------------------------------------------------------------
# D7 — the author column
# ---------------------------------------------------------------------------

def _seed_both_authorities(registry):
    registry.record_start(_RID_ADMIN, _ADMIN, "an admin ran this", "passed",
                          robot_code=_CODE, is_platform_admin=True)
    registry.record_start(_RID_PLAIN, _USER1, "a member ran this", "passed",
                          robot_code=_CODE)


class TestPlatformAdminAuthorship:
    def test_a_caller_who_is_not_a_platform_admin_is_not_handed_the_address(
            self, registry):
        _seed_both_authorities(registry)
        with patch("src.backend.api.history_scope.is_validated_admin",
                   return_value=False):
            client = _client(registry, _USER1)
            try:
                body = client.get("/api/history").json()
            finally:
                _close(client)

        by_id = {r["run_id"]: r for r in body["runs"]}
        assert by_id[_RID_ADMIN]["user_email"] is None
        assert by_id[_RID_ADMIN]["ran_as_platform_admin"] is True
        # An ordinary row is untouched: the org still says who wrote what.
        assert by_id[_RID_PLAIN]["user_email"] == "user1@test.local"
        assert by_id[_RID_PLAIN]["ran_as_platform_admin"] is False

    def test_the_search_box_cannot_be_used_to_derive_the_withheld_address(
            self, registry):
        """D7 has to reach the SEARCH, not only the row.

        The address is blanked on the row, but ?q= still MATCHED on it, and
        the match is a substring -- so a caller who did not know the address
        could derive it roughly 26 requests per character through the public
        API. R11-2 chose server-side suppression because "the next reader of
        row.user_email reintroduces the leak"; this clause was that reader.

        The three controls are what make the zero meaningful: an ordinary
        author is still searchable by address, the admin's own row is still
        findable by its query, and an unfiltered call still returns both --
        this narrows one term, it does not filter rows out.
        """
        _seed_both_authorities(registry)
        with patch("src.backend.api.history_scope.is_validated_admin",
                   return_value=False):
            client = _client(registry, _USER1)
            try:
                def total(q):
                    return client.get(f"/api/history?q={q}").json()["total"]

                assert total("admin@test.local") == 0
                assert total("admin@") == 0, "a substring derives it too"
                # Controls.
                assert total("user1@test.local") == 1
                assert total("an+admin+ran") == 1, "the row is still findable"
                assert client.get("/api/history").json()["total"] == 2
            finally:
                _close(client)

    def test_a_platform_admin_still_sees_the_address(self, registry):
        _seed_both_authorities(registry)
        with patch("src.backend.api.history_scope.is_validated_admin",
                   return_value=True):
            client = _client(registry, _ADMIN)
            try:
                body = client.get("/api/history").json()
            finally:
                _close(client)

        by_id = {r["run_id"]: r for r in body["runs"]}
        assert by_id[_RID_ADMIN]["user_email"] == "admin@test.local"

    def test_the_token_less_dev_caller_still_sees_the_address(self, registry):
        # AUTH_ENFORCED off. is_validated_admin(None) is False, so without the
        # caller_user_id term this caller — the one ownership rule 1 exempts
        # from everything — would be the only one debugging blind.
        _seed_both_authorities(registry)
        with patch("src.backend.api.history_scope.is_validated_admin",
                   return_value=False):
            client = _client(registry, None)
            try:
                body = client.get("/api/history").json()
            finally:
                _close(client)

        by_id = {r["run_id"]: r for r in body["runs"]}
        assert by_id[_RID_ADMIN]["user_email"] == "admin@test.local"

    def test_the_drawer_withholds_it_on_the_same_terms_as_the_table(
            self, registry):
        _seed_both_authorities(registry)
        with patch("src.backend.api.history_scope.is_validated_admin",
                   return_value=False):
            client = _client(registry, _USER1)
            try:
                detail = client.get(f"/api/history/{_RID_ADMIN}").json()
            finally:
                _close(client)

        assert detail["user_email"] is None
        assert detail["ran_as_platform_admin"] is True

    def test_the_drawer_gives_a_platform_admin_the_address(self, registry):
        _seed_both_authorities(registry)
        with patch("src.backend.api.history_scope.is_validated_admin",
                   return_value=True):
            client = _client(registry, _ADMIN)
            try:
                detail = client.get(f"/api/history/{_RID_ADMIN}").json()
            finally:
                _close(client)

        assert detail["user_email"] == "admin@test.local"


class TestTheEndpointsShipTheTestFields:
    def test_the_table_and_the_drawer_both_name_the_test_and_version(
            self, registry):
        registry.record_start(_RID_1, _USER1, "search for shoes", "passed",
                              robot_code=_CODE)
        with patch("src.backend.api.history_scope.is_validated_admin",
                   return_value=False):
            client = _client(registry, _USER1)
            try:
                listed = client.get("/api/history").json()["runs"][0]
                detail = client.get(f"/api/history/{_RID_1}").json()
            finally:
                _close(client)

        for body in (listed, detail):
            assert body["test_id"]
            assert body["test_name"] is None
            assert body["test_query"] == "search for shoes"
            assert body["test_version_n"] == 1

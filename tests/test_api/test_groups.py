"""Personal run groups: registry CRUD/assignment + /api/groups + history filter.

Groups are personal (user_id-scoped) folders for test_runs rows: one group per
run, cross-user access is always 404, deleting a group ungroups (never deletes)
its runs. Mirrors the fixture patterns of test_history_org_scope.py.

Referenced by: src/backend/core/run_registry.py,
               src/backend/api/groups_endpoints.py,
               src/backend/api/history_endpoints.py.
Depends on: tests/test_auth/conftest.py (auth_isolated_schema),
            tests/test_api/conftest.py (register_active).
"""

import uuid

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from src.backend.main import app
    return TestClient(app)


def _register(client, email):
    from tests.test_api.conftest import register_active
    return register_active(client, email)


# ---------------------------------------------------------------------------
# Unit: RunRegistry group CRUD on an isolated schema
# ---------------------------------------------------------------------------

class TestGroupRegistryCrud:
    """Direct RunRegistry unit tests for the run_groups table.

    RED gate before implementation: AttributeError ('RunRegistry' object has
    no attribute 'create_group')."""

    @pytest.fixture(scope="class")
    def reg(self):
        """Isolated RunRegistry on its own schema."""
        import psycopg
        from src.backend.core.config import settings
        from src.backend.core.run_registry import RunRegistry

        schema = "run_groups_test"
        admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
        admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        admin.execute(f"CREATE SCHEMA {schema}")
        sep = "&" if "?" in settings.DATABASE_URL else "?"
        dsn = settings.DATABASE_URL + f"{sep}options=-c%20search_path%3D{schema},public"
        r = RunRegistry(dsn=dsn)
        yield r
        r.close()
        admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        admin.close()

    @pytest.fixture(autouse=True)
    def _clean(self, reg):
        import psycopg
        from src.backend.core.config import settings
        admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
        admin.execute("TRUNCATE run_groups_test.test_runs")
        admin.execute("TRUNCATE run_groups_test.run_groups")
        admin.close()

    def test_create_and_list_groups(self, reg):
        g = reg.create_group("u1", "Checkout")
        assert g["name"] == "Checkout" and g["run_count"] == 0 and g["group_id"]
        listed = reg.list_groups("u1")
        assert [x["name"] for x in listed] == ["Checkout"]

    def test_list_groups_is_per_user_and_name_sorted(self, reg):
        reg.create_group("u1", "smoke")
        reg.create_group("u1", "Checkout")
        reg.create_group("u2", "Other")
        assert [x["name"] for x in reg.list_groups("u1")] == ["Checkout", "smoke"]
        assert [x["name"] for x in reg.list_groups("u2")] == ["Other"]

    def test_duplicate_name_case_insensitive(self, reg):
        from src.backend.core.run_registry import DuplicateGroupName
        reg.create_group("u1", "Smoke")
        with pytest.raises(DuplicateGroupName):
            reg.create_group("u1", "smoke")
        # Same name for a DIFFERENT user is fine.
        assert reg.create_group("u2", "smoke")["name"] == "smoke"

    def test_rename_group(self, reg):
        gid = reg.create_group("u1", "Old")["group_id"]
        assert reg.rename_group("u1", gid, "New") is True
        assert [x["name"] for x in reg.list_groups("u1")] == ["New"]

    def test_rename_to_existing_name_raises(self, reg):
        from src.backend.core.run_registry import DuplicateGroupName
        reg.create_group("u1", "Keep")
        gid = reg.create_group("u1", "Other")["group_id"]
        with pytest.raises(DuplicateGroupName):
            reg.rename_group("u1", gid, "keep")

    def test_rename_foreign_or_unknown_is_false(self, reg):
        gid = reg.create_group("u1", "Mine")["group_id"]
        assert reg.rename_group("u2", gid, "Stolen") is False
        assert reg.rename_group("u1", str(uuid.uuid4()), "Ghost") is False
        assert [x["name"] for x in reg.list_groups("u1")] == ["Mine"]

    def test_delete_group(self, reg):
        gid = reg.create_group("u1", "Gone")["group_id"]
        assert reg.delete_group("u1", gid) is True
        assert reg.list_groups("u1") == []

    def test_delete_foreign_or_unknown_is_false(self, reg):
        gid = reg.create_group("u1", "Mine")["group_id"]
        assert reg.delete_group("u2", gid) is False
        assert reg.delete_group("u1", str(uuid.uuid4())) is False
        assert [x["name"] for x in reg.list_groups("u1")] == ["Mine"]

    def test_count_ungrouped(self, reg):
        r1, r2, r3 = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        for rid, uid in ((r1, "u1"), (r2, "u1"), (r3, "u2")):
            reg.record_start(rid, {"user_id": uid, "email": f"{uid}@e.com"}, "q", "passed")
        assert reg.count_ungrouped("u1") == 2
        assert reg.count_ungrouped("u2") == 1


class TestGroupAssignmentAndFilter:
    """assign_runs atomicity + the group filter / group_name join in
    list_runs and get_run. Reuses TestGroupRegistryCrud's isolated-schema
    pattern via the same fixtures."""

    # Same isolated registry as TestGroupRegistryCrud (class-scoped copy —
    # tests may run per-class, so each class owns its schema lifecycle).
    @pytest.fixture(scope="class")
    def reg(self):
        import psycopg
        from src.backend.core.config import settings
        from src.backend.core.run_registry import RunRegistry

        schema = "run_groups_assign_test"
        admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
        admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        admin.execute(f"CREATE SCHEMA {schema}")
        sep = "&" if "?" in settings.DATABASE_URL else "?"
        dsn = settings.DATABASE_URL + f"{sep}options=-c%20search_path%3D{schema},public"
        r = RunRegistry(dsn=dsn)
        yield r
        r.close()
        admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        admin.close()

    @pytest.fixture(autouse=True)
    def _clean(self, reg):
        import psycopg
        from src.backend.core.config import settings
        admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
        admin.execute("TRUNCATE run_groups_assign_test.test_runs")
        admin.execute("TRUNCATE run_groups_assign_test.run_groups")
        admin.close()

    @staticmethod
    def _seed(reg, run_id, user_id, query="q", status="passed"):
        reg.record_start(
            run_id, {"user_id": user_id, "email": f"{user_id}@e.com"}, query, status
        )

    def test_assign_sets_group_and_filter_returns_rows(self, reg):
        r1, r2 = str(uuid.uuid4()), str(uuid.uuid4())
        self._seed(reg, r1, "u1")
        self._seed(reg, r2, "u1")
        gid = reg.create_group("u1", "Checkout")["group_id"]

        assert reg.assign_runs("u1", [r1, r2], gid) is True

        rows, total = reg.list_runs(user_id="u1", group=gid)
        assert total == 2
        assert all(r["group_id"] == gid and r["group_name"] == "Checkout" for r in rows)
        assert reg.list_groups("u1")[0]["run_count"] == 2
        assert reg.count_ungrouped("u1") == 0

    def test_unassign_with_none_and_ungrouped_filter(self, reg):
        r1 = str(uuid.uuid4())
        self._seed(reg, r1, "u1")
        gid = reg.create_group("u1", "G")["group_id"]
        reg.assign_runs("u1", [r1], gid)

        assert reg.assign_runs("u1", [r1], None) is True
        rows, total = reg.list_runs(user_id="u1", group="ungrouped")
        assert total == 1 and rows[0]["run_id"] == r1 and rows[0]["group_id"] is None

    def test_assign_foreign_run_rejected_atomically(self, reg):
        mine, theirs = str(uuid.uuid4()), str(uuid.uuid4())
        self._seed(reg, mine, "u1")
        self._seed(reg, theirs, "u2")
        gid = reg.create_group("u1", "G")["group_id"]

        assert reg.assign_runs("u1", [mine, theirs], gid) is False
        # Atomic: my run was NOT grouped either.
        _, total = reg.list_runs(user_id="u1", group=gid)
        assert total == 0

    def test_assign_into_foreign_group_rejected(self, reg):
        mine = str(uuid.uuid4())
        self._seed(reg, mine, "u1")
        foreign_gid = reg.create_group("u2", "Theirs")["group_id"]

        assert reg.assign_runs("u1", [mine], foreign_gid) is False
        _, total = reg.list_runs(user_id="u1", group="ungrouped")
        assert total == 1

    def test_group_filter_combines_with_status(self, reg):
        r1, r2 = str(uuid.uuid4()), str(uuid.uuid4())
        self._seed(reg, r1, "u1", status="passed")
        self._seed(reg, r2, "u1", status="failed")
        gid = reg.create_group("u1", "G")["group_id"]
        reg.assign_runs("u1", [r1, r2], gid)

        rows, total = reg.list_runs(user_id="u1", group=gid, status="failed")
        assert total == 1 and rows[0]["run_id"] == r2

    def test_delete_group_ungroups_members(self, reg):
        r1 = str(uuid.uuid4())
        self._seed(reg, r1, "u1")
        gid = reg.create_group("u1", "Doomed")["group_id"]
        reg.assign_runs("u1", [r1], gid)

        assert reg.delete_group("u1", gid) is True
        run = reg.get_run(r1)
        assert run is not None and run["group_id"] is None  # run survived, ungrouped

    def test_get_run_carries_group_name(self, reg):
        r1 = str(uuid.uuid4())
        self._seed(reg, r1, "u1")
        gid = reg.create_group("u1", "Named")["group_id"]
        reg.assign_runs("u1", [r1], gid)

        run = reg.get_run(r1)
        assert run["group_id"] == gid and run["group_name"] == "Named"


# ---------------------------------------------------------------------------
# Integration: /api/groups endpoints — real JWT tokens, personal scoping
# ---------------------------------------------------------------------------

def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def _seed_run_for(client, tok, query="seed query", status="passed") -> str:
    """Insert a test_runs row owned by the token's user; returns the run id."""
    from src.backend.auth.jwt_utils import decode_token
    from src.backend.core.run_registry import get_run_registry

    claims = decode_token(tok)
    rid = str(uuid.uuid4())
    get_run_registry().record_start(
        rid,
        {"user_id": claims["user_id"], "email": claims["email"],
         "org_id": claims["org_id"]},
        query,
        status,
    )
    return rid


def test_groups_crud_roundtrip(client):
    tok = _register(client, f"gc-{uuid.uuid4().hex[:8]}@e.com")

    r = client.post("/api/groups", json={"name": "  Checkout  "}, headers=_auth(tok))
    assert r.status_code == 201, r.text
    gid = r.json()["group_id"]
    assert r.json()["name"] == "Checkout"  # trimmed

    listed = client.get("/api/groups", headers=_auth(tok)).json()["groups"]
    assert [g["group_id"] for g in listed] == [gid]

    r = client.post("/api/groups", json={"name": "checkout"}, headers=_auth(tok))
    assert r.status_code == 409
    assert r.json()["detail"] == 'You already have a group named "checkout"'

    r = client.patch(f"/api/groups/{gid}", json={"name": "Payments"}, headers=_auth(tok))
    assert r.status_code == 200 and r.json()["name"] == "Payments"

    assert client.delete(f"/api/groups/{gid}", headers=_auth(tok)).status_code == 204
    assert client.get("/api/groups", headers=_auth(tok)).json()["groups"] == []


def test_rename_onto_existing_name_is_409(client):
    """Rename has its OWN duplicate guard — create's 409 does not cover it.
    The collision is case-insensitive and must leave both names untouched."""
    tok = _register(client, f"gn-{uuid.uuid4().hex[:8]}@e.com")
    client.post("/api/groups", json={"name": "Alpha"}, headers=_auth(tok))
    gid = client.post("/api/groups", json={"name": "Beta"},
                      headers=_auth(tok)).json()["group_id"]

    r = client.patch(f"/api/groups/{gid}", json={"name": "alpha"}, headers=_auth(tok))
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == 'You already have a group named "alpha"'
    listed = client.get("/api/groups", headers=_auth(tok)).json()["groups"]
    assert sorted(g["name"] for g in listed) == ["Alpha", "Beta"]


def test_group_name_validation(client):
    tok = _register(client, f"gv-{uuid.uuid4().hex[:8]}@e.com")
    assert client.post("/api/groups", json={"name": "   "}, headers=_auth(tok)).status_code == 400
    assert client.post("/api/groups", json={"name": "x" * 61}, headers=_auth(tok)).status_code == 400


def test_groups_are_invisible_and_immutable_across_users(client):
    tok_a = _register(client, f"ga-{uuid.uuid4().hex[:8]}@e.com")
    tok_b = _register(client, f"gb-{uuid.uuid4().hex[:8]}@e.com")

    gid = client.post("/api/groups", json={"name": "Private"},
                      headers=_auth(tok_a)).json()["group_id"]

    assert all(g["group_id"] != gid
               for g in client.get("/api/groups", headers=_auth(tok_b)).json()["groups"])
    assert client.patch(f"/api/groups/{gid}", json={"name": "Hax"},
                        headers=_auth(tok_b)).status_code == 404
    assert client.delete(f"/api/groups/{gid}", headers=_auth(tok_b)).status_code == 404
    # A's group untouched.
    mine = client.get("/api/groups", headers=_auth(tok_a)).json()["groups"]
    assert [g["name"] for g in mine] == ["Private"]


def test_assignments_endpoint(client):
    tok = _register(client, f"as-{uuid.uuid4().hex[:8]}@e.com")
    rid = _seed_run_for(client, tok)
    gid = client.post("/api/groups", json={"name": "Filed"},
                      headers=_auth(tok)).json()["group_id"]

    r = client.put("/api/groups/assignments",
                   json={"run_ids": [rid], "group_id": gid}, headers=_auth(tok))
    assert r.status_code == 200 and r.json()["assigned"] == 1

    body = client.get("/api/groups", headers=_auth(tok)).json()
    assert body["groups"][0]["run_count"] == 1
    assert body["ungrouped_count"] == 0  # the user's only run is filed

    # Unassign with group_id null.
    r = client.put("/api/groups/assignments",
                   json={"run_ids": [rid], "group_id": None}, headers=_auth(tok))
    assert r.status_code == 200
    body = client.get("/api/groups", headers=_auth(tok)).json()
    assert body["groups"][0]["run_count"] == 0 and body["ungrouped_count"] == 1


def test_assignments_reject_foreign_run_atomically(client):
    tok_a = _register(client, f"fa-{uuid.uuid4().hex[:8]}@e.com")
    tok_b = _register(client, f"fb-{uuid.uuid4().hex[:8]}@e.com")
    mine = _seed_run_for(client, tok_a)
    theirs = _seed_run_for(client, tok_b)
    gid = client.post("/api/groups", json={"name": "Mine"},
                      headers=_auth(tok_a)).json()["group_id"]

    r = client.put("/api/groups/assignments",
                   json={"run_ids": [mine, theirs], "group_id": gid},
                   headers=_auth(tok_a))
    assert r.status_code == 404
    # Atomic: A's own run was not grouped either.
    assert client.get("/api/groups", headers=_auth(tok_a)).json()["groups"][0]["run_count"] == 0


def test_assignments_validation(client):
    tok = _register(client, f"av-{uuid.uuid4().hex[:8]}@e.com")
    assert client.put("/api/groups/assignments",
                      json={"run_ids": [], "group_id": None},
                      headers=_auth(tok)).status_code == 400
    assert client.put("/api/groups/assignments",
                      json={"run_ids": ["not-a-uuid"], "group_id": None},
                      headers=_auth(tok)).status_code == 400


def test_anonymous_caller(client):
    """AUTH_ENFORCED is off in this suite (autouse fixture), so token-less
    require_user yields None: reads are empty, mutations are 401."""
    assert client.get("/api/groups").json() == {"groups": [], "ungrouped_count": 0}
    r = client.post("/api/groups", json={"name": "X"})
    assert r.status_code == 401
    assert r.json()["detail"] == "Sign-in required for groups"
    assert client.put("/api/groups/assignments",
                      json={"run_ids": [str(uuid.uuid4())], "group_id": None}).status_code == 401


# ---------------------------------------------------------------------------
# Integration: /api/history group filtering + group fields on rows
# ---------------------------------------------------------------------------

def test_history_group_filter_and_fields(client):
    tok = _register(client, f"hf-{uuid.uuid4().hex[:8]}@e.com")
    grouped = _seed_run_for(client, tok, query="in the group")
    loose = _seed_run_for(client, tok, query="left out")
    gid = client.post("/api/groups", json={"name": "Filter me"},
                      headers=_auth(tok)).json()["group_id"]
    client.put("/api/groups/assignments",
               json={"run_ids": [grouped], "group_id": gid}, headers=_auth(tok))

    page = client.get(f"/api/history?group={gid}", headers=_auth(tok)).json()
    assert page["total"] == 1
    assert page["runs"][0]["run_id"] == grouped
    assert page["runs"][0]["group_name"] == "Filter me"

    page = client.get("/api/history?group=ungrouped", headers=_auth(tok)).json()
    ids = [r["run_id"] for r in page["runs"]]
    assert loose in ids and grouped not in ids

    detail = client.get(f"/api/history/{grouped}", headers=_auth(tok)).json()
    assert detail["group_id"] == gid and detail["group_name"] == "Filter me"


def test_history_group_filter_combines_with_status(client):
    tok = _register(client, f"hc-{uuid.uuid4().hex[:8]}@e.com")
    passed = _seed_run_for(client, tok, status="passed")
    failed = _seed_run_for(client, tok, status="failed")
    gid = client.post("/api/groups", json={"name": "Mixed"},
                      headers=_auth(tok)).json()["group_id"]
    client.put("/api/groups/assignments",
               json={"run_ids": [passed, failed], "group_id": gid}, headers=_auth(tok))

    page = client.get(f"/api/history?group={gid}&status=failed",
                      headers=_auth(tok)).json()
    assert page["total"] == 1 and page["runs"][0]["run_id"] == failed


def test_history_invalid_group_param_is_400(client):
    tok = _register(client, f"hb-{uuid.uuid4().hex[:8]}@e.com")
    r = client.get("/api/history?group=not-a-uuid", headers=_auth(tok))
    assert r.status_code == 400

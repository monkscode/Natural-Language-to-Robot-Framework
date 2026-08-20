"""Run groups: registry CRUD/assignment + /api/groups + history filter.

Groups are ORG-scoped folders over test_runs rows (one group per run,
test_runs.group_id) carrying a per-folder visibility flag: an 'org' folder is
visible to everyone in the org, a 'private' folder only to its creator.
Authority: anyone in the org creates; the creator — or an org_admin, on an
'org' folder only — renames, flips visibility and deletes; a run may be filed
only into a folder the caller can see, and never into a private folder whose
owner does not own the run. Refusals are always 404 (False at the registry),
never 403, so a private folder's existence cannot be probed. Mirrors the
fixture patterns of test_history_org_scope.py.

Referenced by: src/backend/core/run_registry.py,
               src/backend/api/groups_endpoints.py,
               src/backend/api/history_endpoints.py.
Depends on: tests/test_auth/conftest.py (auth_isolated_schema),
            tests/test_api/conftest.py (register_active).
"""

import uuid

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]

ORG_A = "org-a"
ORG_B = "org-b"


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
    """Direct RunRegistry unit tests for the run_groups table."""

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
        g = reg.create_group(ORG_A, "u1", "Checkout")
        assert g["name"] == "Checkout" and g["run_count"] == 0 and g["group_id"]
        listed = reg.list_groups(ORG_A, "u1")
        assert [x["name"] for x in listed] == ["Checkout"]

    def test_list_groups_is_per_org_and_name_sorted(self, reg):
        reg.create_group(ORG_A, "u1", "smoke")
        reg.create_group(ORG_A, "u1", "Checkout")
        reg.create_group(ORG_B, "u2", "Other")
        assert [x["name"] for x in reg.list_groups(ORG_A, "u1")] == ["Checkout", "smoke"]
        assert [x["name"] for x in reg.list_groups(ORG_B, "u2")] == ["Other"]

    def test_duplicate_name_case_insensitive(self, reg):
        from src.backend.core.run_registry import DuplicateGroupName
        reg.create_group(ORG_A, "u1", "Smoke")
        with pytest.raises(DuplicateGroupName):
            reg.create_group(ORG_A, "u1", "smoke")
        # Same name by ANOTHER member of the SAME org also collides — org
        # folders are shared, so the name belongs to the org, not the user.
        with pytest.raises(DuplicateGroupName):
            reg.create_group(ORG_A, "u2", "SMOKE")
        # Same name in a DIFFERENT org is fine.
        assert reg.create_group(ORG_B, "u2", "smoke")["name"] == "smoke"

    def test_rename_group(self, reg):
        gid = reg.create_group(ORG_A, "u1", "Old")["group_id"]
        assert reg.rename_group(ORG_A, "u1", False, gid, name="New") is True
        assert [x["name"] for x in reg.list_groups(ORG_A, "u1")] == ["New"]

    def test_rename_to_existing_name_raises(self, reg):
        """Rename has its OWN duplicate guard — create's does not cover it."""
        from src.backend.core.run_registry import DuplicateGroupName
        reg.create_group(ORG_A, "u1", "Keep")
        gid = reg.create_group(ORG_A, "u1", "Other")["group_id"]
        with pytest.raises(DuplicateGroupName):
            reg.rename_group(ORG_A, "u1", False, gid, name="keep")
        assert sorted(x["name"] for x in reg.list_groups(ORG_A, "u1")) == ["Keep", "Other"]

    def test_rename_foreign_or_unknown_is_false(self, reg):
        gid = reg.create_group(ORG_A, "u1", "Mine")["group_id"]
        assert reg.rename_group(ORG_B, "u2", True, gid, name="Stolen") is False
        assert reg.rename_group(ORG_A, "u1", False, str(uuid.uuid4()), name="Ghost") is False
        assert [x["name"] for x in reg.list_groups(ORG_A, "u1")] == ["Mine"]

    def test_delete_group(self, reg):
        gid = reg.create_group(ORG_A, "u1", "Gone")["group_id"]
        assert reg.delete_group(ORG_A, "u1", False, gid) is True
        assert reg.list_groups(ORG_A, "u1") == []

    def test_delete_foreign_or_unknown_is_false(self, reg):
        gid = reg.create_group(ORG_A, "u1", "Mine")["group_id"]
        assert reg.delete_group(ORG_B, "u2", True, gid) is False
        assert reg.delete_group(ORG_A, "u1", False, str(uuid.uuid4())) is False
        assert [x["name"] for x in reg.list_groups(ORG_A, "u1")] == ["Mine"]

    def test_count_ungrouped(self, reg):
        r1, r2, r3 = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        for rid, uid in ((r1, "u1"), (r2, "u1"), (r3, "u2")):
            reg.record_start(rid, {"user_id": uid, "email": f"{uid}@e.com"}, "q", "passed")
        assert reg.count_ungrouped("u1") == 2
        assert reg.count_ungrouped("u2") == 1


class TestGroupAuthorityMatrix:
    """The authority matrix of the org-keyed model, at the registry.

    Visibility is not on the wire yet (T3), so a private folder cannot even
    be created over HTTP — the matrix has to be asserted here. A refusal is
    False, which the endpoints turn into 404 rather than 403."""

    ORG = "org-acme"
    ADMIN = "u-admin"
    MEMBER = "u-member"
    OUTSIDER = "u-outsider"

    @pytest.fixture(scope="class")
    def reg(self):
        import psycopg
        from src.backend.core.config import settings
        from src.backend.core.run_registry import RunRegistry

        schema = "run_groups_authz_test"
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
        admin.execute("TRUNCATE run_groups_authz_test.test_runs")
        admin.execute("TRUNCATE run_groups_authz_test.run_groups")
        admin.close()

    @staticmethod
    def _seed(reg, run_id, user_id, org_id, status="passed"):
        reg.record_start(
            run_id,
            {"user_id": user_id, "org_id": org_id, "email": f"{user_id}@e.com"},
            "q", status,
        )

    # 1 -------------------------------------------------------------------
    def test_org_folder_is_shared_private_folder_is_not(self, reg):
        reg.create_group(self.ORG, self.ADMIN, "Shared")
        reg.create_group(self.ORG, self.ADMIN, "Secret", visibility="private")

        assert [g["name"] for g in reg.list_groups(self.ORG, self.MEMBER)] == ["Shared"]
        assert sorted(g["name"] for g in reg.list_groups(self.ORG, self.ADMIN)) == [
            "Secret", "Shared"]

    # 2 -------------------------------------------------------------------
    def test_creator_owns_their_private_folder_org_admin_cannot_touch_it(self, reg):
        gid = reg.create_group(
            self.ORG, self.MEMBER, "Mine", visibility="private")["group_id"]

        assert reg.rename_group(self.ORG, self.MEMBER, False, gid, name="Mine 2") is True
        # The org_admin cannot even see it, so acting on it must look like a
        # missing folder (404), never a refusal (403).
        assert reg.rename_group(self.ORG, self.ADMIN, True, gid, name="Hax") is False
        assert reg.delete_group(self.ORG, self.ADMIN, True, gid) is False
        assert [g["name"] for g in reg.list_groups(self.ORG, self.MEMBER)] == ["Mine 2"]
        assert reg.delete_group(self.ORG, self.MEMBER, False, gid) is True

    # 3 -------------------------------------------------------------------
    def test_org_admin_may_mutate_an_org_folder_they_did_not_create(self, reg):
        gid = reg.create_group(self.ORG, self.MEMBER, "Team")["group_id"]
        assert reg.rename_group(self.ORG, self.ADMIN, True, gid, name="Team 2") is True
        assert reg.delete_group(self.ORG, self.ADMIN, True, gid) is True

    # 4 -------------------------------------------------------------------
    def test_member_may_not_mutate_an_org_folder_they_did_not_create(self, reg):
        gid = reg.create_group(self.ORG, self.ADMIN, "Team")["group_id"]
        assert reg.rename_group(self.ORG, self.MEMBER, False, gid, name="Hax") is False
        assert reg.delete_group(self.ORG, self.MEMBER, False, gid) is False
        assert [g["name"] for g in reg.list_groups(self.ORG, self.MEMBER)] == ["Team"]

    # 5 -------------------------------------------------------------------
    def test_member_files_own_run_into_org_folder_but_not_a_private_one(self, reg):
        rid = str(uuid.uuid4())
        self._seed(reg, rid, self.MEMBER, self.ORG)
        org_gid = reg.create_group(self.ORG, self.ADMIN, "Team")["group_id"]
        priv_gid = reg.create_group(
            self.ORG, self.ADMIN, "Secret", visibility="private")["group_id"]

        assert reg.assign_runs(self.ORG, self.MEMBER, False, [rid], org_gid) is True
        assert reg.assign_runs(self.ORG, self.MEMBER, False, [rid], priv_gid) is False
        assert reg.get_run(rid)["group_id"] == org_gid

    # 6 -------------------------------------------------------------------
    def test_org_admin_files_a_members_run_into_an_org_folder(self, reg):
        rid = str(uuid.uuid4())
        self._seed(reg, rid, self.MEMBER, self.ORG)
        gid = reg.create_group(self.ORG, self.ADMIN, "Team")["group_id"]

        assert reg.assign_runs(self.ORG, self.ADMIN, True, [rid], gid) is True
        assert reg.get_run(rid)["group_id"] == gid

    # 7 -------------------------------------------------------------------
    def test_org_admin_cannot_file_a_members_run_into_their_own_private_folder(self, reg):
        rid = str(uuid.uuid4())
        self._seed(reg, rid, self.MEMBER, self.ORG)
        gid = reg.create_group(
            self.ORG, self.ADMIN, "Secret", visibility="private")["group_id"]

        # A run must never land in a folder its own owner cannot see.
        assert reg.assign_runs(self.ORG, self.ADMIN, True, [rid], gid) is False
        assert reg.get_run(rid)["group_id"] is None

    def test_assign_is_all_or_nothing_for_member_and_admin(self, reg):
        """A batch carrying one run the caller may not file leaves the
        caller's OWN runs ungrouped too — now a per-row authority decision,
        which is exactly when partial writes appear."""
        mine, theirs = str(uuid.uuid4()), str(uuid.uuid4())
        self._seed(reg, mine, self.MEMBER, self.ORG)
        self._seed(reg, theirs, self.OUTSIDER, "org-other")
        gid = reg.create_group(self.ORG, self.MEMBER, "Team")["group_id"]

        assert reg.assign_runs(self.ORG, self.MEMBER, False, [mine, theirs], gid) is False
        assert reg.get_run(mine)["group_id"] is None
        # Same for the org_admin: the outsider's run is not in their org.
        assert reg.assign_runs(self.ORG, self.ADMIN, True, [mine, theirs], gid) is False
        assert reg.get_run(mine)["group_id"] is None

    # 8 -------------------------------------------------------------------
    def test_name_collisions_are_scoped_per_visibility(self, reg):
        from src.backend.core.run_registry import DuplicateGroupName
        reg.create_group(self.ORG, self.ADMIN, "checkout")
        # Two ORG folders of the same name in one org — 409.
        with pytest.raises(DuplicateGroupName):
            reg.create_group(self.ORG, self.MEMBER, "Checkout")
        # A private folder of the same name coexists with the org one: a
        # single (org_id, name) index would leak that a private one exists.
        reg.create_group(self.ORG, self.MEMBER, "checkout", visibility="private")
        # Two private folders of the same name by the SAME user — 409.
        with pytest.raises(DuplicateGroupName):
            reg.create_group(self.ORG, self.MEMBER, "CHECKOUT", visibility="private")
        # Private folders of the same name by DIFFERENT users — both fine.
        reg.create_group(self.ORG, self.ADMIN, "checkout", visibility="private")

    # 9 -------------------------------------------------------------------
    def test_flip_to_private_is_refused_while_other_members_runs_are_inside(self, reg):
        from src.backend.core.run_registry import GroupVisibilityConflict
        mine, theirs = str(uuid.uuid4()), str(uuid.uuid4())
        self._seed(reg, mine, self.ADMIN, self.ORG)
        self._seed(reg, theirs, self.MEMBER, self.ORG)
        gid = reg.create_group(self.ORG, self.ADMIN, "Team")["group_id"]
        assert reg.assign_runs(self.ORG, self.ADMIN, True, [mine, theirs], gid) is True

        with pytest.raises(GroupVisibilityConflict) as exc:
            reg.rename_group(self.ORG, self.ADMIN, True, gid, visibility="private")
        assert "1 run" in str(exc.value)          # names the count...
        assert self.MEMBER not in str(exc.value)  # ...never the owners
        assert reg.get_run(theirs)["group_id"] == gid  # nothing moved

        # Once the other member's run is out, the flip goes through — the
        # creator's own run inside the folder is no obstacle.
        assert reg.assign_runs(self.ORG, self.ADMIN, True, [theirs], None) is True
        assert reg.rename_group(
            self.ORG, self.ADMIN, True, gid, visibility="private") is True
        # private -> org only ever widens visibility: always allowed.
        assert reg.rename_group(
            self.ORG, self.ADMIN, True, gid, visibility="org") is True


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
    def _seed(reg, run_id, user_id, org_id=ORG_A, query="q", status="passed"):
        reg.record_start(
            run_id,
            {"user_id": user_id, "org_id": org_id, "email": f"{user_id}@e.com"},
            query, status,
        )

    def test_assign_sets_group_and_filter_returns_rows(self, reg):
        r1, r2 = str(uuid.uuid4()), str(uuid.uuid4())
        self._seed(reg, r1, "u1")
        self._seed(reg, r2, "u1")
        gid = reg.create_group(ORG_A, "u1", "Checkout")["group_id"]

        assert reg.assign_runs(ORG_A, "u1", False, [r1, r2], gid) is True

        rows, total = reg.list_runs(user_id="u1", group=gid)
        assert total == 2
        assert all(r["group_id"] == gid and r["group_name"] == "Checkout" for r in rows)
        assert reg.list_groups(ORG_A, "u1")[0]["run_count"] == 2
        assert reg.count_ungrouped("u1") == 0

    def test_unassign_with_none_and_ungrouped_filter(self, reg):
        r1 = str(uuid.uuid4())
        self._seed(reg, r1, "u1")
        gid = reg.create_group(ORG_A, "u1", "G")["group_id"]
        reg.assign_runs(ORG_A, "u1", False, [r1], gid)

        assert reg.assign_runs(ORG_A, "u1", False, [r1], None) is True
        rows, total = reg.list_runs(user_id="u1", group="ungrouped")
        assert total == 1 and rows[0]["run_id"] == r1 and rows[0]["group_id"] is None

    def test_assign_foreign_run_rejected_atomically(self, reg):
        mine, theirs = str(uuid.uuid4()), str(uuid.uuid4())
        self._seed(reg, mine, "u1")
        self._seed(reg, theirs, "u2", org_id=ORG_B)
        gid = reg.create_group(ORG_A, "u1", "G")["group_id"]

        assert reg.assign_runs(ORG_A, "u1", False, [mine, theirs], gid) is False
        # Atomic: my run was NOT grouped either.
        _, total = reg.list_runs(user_id="u1", group=gid)
        assert total == 0

    def test_assign_into_foreign_group_rejected(self, reg):
        mine = str(uuid.uuid4())
        self._seed(reg, mine, "u1")
        foreign_gid = reg.create_group(ORG_B, "u2", "Theirs")["group_id"]

        assert reg.assign_runs(ORG_A, "u1", False, [mine], foreign_gid) is False
        _, total = reg.list_runs(user_id="u1", group="ungrouped")
        assert total == 1

    def test_group_filter_combines_with_status(self, reg):
        r1, r2 = str(uuid.uuid4()), str(uuid.uuid4())
        self._seed(reg, r1, "u1", status="passed")
        self._seed(reg, r2, "u1", status="failed")
        gid = reg.create_group(ORG_A, "u1", "G")["group_id"]
        reg.assign_runs(ORG_A, "u1", False, [r1, r2], gid)

        rows, total = reg.list_runs(user_id="u1", group=gid, status="failed")
        assert total == 1 and rows[0]["run_id"] == r2

    def test_delete_group_ungroups_members(self, reg):
        r1 = str(uuid.uuid4())
        self._seed(reg, r1, "u1")
        gid = reg.create_group(ORG_A, "u1", "Doomed")["group_id"]
        reg.assign_runs(ORG_A, "u1", False, [r1], gid)

        assert reg.delete_group(ORG_A, "u1", False, gid) is True
        run = reg.get_run(r1)
        assert run is not None and run["group_id"] is None  # run survived, ungrouped

    def test_get_run_carries_group_name(self, reg):
        r1 = str(uuid.uuid4())
        self._seed(reg, r1, "u1")
        gid = reg.create_group(ORG_A, "u1", "Named")["group_id"]
        reg.assign_runs(ORG_A, "u1", False, [r1], gid)

        run = reg.get_run(r1)
        assert run["group_id"] == gid and run["group_name"] == "Named"


# ---------------------------------------------------------------------------
# Integration: /api/groups endpoints — real JWT tokens, org scoping
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


def _login(client, email) -> str:
    r = client.post("/auth/login", json={"email": email, "password": "S3cretpw!"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _team_of_two(client):
    """A team org with A as org_admin and B as org_member.

    JWT claims are minted at login, so BOTH users have to log in AGAIN after
    the membership change — the registration tokens still carry the personal
    org they were provisioned into. Returns (tok_admin, tok_member, org_id)."""
    from src.backend.auth.jwt_utils import decode_token
    from src.backend.auth.org_repository import OrgRepository

    email_a = f"ta-{uuid.uuid4().hex[:8]}@e.com"
    email_b = f"tb-{uuid.uuid4().hex[:8]}@e.com"
    uid_a = decode_token(_register(client, email_a))["user_id"]
    uid_b = decode_token(_register(client, email_b))["user_id"]
    org_id = OrgRepository().create_team_org(f"Acme {uuid.uuid4().hex[:6]}", uid_a)
    OrgRepository().add_member(org_id, uid_b)
    return _login(client, email_a), _login(client, email_b), org_id


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


def test_groups_are_invisible_and_immutable_across_orgs(client):
    """Two separately registered users sit in their own personal orgs, so
    neither sees the other's folders whatever their visibility."""
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


def test_org_folder_is_shared_across_the_org(client):
    """The point of the re-key: a folder one member creates is the org's."""
    tok_a, tok_b, _org_id = _team_of_two(client)
    gid = client.post("/api/groups", json={"name": "Team folder"},
                      headers=_auth(tok_a)).json()["group_id"]

    listed = client.get("/api/groups", headers=_auth(tok_b)).json()["groups"]
    assert [g["group_id"] for g in listed] == [gid]

    rid = _seed_run_for(client, tok_b)
    r = client.put("/api/groups/assignments",
                   json={"run_ids": [rid], "group_id": gid}, headers=_auth(tok_b))
    assert r.status_code == 200, r.text


def test_org_admin_can_file_a_members_run(client):
    """The behaviour the per-user model 404'd on."""
    tok_a, tok_b, _org_id = _team_of_two(client)
    rid = _seed_run_for(client, tok_b)  # owned by the member
    gid = client.post("/api/groups", json={"name": "Admin filed"},
                      headers=_auth(tok_a)).json()["group_id"]

    r = client.put("/api/groups/assignments",
                   json={"run_ids": [rid], "group_id": gid}, headers=_auth(tok_a))
    assert r.status_code == 200, r.text
    detail = client.get(f"/api/history/{rid}", headers=_auth(tok_b)).json()
    assert detail["group_id"] == gid and detail["group_name"] == "Admin filed"


def test_member_cannot_mutate_an_org_folder_they_did_not_create(client):
    tok_a, tok_b, _org_id = _team_of_two(client)
    gid = client.post("/api/groups", json={"name": "Admins folder"},
                      headers=_auth(tok_a)).json()["group_id"]

    assert client.patch(f"/api/groups/{gid}", json={"name": "Hax"},
                        headers=_auth(tok_b)).status_code == 404
    assert client.delete(f"/api/groups/{gid}", headers=_auth(tok_b)).status_code == 404


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

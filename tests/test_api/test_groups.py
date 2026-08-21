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
        # ONE statement, not two: test_runs' foreign key makes
        # run_groups untruncatable on its own.
        admin.execute("TRUNCATE run_groups_test.test_runs, run_groups_test.run_groups")
        admin.close()

    def test_registry_construction_is_idempotent(self, reg):
        """_SCHEMA_DDL runs on EVERY RunRegistry() construction, not once at
        startup, so any statement in it that is not self-guarding makes the
        app unbootable the second time it starts against the same schema."""
        from src.backend.core.run_registry import RunRegistry
        second = RunRegistry(dsn=reg.dsn)
        second.close()

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
        # ONE statement, not two: test_runs' foreign key makes
        # run_groups untruncatable on its own.
        admin.execute("TRUNCATE run_groups_authz_test.test_runs, run_groups_authz_test.run_groups")
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


class TestReadPathVisibility:
    """The read half: the visibility-filtered LEFT JOIN.

    Every folder question is answered by ONE join —
        LEFT JOIN run_groups g ON g.group_id = t.group_id
                              AND g.org_id = <caller org>
                              AND (g.visibility = 'org' OR g.created_by = <me>)
    so a row's folder tag is g.name, its folder id is g.group_id (never
    t.group_id), and "Ungrouped" means "in no folder I can see" rather than
    "t.group_id IS NULL". A caller with no org scope (platform admin, or
    token-less with AUTH_ENFORCED off) gets the join UNFILTERED: binding a
    NULL org would evaluate the condition to NULL for every row and collapse
    the whole page into Ungrouped.

    The identity trap: an org_admin's FILTER user_id is None (they see the
    whole org) while their identity is not, so the private-folder half of the
    predicate is always caller_user_id, never the scoping user_id."""

    ORG = "org-acme"
    ADMIN = "u-admin"
    MEMBER = "u-member"

    @pytest.fixture(scope="class")
    def reg(self):
        import psycopg
        from src.backend.core.config import settings
        from src.backend.core.run_registry import RunRegistry

        schema = "run_groups_read_test"
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
        # ONE statement, not two: test_runs' foreign key makes
        # run_groups untruncatable on its own.
        admin.execute("TRUNCATE run_groups_read_test.test_runs, run_groups_read_test.run_groups")
        admin.close()

    @staticmethod
    def _seed(reg, run_id, user_id, org_id, status="passed"):
        reg.record_start(
            run_id,
            {"user_id": user_id, "org_id": org_id, "email": f"{user_id}@e.com"},
            "q", status,
        )

    # 10 ------------------------------------------------------------------
    def test_run_in_an_invisible_private_folder_reads_as_ungrouped(self, reg):
        """Both halves, or the run disappears from every view: the org_admin
        must see NO folder tag on it AND must find it under Ungrouped."""
        rid = str(uuid.uuid4())
        self._seed(reg, rid, self.MEMBER, self.ORG)
        gid = reg.create_group(
            self.ORG, self.MEMBER, "Mine", visibility="private")["group_id"]
        assert reg.assign_runs(self.ORG, self.MEMBER, False, [rid], gid) is True

        # The org_admin's scope: whole org (filter user_id None), identity ADMIN.
        rows, total = reg.list_runs(
            user_id=None, org_id=self.ORG, caller_user_id=self.ADMIN)
        assert total == 1
        assert rows[0]["group_id"] is None and rows[0]["group_name"] is None
        seen = reg.get_run(rid, org_id=self.ORG, caller_user_id=self.ADMIN)
        assert seen["group_id"] is None and seen["group_name"] is None

        ung, ung_total = reg.list_runs(
            user_id=None, org_id=self.ORG, group="ungrouped",
            caller_user_id=self.ADMIN)
        assert ung_total == 1 and ung[0]["run_id"] == rid

        # The creator still sees it filed, and NOT under their Ungrouped.
        mine, _ = reg.list_runs(
            user_id=self.MEMBER, org_id=self.ORG, caller_user_id=self.MEMBER)
        assert mine[0]["group_id"] == gid and mine[0]["group_name"] == "Mine"
        assert reg.list_runs(
            user_id=self.MEMBER, org_id=self.ORG, group="ungrouped",
            caller_user_id=self.MEMBER)[1] == 0

    # 11 ------------------------------------------------------------------
    def test_org_folder_name_shows_to_every_member(self, reg):
        rid = str(uuid.uuid4())
        self._seed(reg, rid, self.MEMBER, self.ORG)
        gid = reg.create_group(self.ORG, self.ADMIN, "Team")["group_id"]
        assert reg.assign_runs(self.ORG, self.MEMBER, False, [rid], gid) is True

        for me, scope_user in ((self.MEMBER, self.MEMBER), (self.ADMIN, None)):
            rows, total = reg.list_runs(
                user_id=scope_user, org_id=self.ORG, caller_user_id=me)
            assert total == 1
            assert rows[0]["group_id"] == gid and rows[0]["group_name"] == "Team"
            run = reg.get_run(rid, org_id=self.ORG, caller_user_id=me)
            assert run["group_id"] == gid and run["group_name"] == "Team"

    # 12 ------------------------------------------------------------------
    def test_count_ungrouped_agrees_with_the_ungrouped_filter(self, reg):
        """The chip must agree with the table for all three scope shapes."""
        # A solo user is org_admin of their own personal org (fact 2), so
        # their scope is (org=personal, user=None).
        solo = [str(uuid.uuid4()) for _ in range(3)]
        for r in solo:
            self._seed(reg, r, "u-solo", "org-solo")
        solo_gid = reg.create_group(
            "org-solo", "u-solo", "Solo", visibility="private")["group_id"]
        assert reg.assign_runs("org-solo", "u-solo", True, solo[:1], solo_gid) is True

        # The team: the admin's run in an org folder, two member runs of which
        # one sits in the member's OWN private folder (invisible to the admin).
        a1, m1, m2 = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        self._seed(reg, a1, self.ADMIN, self.ORG)
        self._seed(reg, m1, self.MEMBER, self.ORG)
        self._seed(reg, m2, self.MEMBER, self.ORG)
        team_gid = reg.create_group(self.ORG, self.ADMIN, "Team")["group_id"]
        priv_gid = reg.create_group(
            self.ORG, self.MEMBER, "Mine", visibility="private")["group_id"]
        assert reg.assign_runs(self.ORG, self.ADMIN, True, [a1], team_gid) is True
        assert reg.assign_runs(self.ORG, self.MEMBER, False, [m1], priv_gid) is True

        cases = [
            ((None, "org-solo", "u-solo"), 2),           # solo: 3 runs, 1 filed
            ((self.MEMBER, self.ORG, self.MEMBER), 1),   # member: m1 filed, m2 not
            ((None, self.ORG, self.ADMIN), 2),           # admin: m1's folder invisible
        ]
        for (scope_user, scope_org, me), expected in cases:
            _, total = reg.list_runs(
                user_id=scope_user, org_id=scope_org, group="ungrouped",
                caller_user_id=me)
            assert total == expected, (scope_user, scope_org, me)
            assert reg.count_ungrouped(scope_user, scope_org, me) == total

    # 13 ------------------------------------------------------------------
    def test_folder_run_count_agrees_with_the_folder_filter(self, reg):
        a1, m1 = str(uuid.uuid4()), str(uuid.uuid4())
        self._seed(reg, a1, self.ADMIN, self.ORG)
        self._seed(reg, m1, self.MEMBER, self.ORG)
        gid = reg.create_group(self.ORG, self.ADMIN, "Team")["group_id"]
        assert reg.assign_runs(self.ORG, self.ADMIN, True, [a1, m1], gid) is True

        # The member sees only their own run in it...
        listed = reg.list_groups(self.ORG, self.MEMBER, scope_user_id=self.MEMBER)
        _, total = reg.list_runs(
            user_id=self.MEMBER, org_id=self.ORG, group=gid,
            caller_user_id=self.MEMBER)
        assert [g["run_count"] for g in listed] == [total] == [1]

        # ...the org_admin sees both.
        listed = reg.list_groups(self.ORG, self.ADMIN, scope_user_id=None)
        _, total = reg.list_runs(
            user_id=None, org_id=self.ORG, group=gid, caller_user_id=self.ADMIN)
        assert [g["run_count"] for g in listed] == [total] == [2]

    # 14 ------------------------------------------------------------------
    def test_filtering_by_an_invisible_folder_returns_nothing(self, reg):
        rid = str(uuid.uuid4())
        self._seed(reg, rid, self.MEMBER, self.ORG)
        gid = reg.create_group(
            self.ORG, self.MEMBER, "Mine", visibility="private")["group_id"]
        assert reg.assign_runs(self.ORG, self.MEMBER, False, [rid], gid) is True

        rows, total = reg.list_runs(
            user_id=None, org_id=self.ORG, group=gid, caller_user_id=self.ADMIN)
        assert rows == [] and total == 0

    # 15 ------------------------------------------------------------------
    def test_unscoped_caller_still_sees_every_folder(self, reg):
        """org_id=None is the platform admin / token-less branch: no
        visibility filter at all. Bind None into the join instead and every
        folder vanishes and the page collapses into Ungrouped."""
        rid = str(uuid.uuid4())
        self._seed(reg, rid, self.MEMBER, self.ORG)
        gid = reg.create_group(
            self.ORG, self.MEMBER, "Mine", visibility="private")["group_id"]
        assert reg.assign_runs(self.ORG, self.MEMBER, False, [rid], gid) is True

        rows, total = reg.list_runs(user_id=None, org_id=None, caller_user_id=None)
        assert total == 1
        assert rows[0]["group_id"] == gid and rows[0]["group_name"] == "Mine"

        assert reg.list_runs(user_id=None, org_id=None, group="ungrouped",
                             caller_user_id=None)[1] == 0
        assert reg.count_ungrouped(None, None, None) == 0
        assert reg.list_runs(user_id=None, org_id=None, group=gid,
                             caller_user_id=None)[1] == 1
        run = reg.get_run(rid)
        assert run["group_id"] == gid and run["group_name"] == "Mine"

    def test_join_parameters_bind_before_the_where_clause(self, reg):
        """The join now carries its OWN placeholders, and they appear in the
        SQL text BEFORE the WHERE clause's. psycopg binds %s strictly by
        position, so mis-ordering them filters on the wrong values SILENTLY
        rather than raising. Every filter at once is what catches it."""
        mine, other = str(uuid.uuid4()), str(uuid.uuid4())
        self._seed(reg, mine, self.MEMBER, self.ORG, status="failed")
        self._seed(reg, other, self.ADMIN, self.ORG, status="failed")
        gid = reg.create_group(self.ORG, self.ADMIN, "Team")["group_id"]
        assert reg.assign_runs(self.ORG, self.ADMIN, True, [mine, other], gid) is True

        rows, total = reg.list_runs(
            user_id=self.MEMBER, org_id=self.ORG, status="failed", q="q",
            group=gid, caller_user_id=self.MEMBER, limit=10, offset=0)
        assert total == 1
        assert rows[0]["run_id"] == mine and rows[0]["group_name"] == "Team"


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
        # ONE statement, not two: test_runs' foreign key makes
        # run_groups untruncatable on its own.
        admin.execute("TRUNCATE run_groups_assign_test.test_runs, run_groups_assign_test.run_groups")
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

    def test_race_with_delete_cannot_orphan_a_run(self, reg):
        """Replay the assign/delete race on two connections at READ
        COMMITTED: T1's authority check passes, T2 deletes the folder and
        commits, T1 then writes the run into it. Whatever T1's write does,
        no test_runs row may be left pointing at a folder that is gone."""
        import psycopg
        r1 = str(uuid.uuid4())
        self._seed(reg, r1, "u1")
        gid = reg.create_group(ORG_A, "u1", "Racy")["group_id"]

        t1 = psycopg.connect(reg.dsn)
        t2 = psycopg.connect(reg.dsn, autocommit=True)
        refused = False
        try:
            # T1 sees the folder, and has not written yet.
            assert t1.execute(
                "SELECT 1 FROM run_groups WHERE group_id = %s", (gid,)
            ).fetchone() is not None
            # T2 deletes it and commits.
            t2.execute("DELETE FROM run_groups WHERE group_id = %s", (gid,))
            # T1 files the run into the folder it saw a moment ago. Without
            # the constraint this UPDATE matches exactly one row and commits
            # the dead id, so the row count is what makes the test bite: a
            # seed that stopped writing r1 would match zero rows and the
            # dangling count would be 0 for the wrong reason.
            matched = None
            try:
                matched = t1.execute(
                    "UPDATE test_runs SET group_id = %s WHERE run_id = %s",
                    (gid, r1),
                ).rowcount
                t1.commit()
            except psycopg.errors.ForeignKeyViolation:
                refused = True
                t1.rollback()
            dangling = t2.execute(
                "SELECT COUNT(*) FROM test_runs t WHERE t.group_id IS NOT NULL"
                " AND NOT EXISTS (SELECT 1 FROM run_groups g"
                " WHERE g.group_id = t.group_id)"
            ).fetchone()[0]
            # The run is still there, and still targetable by that WHERE.
            targetable = t2.execute(
                "SELECT COUNT(*) FROM test_runs WHERE run_id = %s", (r1,)
            ).fetchone()[0]
        finally:
            t1.close()
            t2.close()
        assert targetable == 1, "the seeded run vanished — the race was never run"
        assert refused, f"the losing UPDATE was allowed (it matched {matched} rows)"
        assert dangling == 0

    def test_flip_to_private_cannot_race_a_concurrent_assign(self, reg):
        """The org->private flip and a concurrent file-a-run must serialize.

        Reproduced 2026-08-21 before the row lock: rename_group counted 0
        foreign-owned runs, another member's run was filed in by a second
        transaction, both committed, and the result was a run inside a
        private folder its own owner cannot see — the invariant three
        separate paths exist to uphold. The read path hides the breach (the
        run just reads as Ungrouped), so nothing surfaces it.

        _visible_group now takes the folder row FOR UPDATE, so the filer
        waits for the flip to commit, re-reads 'private', and refuses.
        """
        import threading
        import psycopg

        gid = reg.create_group(ORG_A, "u1", "Shared")["group_id"]
        rid = str(uuid.uuid4())
        self._seed(reg, rid, "u2")

        flip = psycopg.connect(reg.dsn)
        result: list = []
        t = None
        try:
            # Stand in for rename_group's transaction: hold the folder row
            # exactly as _visible_group now does, BEFORE counting foreign runs.
            flip.execute(
                "SELECT created_by FROM run_groups WHERE group_id = %s FOR UPDATE",
                (gid,),
            )
            t = threading.Thread(
                target=lambda: result.append(
                    reg.assign_runs(ORG_A, "u2", False, [rid], gid)))
            t.start()
            t.join(timeout=2.0)
            assert t.is_alive(), (
                "assign_runs did not wait for the folder row lock — without it "
                "the flip's foreign-run count is already stale when it commits")
            flip.execute(
                "UPDATE run_groups SET visibility = 'private' WHERE group_id = %s",
                (gid,))
            flip.commit()
        finally:
            flip.close()
            if t is not None:
                t.join(timeout=10.0)

        assert result == [False], (
            "the filer must re-read the committed 'private' folder and refuse "
            f"u2's run, got {result}")
        with reg._pool.connection() as conn:
            breached = conn.execute(
                "SELECT COUNT(*) AS n FROM test_runs t "
                "JOIN run_groups g ON g.group_id = t.group_id "
                "WHERE g.visibility = 'private' AND g.created_by <> t.user_id"
            ).fetchone()["n"]
        assert breached == 0

    def test_assign_runs_returns_false_when_the_folder_vanishes(self, reg):
        """assign_runs loses the race: the folder clears the authority check,
        then is gone by the time the UPDATE runs. It must fail closed —
        False (which the endpoint turns into a 404), no exception, nothing
        written — and leave the pooled connection usable."""
        from unittest.mock import patch
        r1 = str(uuid.uuid4())
        self._seed(reg, r1, "u1")
        ghost = str(uuid.uuid4())
        vanished = {"group_id": ghost, "visibility": "org", "created_by": "u1"}

        with patch.object(reg, "_visible_group", return_value=vanished):
            assert reg.assign_runs(ORG_A, "u1", False, [r1], ghost) is False

        run = reg.get_run(r1)
        # get_run swallows its own exceptions and returns None, so check the
        # row came back before subscripting: a connection poisoned by the
        # caught violation shows up here as a legible assertion, not a
        # TypeError on NoneType.
        assert run is not None, "the pooled connection did not survive the rollback"
        assert run["group_id"] is None

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
    assert r.json()["detail"] == 'A group named "checkout" already exists'

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
    assert r.json()["detail"] == 'A group named "alpha" already exists'
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


def test_drawer_hides_a_folder_the_caller_cannot_see(client):
    """PRESERVE item 5's negative: a run inside a folder the caller cannot see
    reports null for BOTH group fields — never the folder's name, and never a
    dangling id.

    The private folder is created through the registry rather than the
    endpoint, so this stays a read-path test; the endpoint reads the very
    same rows."""
    from src.backend.auth.jwt_utils import decode_token
    from src.backend.core.run_registry import get_run_registry

    tok_a, tok_b, org_id = _team_of_two(client)
    uid_b = decode_token(tok_b)["user_id"]
    rid = _seed_run_for(client, tok_b)
    reg = get_run_registry()
    name = f"Bs secret {uuid.uuid4().hex[:6]}"
    gid = reg.create_group(org_id, uid_b, name, visibility="private")["group_id"]
    assert reg.assign_runs(org_id, uid_b, False, [rid], gid) is True

    # The creator sees it filed...
    mine = client.get(f"/api/history/{rid}", headers=_auth(tok_b)).json()
    assert mine["group_id"] == gid and mine["group_name"] == name
    # ...their org_admin sees the run itself, but no folder at all.
    theirs = client.get(f"/api/history/{rid}", headers=_auth(tok_a)).json()
    assert theirs["run_id"] == rid
    assert theirs["group_id"] is None and theirs["group_name"] is None
    # ...and it is under the admin's Ungrouped rather than nowhere.
    page = client.get("/api/history?group=ungrouped", headers=_auth(tok_a)).json()
    assert rid in [r["run_id"] for r in page["runs"]]


def test_ungrouped_chip_agrees_with_the_history_table(client):
    """The chip and the table must come from the SAME scope. An org_admin's
    History spans the whole org, so their Ungrouped count has to as well —
    a per-user count would show 1 beside a table listing 2."""
    tok_a, tok_b, _org_id = _team_of_two(client)
    filed = _seed_run_for(client, tok_a)
    _seed_run_for(client, tok_a)   # the admin's own, left out
    _seed_run_for(client, tok_b)   # the member's, left out
    gid = client.post("/api/groups", json={"name": f"F{uuid.uuid4().hex[:6]}"},
                      headers=_auth(tok_a)).json()["group_id"]
    assert client.put("/api/groups/assignments",
                      json={"run_ids": [filed], "group_id": gid},
                      headers=_auth(tok_a)).status_code == 200

    for tok in (tok_a, tok_b):
        body = client.get("/api/groups", headers=_auth(tok)).json()
        table = client.get("/api/history?group=ungrouped",
                           headers=_auth(tok)).json()["total"]
        assert body["ungrouped_count"] == table
        folder = next(g for g in body["groups"] if g["group_id"] == gid)
        in_folder = client.get(f"/api/history?group={gid}",
                               headers=_auth(tok)).json()["total"]
        assert folder["run_count"] == in_folder

    # The two scopes really do differ, so the agreement above is not trivial.
    assert client.get("/api/groups",
                      headers=_auth(tok_a)).json()["ungrouped_count"] == 2
    assert client.get("/api/groups",
                      headers=_auth(tok_b)).json()["ungrouped_count"] == 1


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
    require_user yields None. No identity owns a folder, so the list stays
    empty — but /api/history hands that same caller every run, so the
    Ungrouped chip has to be the real unscoped count, not a hardcoded 0.

    Mutations are 403, not 401: the SPA treats EVERY 401 as "session
    expired", clears the token and hard-redirects to /login, so a 401 here
    logged the whole app out on a click (F11)."""
    # Seed through a real token first: auth_isolated_schema is package-scoped
    # with no per-test truncation, so run alone this test would compare 0 to 0
    # and the defect it exists to pin (a hardcoded 0 beside a full table)
    # would go undetected.
    _seed_run_for(client, _register(client, f"an-{uuid.uuid4().hex[:8]}@e.com"))

    body = client.get("/api/groups").json()
    assert body["groups"] == []
    table = client.get("/api/history?group=ungrouped").json()["total"]
    assert table > 0
    assert body["ungrouped_count"] == table

    r = client.post("/api/groups", json={"name": "X"})
    assert r.status_code == 403
    assert r.json()["detail"] == "Sign-in required for groups"
    assert client.patch(f"/api/groups/{uuid.uuid4()}",
                        json={"name": "X"}).status_code == 403
    assert client.delete(f"/api/groups/{uuid.uuid4()}").status_code == 403
    assert client.put("/api/groups/assignments",
                      json={"run_ids": [str(uuid.uuid4())], "group_id": None}).status_code == 403


def _orgless_token(client) -> str:
    """An ACTIVE user whose token carries NO org — the shape a login mints
    when org provisioning failed, and it stays valid for its full life."""
    from src.backend.auth.jwt_utils import create_access_token, decode_token

    claims = decode_token(_register(client, f"no-org-{uuid.uuid4().hex[:8]}@e.com"))
    return create_access_token({
        "id": claims["user_id"], "email": claims["email"], "role": "user",
        "display_name": "", "org_id": None, "org_role": None,
        "token_version": claims["token_version"], "status": "active",
    })


def test_orgless_caller_sees_no_folders(client):
    """org_id=None means UNSCOPED at the registry — every org's folders,
    private ones included. That branch belongs to the platform admin alone;
    a token that merely lacks an org must never reach it."""
    other = _register(client, f"oo-{uuid.uuid4().hex[:8]}@e.com")
    client.post("/api/groups", json={"name": "Someone elses"}, headers=_auth(other))

    body = client.get("/api/groups", headers=_auth(_orgless_token(client))).json()
    assert body == {"groups": [], "ungrouped_count": 0}


def test_orgless_caller_cannot_mutate_folders(client):
    """...and the same caller gets a coherent 403 on every mutation, not a
    NotNullViolation 500 on create and a 404 on the rest."""
    other = _register(client, f"om-{uuid.uuid4().hex[:8]}@e.com")
    gid = client.post("/api/groups", json={"name": "Not yours"},
                      headers=_auth(other)).json()["group_id"]
    tok = _orgless_token(client)

    r = client.post("/api/groups", json={"name": "X"}, headers=_auth(tok))
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "Your account is not in an organization yet"
    assert client.patch(f"/api/groups/{gid}", json={"name": "Hax"},
                        headers=_auth(tok)).status_code == 403
    assert client.delete(f"/api/groups/{gid}", headers=_auth(tok)).status_code == 403
    assert client.put("/api/groups/assignments",
                      json={"run_ids": [str(uuid.uuid4())], "group_id": gid},
                      headers=_auth(tok)).status_code == 403
    # The other org's folder is untouched.
    assert [g["name"] for g in
            client.get("/api/groups", headers=_auth(other)).json()["groups"]] == ["Not yours"]


def test_platform_admin_sees_every_orgs_folders(client):
    """INVERTED CONTRACT (2026-08-21): a platform admin does NOT see every
    org's folders. Their runs span every org; their folders are their own
    org's, so read and write finally agree. Kept under the old name so the
    inversion is visible in the diff — see
    test_platform_admin_folder_view_is_their_own_org for the full rule."""
    from src.backend.auth.jwt_utils import create_access_token

    owner = _register(client, f"pa-{uuid.uuid4().hex[:8]}@e.com")
    gid = client.post("/api/groups", json={"name": f"Owned {uuid.uuid4().hex[:6]}"},
                      headers=_auth(owner)).json()["group_id"]
    admin_tok = create_access_token({
        "id": str(uuid.uuid4()), "email": "admin@test.local", "role": "admin",
        "display_name": "", "org_id": None, "org_role": None, "status": "active",
    })

    seen = client.get("/api/groups", headers=_auth(admin_tok)).json()["groups"]
    assert gid not in [g["group_id"] for g in seen]


def test_platform_admin_folder_view_is_their_own_org(client):
    """A platform admin's runs span every org; their FOLDERS do not.

    Before this, /api/groups was unscoped for them, so every org's folder
    names came back — other users' PRIVATE folder names included — while
    every mutation binds their real org and answered 404. The SPA drew
    Edit/Delete/Move on folders that could never work. Read and write now
    agree: their own org's folders, and nobody else's.
    """
    import jwt as _jwt
    from src.backend.auth.jwt_utils import create_access_token
    from src.backend.core.config import settings

    owner = _register(client, f"paf-{uuid.uuid4().hex[:8]}@e.com")
    foreign_org = client.post(
        "/api/groups", json={"name": f"Foreign {uuid.uuid4().hex[:6]}"},
        headers=_auth(owner)).json()
    foreign_private = client.post(
        "/api/groups",
        json={"name": f"Secret {uuid.uuid4().hex[:6]}", "visibility": "private"},
        headers=_auth(owner)).json()

    admin = _register(client, f"pa2-{uuid.uuid4().hex[:8]}@e.com")
    assert client.get("/api/groups", headers=_auth(admin)).json()["groups"] == []
    own = client.post("/api/groups", json={"name": f"Mine {uuid.uuid4().hex[:6]}"},
                      headers=_auth(admin)).json()

    # Re-mint the same identity with role=admin, keeping their real org claim.
    claims = _jwt.decode(admin, settings.JWT_SECRET_KEY, algorithms=["HS256"])
    admin_tok = create_access_token({
        "id": claims["sub"], "email": claims["email"], "role": "admin",
        "display_name": "", "org_id": claims["org_id"],
        "org_role": claims["org_role"], "status": "active",
        "token_version": claims.get("tv", 0),
    })

    seen = client.get("/api/groups", headers=_auth(admin_tok)).json()["groups"]
    ids = [g["group_id"] for g in seen]
    names = [g["name"] for g in seen]
    assert own["group_id"] in ids, "their own org's folder must show"
    assert foreign_org["group_id"] not in ids, "another org's folder must not"
    assert foreign_private["name"] not in names, "a foreign PRIVATE name must not leak"


def test_platform_admin_chip_still_equals_the_table(client):
    """The Ungrouped chip and the Ungrouped filter must describe one set for
    a platform admin too — a run filed in another org's folder is now 'in no
    folder I can see', so it belongs under Ungrouped rather than vanishing."""
    import jwt as _jwt
    from src.backend.auth.jwt_utils import create_access_token
    from src.backend.core.config import settings

    owner = _register(client, f"pac-{uuid.uuid4().hex[:8]}@e.com")
    rid = _seed_run_for(client, owner, query="filed in another org")
    gid = client.post("/api/groups", json={"name": f"Theirs {uuid.uuid4().hex[:6]}"},
                      headers=_auth(owner)).json()["group_id"]
    client.put("/api/groups/assignments",
               json={"run_ids": [rid], "group_id": gid}, headers=_auth(owner))

    admin = _register(client, f"pac2-{uuid.uuid4().hex[:8]}@e.com")
    claims = _jwt.decode(admin, settings.JWT_SECRET_KEY, algorithms=["HS256"])
    admin_tok = create_access_token({
        "id": claims["sub"], "email": claims["email"], "role": "admin",
        "display_name": "", "org_id": claims["org_id"],
        "org_role": claims["org_role"], "status": "active",
        "token_version": claims.get("tv", 0),
    })

    row = [r for r in client.get("/api/history", headers=_auth(admin_tok)).json()["runs"]
           if r["run_id"] == rid][0]
    assert row["group_id"] is None and row["group_name"] is None, \
        "another org's folder must not tag a row for a platform admin"

    chip = client.get("/api/groups", headers=_auth(admin_tok)).json()["ungrouped_count"]
    table = client.get("/api/history?group=ungrouped",
                       headers=_auth(admin_tok)).json()["total"]
    assert chip == table


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


# ---------------------------------------------------------------------------
# Integration: T3 — visibility on the wire, empty ?group=, token-less mutations
# ---------------------------------------------------------------------------

def test_create_private_folder_carries_visibility_and_created_by(client):
    """visibility and created_by are what the chip row draws the lock icon
    from and what it decides whether to offer rename/delete from, so both
    POST's echo and GET's list have to carry them."""
    from src.backend.auth.jwt_utils import decode_token

    tok = _register(client, f"pv-{uuid.uuid4().hex[:8]}@e.com")
    uid = decode_token(tok)["user_id"]

    r = client.post("/api/groups",
                    json={"name": "Secret", "visibility": "private"},
                    headers=_auth(tok))
    assert r.status_code == 201, r.text
    assert r.json()["visibility"] == "private"
    assert r.json()["created_by"] == uid

    listed = client.get("/api/groups", headers=_auth(tok)).json()["groups"]
    folder = next(g for g in listed if g["group_id"] == r.json()["group_id"])
    assert folder["visibility"] == "private" and folder["created_by"] == uid


def test_create_defaults_to_org_visibility(client):
    """Sharing is the default: a folder created without a visibility is the
    org's, matching the re-key's whole point."""
    tok = _register(client, f"dv-{uuid.uuid4().hex[:8]}@e.com")
    r = client.post("/api/groups", json={"name": "Shared"}, headers=_auth(tok))
    assert r.status_code == 201, r.text
    assert r.json()["visibility"] == "org"


def test_invalid_visibility_is_400_with_a_string_detail(client):
    """A hand-rolled 400, not a pydantic Literal's 422: the SPA renders
    detail as a string, and a 422's list-of-objects detail renders as
    garbage in the dialog."""
    tok = _register(client, f"bv-{uuid.uuid4().hex[:8]}@e.com")
    r = client.post("/api/groups", json={"name": "Nope", "visibility": "team"},
                    headers=_auth(tok))
    assert r.status_code == 400, r.text
    assert isinstance(r.json()["detail"], str)


def test_non_string_visibility_is_400_with_a_string_detail(client):
    """The same 422 rendering failure, reached by TYPE rather than by value:
    a nullable dialog state binding straight to the field sends null, and a
    pydantic `str` annotation would answer with a list detail. Validation is
    manual, so any JSON value has to reach _clean_visibility."""
    tok = _register(client, f"nv-{uuid.uuid4().hex[:8]}@e.com")
    for junk in (123, None, True, ["private"], {"v": "private"}):
        r = client.post("/api/groups", json={"name": "Nope", "visibility": junk},
                        headers=_auth(tok))
        assert r.status_code == 400, (junk, r.text)
        assert isinstance(r.json()["detail"], str), junk

    gid = client.post("/api/groups", json={"name": "Patch me"},
                      headers=_auth(tok)).json()["group_id"]
    for junk in (123, True, ["private"]):
        r = client.patch(f"/api/groups/{gid}", json={"visibility": junk},
                         headers=_auth(tok))
        assert r.status_code == 400, (junk, r.text)
        assert isinstance(r.json()["detail"], str), junk


def test_patch_flips_private_to_org(client):
    """Widening is always allowed, and the list reflects it immediately."""
    tok = _register(client, f"fp-{uuid.uuid4().hex[:8]}@e.com")
    gid = client.post("/api/groups",
                      json={"name": "Widen me", "visibility": "private"},
                      headers=_auth(tok)).json()["group_id"]

    r = client.patch(f"/api/groups/{gid}", json={"visibility": "org"},
                     headers=_auth(tok))
    assert r.status_code == 200, r.text
    assert r.json() == {"group_id": gid, "visibility": "org"}

    listed = client.get("/api/groups", headers=_auth(tok)).json()["groups"]
    assert next(g for g in listed if g["group_id"] == gid)["visibility"] == "org"


def test_flip_to_private_is_409_while_a_members_run_is_inside(client):
    """Narrowing would silently hide another member's own run from them, so
    it is refused — and the refusal is a COUNT, never who owns the runs."""
    from src.backend.auth.jwt_utils import decode_token

    tok_a, tok_b, _org_id = _team_of_two(client)
    gid = client.post("/api/groups", json={"name": f"Shared {uuid.uuid4().hex[:6]}"},
                      headers=_auth(tok_a)).json()["group_id"]
    rid = _seed_run_for(client, tok_b)
    assert client.put("/api/groups/assignments",
                      json={"run_ids": [rid], "group_id": gid},
                      headers=_auth(tok_b)).status_code == 200

    r = client.patch(f"/api/groups/{gid}", json={"visibility": "private"},
                     headers=_auth(tok_a))
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "1 run by another member" in detail
    claims_b = decode_token(tok_b)
    assert claims_b["email"] not in detail and claims_b["user_id"] not in detail

    # Refused means unchanged — B still sees the folder.
    listed = client.get("/api/groups", headers=_auth(tok_b)).json()["groups"]
    assert next(g for g in listed if g["group_id"] == gid)["visibility"] == "org"


def test_only_the_creator_can_change_visibility(client):
    """An org_admin may RENAME a member's org folder but may not flip it.

    created_by stays the member, so after a flip _visible_group refuses the
    admin the folder they just changed — a one-way action with no way back
    through the API. 403, not 404: they can already see the folder, so a
    'not found' would be a lie about something on their screen.
    """
    tok_admin, tok_member, _org_id = _team_of_two(client)

    gid = client.post(
        "/api/groups", json={"name": "Members folder"},
        headers=_auth(tok_member)).json()["group_id"]

    # The admin may still rename it.
    assert client.patch(f"/api/groups/{gid}", json={"name": "Renamed by admin"},
                        headers=_auth(tok_admin)).status_code == 200

    # But not flip it.
    r = client.patch(f"/api/groups/{gid}", json={"visibility": "private"},
                     headers=_auth(tok_admin))
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "Only the folder's creator can change its visibility"

    # Name and visibility both survive.
    seen = [g for g in client.get("/api/groups", headers=_auth(tok_admin)).json()["groups"]
            if g["group_id"] == gid][0]
    assert seen["name"] == "Renamed by admin" and seen["visibility"] == "org"

    # The creator still may.
    assert client.patch(f"/api/groups/{gid}", json={"visibility": "private"},
                        headers=_auth(tok_member)).status_code == 200


def test_patch_with_an_empty_body_is_400(client):
    """Neither name nor visibility is nothing to do — a 400, not the
    registry's ValueError surfacing as a 500."""
    tok = _register(client, f"eb-{uuid.uuid4().hex[:8]}@e.com")
    gid = client.post("/api/groups", json={"name": "Untouched"},
                      headers=_auth(tok)).json()["group_id"]

    r = client.patch(f"/api/groups/{gid}", json={}, headers=_auth(tok))
    assert r.status_code == 400, r.text
    assert isinstance(r.json()["detail"], str)


def test_flip_that_collides_with_an_existing_private_name_is_409(client):
    """The two partial indexes let a private "X" and an org "X" coexist, so
    the flip is where that name collision finally lands."""
    tok = _register(client, f"cc-{uuid.uuid4().hex[:8]}@e.com")
    name = f"Dup {uuid.uuid4().hex[:6]}"
    assert client.post("/api/groups", json={"name": name, "visibility": "private"},
                       headers=_auth(tok)).status_code == 201
    gid = client.post("/api/groups", json={"name": name, "visibility": "org"},
                      headers=_auth(tok)).json()["group_id"]

    r = client.patch(f"/api/groups/{gid}", json={"visibility": "private"},
                     headers=_auth(tok))
    assert r.status_code == 409, r.text
    assert isinstance(r.json()["detail"], str)


def test_org_admin_created_folder_records_their_real_user_id(client):
    """The identity trap: an org_admin's run-FILTER user_id is None while
    their identity is not. Wire created_by from the filter and every folder
    an org_admin makes is owned by NULL."""
    from src.backend.auth.jwt_utils import decode_token

    tok_a, _tok_b, _org_id = _team_of_two(client)
    uid_a = decode_token(tok_a)["user_id"]

    r = client.post("/api/groups",
                    json={"name": f"Admin made {uuid.uuid4().hex[:6]}"},
                    headers=_auth(tok_a))
    assert r.status_code == 201, r.text
    assert r.json()["created_by"] == uid_a

    listed = client.get("/api/groups", headers=_auth(tok_a)).json()["groups"]
    folder = next(g for g in listed if g["group_id"] == r.json()["group_id"])
    assert folder["created_by"] == uid_a


def test_assignments_reject_more_than_500_run_ids(client):
    """A cap on the batch — one UPDATE with an unbounded ANY(%s) array is
    the one request that can pin the pool."""
    tok = _register(client, f"cap-{uuid.uuid4().hex[:8]}@e.com")
    r = client.put("/api/groups/assignments",
                   json={"run_ids": [str(uuid.uuid4()) for _ in range(501)],
                         "group_id": None},
                   headers=_auth(tok))
    assert r.status_code == 400, r.text


def test_empty_group_param_returns_the_full_total(client):
    """F6: an empty ?group= is falsy, so it skipped the uuid validation and
    reached the registry as '' — SQL read that as group_id = '' and matched
    0 rows of N instead of all of them."""
    tok = _register(client, f"eg-{uuid.uuid4().hex[:8]}@e.com")
    grouped = _seed_run_for(client, tok)
    _seed_run_for(client, tok)
    gid = client.post("/api/groups", json={"name": "Some folder"},
                      headers=_auth(tok)).json()["group_id"]
    assert client.put("/api/groups/assignments",
                      json={"run_ids": [grouped], "group_id": gid},
                      headers=_auth(tok)).status_code == 200

    unfiltered = client.get("/api/history", headers=_auth(tok)).json()["total"]
    assert unfiltered == 2
    assert client.get("/api/history?group=",
                      headers=_auth(tok)).json()["total"] == unfiltered
    # Whitespace-only is the same defect one input class in: strip() leaves ""
    # behind, and "" is not None, so it still reaches SQL as group_id = ''.
    assert client.get("/api/history?group=%20%20",
                      headers=_auth(tok)).json()["total"] == unfiltered

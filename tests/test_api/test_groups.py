"""Run groups: registry CRUD/assignment + /api/groups + history filter.

Groups are ORG-scoped folders over test_runs rows (one group per run,
test_runs.group_id). A folder belongs to the org: every member sees every
folder in it, and every run filed into one. Filing a run is what PUBLISHES it
to the team — an ungrouped run is work in progress and stays with its author,
which is what tests/test_api/test_group_org_visibility.py covers end to end.

Authority: anyone in the org creates; the creator, or an org_admin, renames;
an ORG_ADMIN alone deletes, because deleting returns every run inside to
Ungrouped and so un-shares them from the whole org; a run is filed only by its own owner or an org_admin, so seeing a
colleague's published test is not authority over where it lives. Refusals are
404 (False at the registry), never 403, so a folder's existence cannot be
probed by id. A folder name belongs to the org, case-insensitively.

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
        listed = reg.list_groups(ORG_A)
        assert [x["name"] for x in listed] == ["Checkout"]

    def test_list_groups_is_per_org_and_name_sorted(self, reg):
        reg.create_group(ORG_A, "u1", "smoke")
        reg.create_group(ORG_A, "u1", "Checkout")
        reg.create_group(ORG_B, "u2", "Other")
        assert [x["name"] for x in reg.list_groups(ORG_A)] == ["Checkout", "smoke"]
        assert [x["name"] for x in reg.list_groups(ORG_B)] == ["Other"]

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
        assert reg.rename_group(ORG_A, "u1", False, gid, "New") is True
        assert [x["name"] for x in reg.list_groups(ORG_A)] == ["New"]

    def test_rename_to_existing_name_raises(self, reg):
        """Rename has its OWN duplicate guard — create's does not cover it."""
        from src.backend.core.run_registry import DuplicateGroupName
        reg.create_group(ORG_A, "u1", "Keep")
        gid = reg.create_group(ORG_A, "u1", "Other")["group_id"]
        with pytest.raises(DuplicateGroupName):
            reg.rename_group(ORG_A, "u1", False, gid, "keep")
        assert sorted(x["name"] for x in reg.list_groups(ORG_A)) == ["Keep", "Other"]

    def test_rename_foreign_or_unknown_is_false(self, reg):
        gid = reg.create_group(ORG_A, "u1", "Mine")["group_id"]
        assert reg.rename_group(ORG_B, "u2", True, gid, "Stolen") is False
        assert reg.rename_group(ORG_A, "u1", False, str(uuid.uuid4()), name="Ghost") is False
        assert [x["name"] for x in reg.list_groups(ORG_A)] == ["Mine"]

    def test_rename_group_audit_old_name_captures_the_prior_name(self, reg):
        """The audit trail must say what a folder USED to be called, not
        just what it is now — the new name alone can't answer that."""
        gid = reg.create_group(ORG_A, "u1", "Before")["group_id"]
        captured: list = []
        assert reg.rename_group(
            ORG_A, "u1", False, gid, "After", audit_old_name=captured) is True
        assert captured == ["Before"]

    def test_rename_group_audit_old_name_empty_on_refusal(self, reg):
        """A refused rename must record no name at all — the out-param
        stays empty exactly when the caller's own audit_detail must too."""
        gid = reg.create_group(ORG_A, "u1", "Untouched")["group_id"]
        captured: list = []
        assert reg.rename_group(
            ORG_B, "u2", True, gid, "Stolen", audit_old_name=captured) is False
        assert captured == []

    def test_delete_group(self, reg):
        gid = reg.create_group(ORG_A, "u1", "Gone")["group_id"]
        assert reg.delete_group(ORG_A, "u1", True, gid) is True
        assert reg.list_groups(ORG_A) == []

    def test_delete_foreign_or_unknown_is_false(self, reg):
        gid = reg.create_group(ORG_A, "u1", "Mine")["group_id"]
        assert reg.delete_group(ORG_B, "u2", True, gid) is False
        assert reg.delete_group(ORG_A, "u1", False, str(uuid.uuid4())) is False
        assert [x["name"] for x in reg.list_groups(ORG_A)] == ["Mine"]

    def test_delete_group_no_connection_when_caller_lacks_authority(self, reg):
        """is_org_admin/org_id needs no database — a caller who fails that
        check must never reach the pool at all, so the DELETE's own cost
        (and any lock it would take) is paid only when it might actually
        run."""
        from unittest.mock import patch
        gid = str(uuid.uuid4())
        with patch.object(reg._pool, "connection") as mock_connect:
            assert reg.delete_group(ORG_A, "u1", False, gid) is False
            assert reg.delete_group(None, "u1", True, gid) is False
        mock_connect.assert_not_called()

    def test_count_ungrouped(self, reg):
        r1, r2, r3 = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        for rid, uid in ((r1, "u1"), (r2, "u1"), (r3, "u2")):
            reg.record_start(rid, {"user_id": uid, "email": f"{uid}@e.com"}, "q", "passed")
        assert reg.count_ungrouped("u1") == 2
        assert reg.count_ungrouped("u2") == 1


class TestGroupAuthorityMatrix:
    """The authority matrix of the org-keyed model, at the registry.

    Asserted at the registry, which is where the matrix lives: a refusal is
    False, which the endpoints turn into 404 rather than 403. Visibility is
    on the wire too (T3) — the endpoint-level tests for it are further down
    this file."""

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
    def test_every_folder_is_shared_with_the_whole_org(self, reg):
        """A folder belongs to the org, so a member sees one an admin made and
        an admin sees one a member made. There is no folder anybody in the org
        cannot see."""
        reg.create_group(self.ORG, self.ADMIN, "Admin made")
        reg.create_group(self.ORG, self.MEMBER, "Member made")

        for who in (self.MEMBER, self.ADMIN):
            assert sorted(g["name"] for g in reg.list_groups(self.ORG)) == [
                "Admin made", "Member made"], f"{who} sees a different set"

    # 2 -------------------------------------------------------------------
    def test_a_creator_renames_their_own_folder_but_cannot_delete_it(self, reg):
        """Rename and delete part company here. Deleting returns every run in
        the folder to Ungrouped, un-sharing them from the org, so it is an
        org-level act whoever made the folder (owner decision, 2026-08-23)."""
        gid = reg.create_group(self.ORG, self.MEMBER, "Mine")["group_id"]

        assert reg.rename_group(self.ORG, self.MEMBER, False, gid, "Mine 2") is True
        assert [g["name"] for g in reg.list_groups(self.ORG)] == ["Mine 2"]

        assert reg.delete_group(self.ORG, self.MEMBER, False, gid) is False
        assert [g["name"] for g in reg.list_groups(self.ORG)] == ["Mine 2"], (
            "the refused delete must leave the folder alone")
        assert reg.delete_group(self.ORG, self.ADMIN, True, gid) is True

    # 3 -------------------------------------------------------------------
    def test_org_admin_may_mutate_an_org_folder_they_did_not_create(self, reg):
        gid = reg.create_group(self.ORG, self.MEMBER, "Team")["group_id"]
        assert reg.rename_group(self.ORG, self.ADMIN, True, gid, "Team 2") is True
        assert reg.delete_group(self.ORG, self.ADMIN, True, gid) is True

    # 4 -------------------------------------------------------------------
    def test_member_may_not_mutate_an_org_folder_they_did_not_create(self, reg):
        gid = reg.create_group(self.ORG, self.ADMIN, "Team")["group_id"]
        assert reg.rename_group(self.ORG, self.MEMBER, False, gid, "Hax") is False
        assert reg.delete_group(self.ORG, self.MEMBER, False, gid) is False
        assert [g["name"] for g in reg.list_groups(self.ORG)] == ["Team"]

    # 5 -------------------------------------------------------------------
    def test_member_files_own_run_into_any_folder_in_the_org(self, reg):
        rid = str(uuid.uuid4())
        self._seed(reg, rid, self.MEMBER, self.ORG)
        gid = reg.create_group(self.ORG, self.ADMIN, "Team")["group_id"]

        assert reg.assign_runs(self.ORG, self.MEMBER, False, [rid], gid) is True
        assert reg.get_run(rid)["group_id"] == gid

    # 6 -------------------------------------------------------------------
    def test_org_admin_files_a_members_run_into_an_org_folder(self, reg):
        rid = str(uuid.uuid4())
        self._seed(reg, rid, self.MEMBER, self.ORG)
        gid = reg.create_group(self.ORG, self.ADMIN, "Team")["group_id"]

        assert reg.assign_runs(self.ORG, self.ADMIN, True, [rid], gid) is True
        assert reg.get_run(rid)["group_id"] == gid

    # 7 -------------------------------------------------------------------
    def test_member_cannot_file_a_run_they_do_not_own(self, reg):
        """Decision D4. Once a colleague's run is published the member can SEE
        it, which is exactly when this needs pinning: reading a run is not
        authority over where it lives."""
        rid = str(uuid.uuid4())
        self._seed(reg, rid, self.ADMIN, self.ORG)
        gid = reg.create_group(self.ORG, self.MEMBER, "Team")["group_id"]
        assert reg.assign_runs(self.ORG, self.ADMIN, True, [rid], gid) is True

        other = reg.create_group(self.ORG, self.MEMBER, "Elsewhere")["group_id"]
        assert reg.assign_runs(self.ORG, self.MEMBER, False, [rid], other) is False
        assert reg.get_run(rid)["group_id"] == gid

    # 7b ------------------------------------------------------------------
    def test_a_run_from_another_org_is_never_filed(self, reg):
        """The clause that stops grouping from publishing a run to an org it
        does not belong to: a member's OWN run, made while they were in a
        different org, cannot be filed into this org's folder."""
        rid = str(uuid.uuid4())
        self._seed(reg, rid, self.MEMBER, "org-elsewhere")
        gid = reg.create_group(self.ORG, self.MEMBER, "Team")["group_id"]

        assert reg.assign_runs(self.ORG, self.MEMBER, False, [rid], gid) is False
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
    def test_a_folder_name_belongs_to_the_whole_org(self, reg):
        """One name, one folder, whoever created it — so every testcase
        carries a single folder name. Case-insensitive, and scoped to the org
        rather than to the creator, so a colleague's name is taken too."""
        from src.backend.core.run_registry import DuplicateGroupName
        reg.create_group(self.ORG, self.ADMIN, "checkout")
        with pytest.raises(DuplicateGroupName):
            reg.create_group(self.ORG, self.MEMBER, "Checkout")
        with pytest.raises(DuplicateGroupName):
            reg.create_group(self.ORG, self.ADMIN, "CHECKOUT")
        # A different org is a different name space.
        reg.create_group("org-elsewhere", self.MEMBER, "checkout")

    # 8b ------------------------------------------------------------------
    def test_the_duplicate_error_names_the_folder_that_already_exists(self, reg):
        """The message has to be findable. Echoing the name the caller TYPED
        answered 'a group named "regression PACK" already exists' for a folder
        actually called "Regression Pack" — the user then goes looking for a
        name nothing in the list carries."""
        from src.backend.core.run_registry import DuplicateGroupName
        reg.create_group(self.ORG, self.ADMIN, "Regression Pack")

        with pytest.raises(DuplicateGroupName) as exc:
            reg.create_group(self.ORG, self.MEMBER, "regression PACK")
        assert str(exc.value) == "Regression Pack"

        gid = reg.create_group(self.ORG, self.MEMBER, "Smoke")["group_id"]
        with pytest.raises(DuplicateGroupName) as exc:
            reg.rename_group(self.ORG, self.MEMBER, False, gid, "REGRESSION pack")
        assert str(exc.value) == "Regression Pack"


class TestReadPathVisibility:
    """The read half: the org-scoped LEFT JOIN, and the run predicate.

    Every folder question is answered by ONE join —
        LEFT JOIN run_groups g ON g.group_id = t.group_id
                              AND g.org_id = <caller org>
    so a row's folder tag is g.name, its folder id is g.group_id (never
    t.group_id), and "Ungrouped" means "in no folder I can see" rather than
    "t.group_id IS NULL". The TOKEN-LESS path (AUTH_ENFORCED off) gets the
    join UNFILTERED: binding a NULL org would evaluate the condition to NULL
    for every row and collapse the whole page into Ungrouped. An identified
    caller who simply has no org is the other no-org case and resolves NO
    folder — they own no org, so nothing is theirs to see.

    There is no per-user term in the join any more — a folder belongs to the
    org, so every member sees every folder in it. WHICH RUNS a caller sees is
    a separate predicate (_VISIBLE_RUN_SQL), but it reads its published half
    off this same join: their own work, plus anything filed into a folder of
    THEIR org.

    The org trap: a platform admin's FILTER org_id is None (their runs span
    every org) while their folder org is not, so the join always binds
    folder_org_id, never the scoping org_id."""

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
    def test_a_folder_name_shows_to_every_member(self, reg):
        """A folder is the org's, so its name reaches every member — there is
        no folder whose name is hidden from someone inside the org."""
        gid = reg.create_group(self.ORG, self.ADMIN, "Team Checkout")["group_id"]
        rid = str(uuid.uuid4())
        self._seed(reg, rid, self.MEMBER, self.ORG)
        assert reg.assign_runs(self.ORG, self.MEMBER, False, [rid], gid) is True

        rows, _ = reg.list_runs(user_id=self.MEMBER, org_id=self.ORG,
                                folder_org_id=self.ORG)
        row = next(r for r in rows if r["run_id"] == rid)
        assert row["group_name"] == "Team Checkout"
        assert row["group_id"] == gid

    def test_a_folder_from_another_org_reads_as_ungrouped(self, reg):
        """The join is org-scoped, so a row somehow pointing at a foreign
        folder reports no folder rather than leaking its name — and still
        appears, under Ungrouped, instead of belonging to no filter at all."""
        foreign = reg.create_group("org-elsewhere", self.ADMIN, "Theirs")["group_id"]
        rid = str(uuid.uuid4())
        self._seed(reg, rid, self.MEMBER, self.ORG)
        with reg._pool.connection() as conn:
            conn.execute("UPDATE test_runs SET group_id = %s WHERE run_id = %s",
                         (foreign, rid))

        rows, _ = reg.list_runs(user_id=self.MEMBER, org_id=self.ORG,
                                folder_org_id=self.ORG)
        row = next(r for r in rows if r["run_id"] == rid)
        assert row["group_id"] is None and row["group_name"] is None

        ungrouped, _ = reg.list_runs(user_id=self.MEMBER, org_id=self.ORG,
                                     folder_org_id=self.ORG, group="ungrouped")
        assert rid in [r["run_id"] for r in ungrouped]

    def test_a_foreign_folder_does_not_publish_a_run_to_a_peer(self, reg):
        """The other seat on the row above, which asserts only as the OWNER —
        for whom the predicate's first half answers no matter what its second
        half does. A folder outside the org cannot publish anything TO the
        org, so a peer must not reach the run at all: not in their table, not
        under Ungrouped, and not in the chip that counts it. Testing the raw
        t.group_id here handed a colleague's unfiled work — log.html and
        whatever credentials its author typed — to every member."""
        foreign = reg.create_group("org-elsewhere", self.ADMIN, "Theirs")["group_id"]
        rid = str(uuid.uuid4())
        self._seed(reg, rid, self.MEMBER, self.ORG)
        with reg._pool.connection() as conn:
            conn.execute("UPDATE test_runs SET group_id = %s WHERE run_id = %s",
                         (foreign, rid))

        peer = "u-peer"
        rows, total = reg.list_runs(user_id=peer, org_id=self.ORG,
                                    folder_org_id=self.ORG)
        assert [r["run_id"] for r in rows] == [] and total == 0

        ungrouped, u_total = reg.list_runs(user_id=peer, org_id=self.ORG,
                                           folder_org_id=self.ORG,
                                           group="ungrouped")
        assert [r["run_id"] for r in ungrouped] == []
        assert reg.count_ungrouped(
            peer, self.ORG, folder_org_id=self.ORG) == u_total == 0

    def test_a_caller_with_no_org_sees_only_their_own_runs(self, reg):
        """ownership.caller_can_access rule 4: an identity carrying no org_id
        claim falls back to the bare owner check. With no org there is no org
        anything could have been published TO, so the predicate's second half
        must vanish for them — and the folder join must not hand them a
        foreign folder's name either."""
        gid = reg.create_group(self.ORG, self.ADMIN, "Team")["group_id"]
        theirs, mine = str(uuid.uuid4()), str(uuid.uuid4())
        self._seed(reg, theirs, self.ADMIN, self.ORG)
        self._seed(reg, mine, "u-nomad", None)
        assert reg.assign_runs(self.ORG, self.ADMIN, True, [theirs], gid) is True

        rows, total = reg.list_runs(user_id="u-nomad", org_id=None,
                                    folder_org_id=None)
        assert [r["run_id"] for r in rows] == [mine] and total == 1
        assert rows[0]["group_id"] is None and rows[0]["group_name"] is None

    def test_count_ungrouped_agrees_with_the_ungrouped_filter(self, reg):
        """The chip and the filter must always describe ONE set. Ungrouped
        means work in progress, so for a member it is their OWN unfiled runs:
        not a colleague's unfiled work, and not anything already published."""
        gid = reg.create_group(self.ORG, self.ADMIN, "Team")["group_id"]
        filed, mine_open, theirs_open = (str(uuid.uuid4()) for _ in range(3))
        self._seed(reg, filed, self.MEMBER, self.ORG)
        self._seed(reg, mine_open, self.MEMBER, self.ORG)
        self._seed(reg, theirs_open, self.ADMIN, self.ORG)
        assert reg.assign_runs(self.ORG, self.MEMBER, False, [filed], gid) is True

        rows, total = reg.list_runs(user_id=self.MEMBER, org_id=self.ORG,
                                    folder_org_id=self.ORG, group="ungrouped")
        n = reg.count_ungrouped(self.MEMBER, self.ORG, folder_org_id=self.ORG)
        assert [r["run_id"] for r in rows] == [mine_open]
        assert n == total == 1

    def test_an_org_admin_chip_still_equals_their_table(self, reg):
        """An org_admin's scope spans the org, so their Ungrouped covers every
        member's unfiled work — and the chip has to agree with that too."""
        a1, m1 = str(uuid.uuid4()), str(uuid.uuid4())
        self._seed(reg, a1, self.ADMIN, self.ORG)
        self._seed(reg, m1, self.MEMBER, self.ORG)

        rows, total = reg.list_runs(user_id=None, org_id=self.ORG,
                                    folder_org_id=self.ORG, group="ungrouped")
        n = reg.count_ungrouped(None, self.ORG, folder_org_id=self.ORG)
        assert sorted(r["run_id"] for r in rows) == sorted([a1, m1])
        assert n == total == 2

    def test_the_chip_equals_the_filter_for_every_caller_shape(self, reg):
        """The chip and the Ungrouped filter share one predicate constant so
        they cannot drift; this pins that for the caller shapes the two tests
        above do not cover, against a board that includes a run pointing at a
        FOREIGN folder — the row where the two used to agree on the wrong
        number for a peer.

        Shapes, in order: a plain member of the org; the org_admin (no
        per-user narrowing); a platform admin (runs span every org, folders
        do not); the token-less dev caller (no scope at all); and an identity
        with no org (rule 4, own work only)."""
        gid = reg.create_group(self.ORG, self.ADMIN, "Team")["group_id"]
        foreign = reg.create_group("org-elsewhere", self.ADMIN, "Theirs")["group_id"]
        filed, peer_open, stray, nomad = (str(uuid.uuid4()) for _ in range(4))
        self._seed(reg, filed, self.ADMIN, self.ORG)
        self._seed(reg, peer_open, self.MEMBER, self.ORG)
        self._seed(reg, stray, self.ADMIN, self.ORG)
        self._seed(reg, nomad, "u-nomad", None)
        assert reg.assign_runs(self.ORG, self.ADMIN, True, [filed], gid) is True
        with reg._pool.connection() as conn:
            conn.execute("UPDATE test_runs SET group_id = %s WHERE run_id = %s",
                         (foreign, stray))

        shapes = [
            # (label, user_id, org_id, folder_org_id, include_unowned, expected)
            ("member", self.MEMBER, self.ORG, self.ORG, False, [peer_open]),
            ("org_admin", None, self.ORG, self.ORG, False, [peer_open, stray]),
            ("platform_admin", None, None, self.ORG, True,
             [peer_open, stray, nomad]),
            ("dev_caller", None, None, None, True, [peer_open, nomad]),
            ("no_org", "u-nomad", None, None, False, [nomad]),
        ]
        for label, uid, org, folder_org, unowned, expected in shapes:
            rows, total = reg.list_runs(
                user_id=uid, org_id=org, folder_org_id=folder_org,
                group="ungrouped", include_unowned=unowned)
            n = reg.count_ungrouped(uid, org, folder_org_id=folder_org,
                                    include_unowned=unowned)
            assert sorted(r["run_id"] for r in rows) == sorted(expected), label
            assert n == total == len(expected), label

    def test_folder_run_count_agrees_with_the_folder_filter(self, reg):
        """A folder's contents are the ORG's, so the chip and the filtered
        table read the same number for every member of it — not the caller's
        share of the folder. A member seeing 1 beside a folder holding 2 was
        the defect that made a shared folder cosmetic."""
        a1, m1 = str(uuid.uuid4()), str(uuid.uuid4())
        self._seed(reg, a1, self.ADMIN, self.ORG)
        self._seed(reg, m1, self.MEMBER, self.ORG)
        gid = reg.create_group(self.ORG, self.ADMIN, "Team")["group_id"]
        assert reg.assign_runs(self.ORG, self.ADMIN, True, [a1, m1], gid) is True

        listed = reg.list_groups(self.ORG)
        for scope_user in (self.MEMBER, None):   # plain member, then org_admin
            _, total = reg.list_runs(user_id=scope_user, org_id=self.ORG,
                                     folder_org_id=self.ORG, group=gid)
            assert [g["run_count"] for g in listed] == [total] == [2], scope_user

    # 14 ------------------------------------------------------------------
    def test_filtering_by_a_foreign_orgs_folder_returns_nothing(self, reg):
        """A folder id the caller's org does not own narrows to nothing rather
        than reaching across orgs."""
        foreign = reg.create_group("org-elsewhere", self.ADMIN, "Theirs")["group_id"]
        rid = str(uuid.uuid4())
        self._seed(reg, rid, self.MEMBER, self.ORG)

        rows, total = reg.list_runs(user_id=self.MEMBER, org_id=self.ORG,
                                    folder_org_id=self.ORG, group=foreign)
        assert rows == [] and total == 0

    def test_get_run_hides_a_foreign_folder_from_a_caller_with_no_org(self, reg):
        """get_run answers the drawer off the same join, so it owes the same
        answer. The token-less dev caller keeps its historic unfiltered form;
        an identity that simply has no org resolves no folder, exactly as it
        does in the list."""
        foreign = reg.create_group("org-elsewhere", self.ADMIN, "Theirs")["group_id"]
        rid = str(uuid.uuid4())
        self._seed(reg, rid, "u-nomad", None)
        with reg._pool.connection() as conn:
            conn.execute("UPDATE test_runs SET group_id = %s WHERE run_id = %s",
                         (foreign, rid))

        row = reg.get_run(rid, org_id=None, identified=True)
        assert row is not None
        assert row["group_id"] is None and row["group_name"] is None

        dev = reg.get_run(rid)
        assert dev["group_id"] == foreign and dev["group_name"] == "Theirs"

    def test_join_parameters_bind_before_the_where_clause(self, reg):
        """The join carries its OWN placeholders, and they appear in the SQL
        text BEFORE the WHERE clause's. psycopg binds %s strictly by position,
        so mis-ordering them filters on the wrong values SILENTLY rather than
        raising. Every filter at once is what catches it.

        Both runs are in the folder and both are visible to the member now, so
        the STATUS filter is what has to do the narrowing here — which is the
        point: it proves the WHERE params survived the join's."""
        mine, other = str(uuid.uuid4()), str(uuid.uuid4())
        self._seed(reg, mine, self.MEMBER, self.ORG, status="failed")
        self._seed(reg, other, self.ADMIN, self.ORG, status="passed")
        gid = reg.create_group(self.ORG, self.ADMIN, "Team")["group_id"]
        assert reg.assign_runs(self.ORG, self.ADMIN, True, [mine, other], gid) is True

        rows, total = reg.list_runs(
            user_id=self.MEMBER, org_id=self.ORG, status="failed", q="q",
            group=gid, folder_org_id=self.ORG, limit=10, offset=0)
        assert total == 1
        assert rows[0]["run_id"] == mine and rows[0]["group_name"] == "Team"


class TestGroupAssignmentAndFilter:
    """assign_runs atomicity + the group filter / group_name join in
    list_runs and get_run. Reuses TestGroupRegistryCrud's isolated-schema
    pattern via the same fixtures.

    The reads pass org_id, as every real call site does (history_scope always
    supplies the caller's own org): an identity with no org resolves no
    folder at all, so a folder filter from one narrows to nothing."""

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
    def _seed(reg, run_id, user_id, org_id=ORG_A, query="q", status="passed",
              rerun_of=None):
        reg.record_start(
            run_id,
            {"user_id": user_id, "org_id": org_id, "email": f"{user_id}@e.com"},
            query, status, rerun_of=rerun_of,
        )

    def test_assign_sets_group_and_filter_returns_rows(self, reg):
        r1, r2 = str(uuid.uuid4()), str(uuid.uuid4())
        self._seed(reg, r1, "u1")
        self._seed(reg, r2, "u1")
        gid = reg.create_group(ORG_A, "u1", "Checkout")["group_id"]

        assert reg.assign_runs(ORG_A, "u1", False, [r1, r2], gid) is True

        rows, total = reg.list_runs(user_id="u1", org_id=ORG_A, group=gid)
        assert total == 2
        assert all(r["group_id"] == gid and r["group_name"] == "Checkout" for r in rows)
        assert reg.list_groups(ORG_A)[0]["run_count"] == 2
        assert reg.count_ungrouped("u1", ORG_A) == 0

    def test_unassign_with_none_and_ungrouped_filter(self, reg):
        r1 = str(uuid.uuid4())
        self._seed(reg, r1, "u1")
        gid = reg.create_group(ORG_A, "u1", "G")["group_id"]
        reg.assign_runs(ORG_A, "u1", False, [r1], gid)

        assert reg.assign_runs(ORG_A, "u1", False, [r1], None) is True
        rows, total = reg.list_runs(user_id="u1", org_id=ORG_A, group="ungrouped")
        assert total == 1 and rows[0]["run_id"] == r1 and rows[0]["group_id"] is None

    def test_assign_foreign_run_rejected_atomically(self, reg):
        mine, theirs = str(uuid.uuid4()), str(uuid.uuid4())
        self._seed(reg, mine, "u1")
        self._seed(reg, theirs, "u2", org_id=ORG_B)
        gid = reg.create_group(ORG_A, "u1", "G")["group_id"]

        assert reg.assign_runs(ORG_A, "u1", False, [mine, theirs], gid) is False
        # Atomic: my run was NOT grouped either.
        _, total = reg.list_runs(user_id="u1", org_id=ORG_A, group=gid)
        assert total == 0

    def test_assign_into_foreign_group_rejected(self, reg):
        mine = str(uuid.uuid4())
        self._seed(reg, mine, "u1")
        foreign_gid = reg.create_group(ORG_B, "u2", "Theirs")["group_id"]

        assert reg.assign_runs(ORG_A, "u1", False, [mine], foreign_gid) is False
        _, total = reg.list_runs(user_id="u1", org_id=ORG_A, group="ungrouped")
        assert total == 1

    def test_group_filter_combines_with_status(self, reg):
        r1, r2 = str(uuid.uuid4()), str(uuid.uuid4())
        self._seed(reg, r1, "u1", status="passed")
        self._seed(reg, r2, "u1", status="failed")
        gid = reg.create_group(ORG_A, "u1", "G")["group_id"]
        reg.assign_runs(ORG_A, "u1", False, [r1, r2], gid)

        rows, total = reg.list_runs(user_id="u1", org_id=ORG_A, group=gid,
                                    status="failed")
        assert total == 1 and rows[0]["run_id"] == r2

    def test_delete_group_ungroups_members(self, reg):
        r1 = str(uuid.uuid4())
        self._seed(reg, r1, "u1")
        gid = reg.create_group(ORG_A, "u1", "Doomed")["group_id"]
        reg.assign_runs(ORG_A, "u1", False, [r1], gid)

        assert reg.delete_group(ORG_A, "u1", True, gid) is True
        run = reg.get_run(r1)
        assert run is not None and run["group_id"] is None  # run survived, ungrouped

    def test_delete_group_audit_run_ids_captures_members_before_the_delete(self, reg):
        """fk_test_runs_group is ON DELETE SET NULL, so the membership is
        gone the instant the DELETE commits — this proves the read happens
        while it can still see it."""
        r1, r2 = str(uuid.uuid4()), str(uuid.uuid4())
        self._seed(reg, r1, "u1")
        self._seed(reg, r2, "u1")
        gid = reg.create_group(ORG_A, "u1", "Doomed")["group_id"]
        reg.assign_runs(ORG_A, "u1", False, [r1, r2], gid)

        captured: list = []
        assert reg.delete_group(
            ORG_A, "u1", True, gid, audit_run_ids=captured) is True
        assert sorted(captured) == sorted([r1, r2])

    def test_delete_group_audit_run_ids_empty_on_refusal(self, reg):
        r1 = str(uuid.uuid4())
        self._seed(reg, r1, "u1")
        gid = reg.create_group(ORG_A, "u1", "Guarded")["group_id"]
        reg.assign_runs(ORG_A, "u1", False, [r1], gid)

        captured: list = []
        assert reg.delete_group(
            ORG_A, "u1", False, gid, audit_run_ids=captured) is False
        assert captured == []

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

    def test_assign_runs_returns_false_when_the_folder_vanishes(self, reg):
        """assign_runs loses the race: the folder clears the authority check,
        then is gone by the time the UPDATE runs. It must fail closed —
        False (which the endpoint turns into a 404), no exception, nothing
        written — and leave the pooled connection usable."""
        from unittest.mock import patch
        r1 = str(uuid.uuid4())
        self._seed(reg, r1, "u1")
        ghost = str(uuid.uuid4())
        vanished = {"group_id": ghost, "created_by": "u1"}

        with patch.object(reg, "_visible_group", return_value=vanished):
            assert reg.assign_runs(ORG_A, "u1", False, [r1], ghost) is False

        run = reg.get_run(r1)
        # get_run swallows its own exceptions and returns None, so check the
        # row came back before subscripting: a connection poisoned by the
        # caught violation shows up here as a legible assertion, not a
        # TypeError on NoneType.
        assert run is not None, "the pooled connection did not survive the rollback"
        assert run["group_id"] is None

    # -- A re-run belongs to its test (owner decision X2) -----------------
    #
    # `rerun_of` is the only link between the two rows, and publication lives
    # on each row's own group_id. Without the cascade below, a peer's re-run
    # kept the author's script published to the org after the author un-filed
    # the original — reproduced live 2026-08-24: the original answered 404
    # while the re-run answered 200 with byte-identical robot_code.

    def test_unfiling_a_test_takes_its_colocated_rerun_with_it(self, reg):
        """The core case. The re-run sits in the SAME folder as its test, so
        un-publishing the test un-publishes the copy of it."""
        parent, child = str(uuid.uuid4()), str(uuid.uuid4())
        self._seed(reg, parent, "u1")
        self._seed(reg, child, "u2", rerun_of=parent)
        gid = reg.create_group(ORG_A, "u1", "Checkout")["group_id"]
        assert reg.assign_runs(ORG_A, "u1", False, [parent], gid) is True
        assert reg.assign_runs(ORG_A, "u2", False, [child], gid) is True

        assert reg.assign_runs(ORG_A, "u1", False, [parent], None) is True

        assert reg.get_run(parent)["group_id"] is None
        assert reg.get_run(child)["group_id"] is None, (
            "the re-run kept the test published after its owner un-filed it")

    def test_an_ungrouped_rerun_is_never_dragged_along(self, reg):
        """Monotonic: the cascade only ever REDUCES exposure. A re-run its
        owner deliberately kept out of the folder stays out."""
        parent, child = str(uuid.uuid4()), str(uuid.uuid4())
        self._seed(reg, parent, "u1")
        self._seed(reg, child, "u2", rerun_of=parent)
        gid = reg.create_group(ORG_A, "u1", "Checkout")["group_id"]
        assert reg.assign_runs(ORG_A, "u1", False, [parent], gid) is True

        assert reg.assign_runs(ORG_A, "u1", False, [parent], None) is True

        assert reg.get_run(child)["group_id"] is None

    def test_a_rerun_filed_somewhere_else_stays_there(self, reg):
        """The re-run lives in its own folder, so it is not this test's copy
        to move — yanking it out would un-publish work nobody asked about."""
        parent, child = str(uuid.uuid4()), str(uuid.uuid4())
        self._seed(reg, parent, "u1")
        self._seed(reg, child, "u2", rerun_of=parent)
        a = reg.create_group(ORG_A, "u1", "A")["group_id"]
        b = reg.create_group(ORG_A, "u1", "B")["group_id"]
        assert reg.assign_runs(ORG_A, "u1", False, [parent], a) is True
        assert reg.assign_runs(ORG_A, "u2", False, [child], b) is True

        assert reg.assign_runs(ORG_A, "u1", False, [parent], None) is True

        assert reg.get_run(child)["group_id"] == b

    def test_filing_an_ungrouped_test_drags_no_rerun_in(self, reg):
        """The guard on the parent's OLD folder being non-NULL. Publishing a
        test must never publish a re-run of it that was not already shared."""
        parent, child = str(uuid.uuid4()), str(uuid.uuid4())
        self._seed(reg, parent, "u1")
        self._seed(reg, child, "u2", rerun_of=parent)
        gid = reg.create_group(ORG_A, "u1", "Checkout")["group_id"]

        assert reg.assign_runs(ORG_A, "u1", False, [parent], gid) is True

        assert reg.get_run(parent)["group_id"] == gid
        assert reg.get_run(child)["group_id"] is None

    def test_a_colocated_rerun_moves_between_folders_with_its_test(self, reg):
        parent, child = str(uuid.uuid4()), str(uuid.uuid4())
        self._seed(reg, parent, "u1")
        self._seed(reg, child, "u2", rerun_of=parent)
        a = reg.create_group(ORG_A, "u1", "A")["group_id"]
        b = reg.create_group(ORG_A, "u1", "B")["group_id"]
        assert reg.assign_runs(ORG_A, "u1", False, [parent], a) is True
        assert reg.assign_runs(ORG_A, "u2", False, [child], a) is True

        assert reg.assign_runs(ORG_A, "u1", False, [parent], b) is True

        assert reg.get_run(parent)["group_id"] == b
        assert reg.get_run(child)["group_id"] == b

    def test_the_cascade_does_not_count_towards_all_or_nothing(self, reg):
        """The child rows the cascade moves are NOT in run_ids, so folding
        their rowcount into the atomicity check would make every cascading
        move fail closed."""
        parent, child = str(uuid.uuid4()), str(uuid.uuid4())
        self._seed(reg, parent, "u1")
        self._seed(reg, child, "u1", rerun_of=parent)
        a = reg.create_group(ORG_A, "u1", "A")["group_id"]
        assert reg.assign_runs(ORG_A, "u1", False, [parent, child], a) is True

        assert reg.assign_runs(ORG_A, "u1", False, [parent], None) is True
        assert reg.get_run(parent)["group_id"] is None
        assert reg.get_run(child)["group_id"] is None

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
    # The folder that EXISTS, not the casing the caller typed — the message
    # is what they use to go and find it.
    assert r.json()["detail"] == 'A group named "Checkout" already exists'

    r = client.patch(f"/api/groups/{gid}", json={"name": "Payments"}, headers=_auth(tok))
    assert r.status_code == 200 and r.json()["name"] == "Payments"

    # A solo user is org_admin of their own personal org, so deleting their
    # own folder is not blocked by the org_admin-only delete rule.
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
    assert r.json()["detail"] == 'A group named "Alpha" already exists'
    listed = client.get("/api/groups", headers=_auth(tok)).json()["groups"]
    assert sorted(g["name"] for g in listed) == ["Alpha", "Beta"]


def test_group_name_validation(client):
    tok = _register(client, f"gv-{uuid.uuid4().hex[:8]}@e.com")
    assert client.post("/api/groups", json={"name": "   "}, headers=_auth(tok)).status_code == 400
    assert client.post("/api/groups", json={"name": "x" * 61}, headers=_auth(tok)).status_code == 400


def test_groups_are_invisible_and_immutable_across_orgs(client):
    """Two separately registered users sit in their own personal orgs, so
    neither sees the other's folders."""
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
    """A run pointing at a folder outside the caller's org reports null for
    BOTH group fields — never the folder's name, and never a dangling id."""
    from src.backend.auth.jwt_utils import decode_token
    from src.backend.core.run_registry import get_run_registry

    _tok_a, tok_b, _org_id = _team_of_two(client)
    uid_b = decode_token(tok_b)["user_id"]
    reg = get_run_registry()
    rid = _seed_run_for(client, tok_b)
    foreign = reg.create_group("org-not-theirs", uid_b, "Elsewhere")["group_id"]
    with reg._pool.connection() as conn:
        conn.execute("UPDATE test_runs SET group_id = %s WHERE run_id = %s",
                     (foreign, rid))

    body = client.get(f"/api/history/{rid}", headers=_auth(tok_b)).json()
    assert body["group_id"] is None and body["group_name"] is None


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
    That branch belongs to the platform admin alone;
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


def test_platform_admin_does_not_see_another_orgs_folder(client):
    """INVERTED CONTRACT (2026-08-21): a platform admin does NOT see every
    org's folders, which is what this test asserted before that date. Their
    runs span every org; their folders are their own org's, so read and write
    finally agree — see test_platform_admin_folder_view_is_their_own_org for
    the full rule."""
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
    foreign_second = client.post(
        "/api/groups", json={"name": f"Second {uuid.uuid4().hex[:6]}"},
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
    assert foreign_second["name"] not in names, "no foreign folder name may leak"


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


def _forget_org_id(run_id: str) -> None:
    """Make a run look like a row written before the org_id backfill: owned by
    a user, but carrying no org. record_start cannot produce that shape for a
    user who has a membership — it derives the org itself — so the column is
    cleared directly."""
    import psycopg
    from src.backend.core.run_registry import get_run_registry

    with psycopg.connect(get_run_registry().dsn, autocommit=True) as conn:
        conn.execute("UPDATE test_runs SET org_id = NULL WHERE run_id = %s",
                     (run_id,))


def test_platform_admin_folder_chip_equals_its_filtered_table(client):
    """The chip and the folder-filtered table must describe ONE set for a
    platform admin too — their runs span every org while their folders are
    their own org's, so the count and the filter read two different scopes and
    have to be kept in step deliberately.

    A run with NO org is the awkward member of that set, and it is now
    excluded at the WRITE instead: a folder is org-keyed, so an org-less run
    (AUTH_ENFORCED off, or a row that predates the org backfill) has no org to
    match and cannot be published at all. Refusing it is what keeps grouping
    from meaning "visible to an org" for a row that belongs to none.
    """
    import jwt as _jwt
    from src.backend.auth.jwt_utils import create_access_token
    from src.backend.core.config import settings

    admin = _register(client, f"pafc-{uuid.uuid4().hex[:8]}@e.com")
    attributed = _seed_run_for(client, admin, query="has an org")
    legacy = _seed_run_for(client, admin, query="predates the org backfill")
    _forget_org_id(legacy)
    gid = client.post("/api/groups", json={"name": f"Mixed {uuid.uuid4().hex[:6]}"},
                      headers=_auth(admin)).json()["group_id"]

    # All-or-nothing, so the batch carrying the org-less row files nothing.
    assert client.put(
        "/api/groups/assignments",
        json={"run_ids": [attributed, legacy], "group_id": gid},
        headers=_auth(admin)).status_code == 404
    # The attributed run alone goes in fine.
    assert client.put(
        "/api/groups/assignments",
        json={"run_ids": [attributed], "group_id": gid},
        headers=_auth(admin)).status_code == 200

    claims = _jwt.decode(admin, settings.JWT_SECRET_KEY, algorithms=["HS256"])
    admin_tok = create_access_token({
        "id": claims["sub"], "email": claims["email"], "role": "admin",
        "display_name": "", "org_id": claims["org_id"],
        "org_role": claims["org_role"], "status": "active",
        "token_version": claims.get("tv", 0),
    })

    chips = client.get("/api/groups", headers=_auth(admin_tok)).json()["groups"]
    chip = [g for g in chips if g["group_id"] == gid][0]["run_count"]
    page = client.get(f"/api/history?group={gid}", headers=_auth(admin_tok)).json()
    assert {r["run_id"] for r in page["runs"]} == {attributed}
    assert chip == page["total"] == 1


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
# Integration: what a folder carries on the wire, empty ?group=, token-less
# mutations. There is no visibility field any more — a folder is the org's.
# ---------------------------------------------------------------------------

def test_create_carries_created_by_and_no_visibility(client):
    """created_by is what the chip row draws Edit/Delete from, so it has to
    reach the client on the create response AND on the list. visibility is
    gone from both — its absence is the contract now."""
    from src.backend.auth.jwt_utils import decode_token

    tok = _register(client, f"cb-{uuid.uuid4().hex[:8]}@e.com")
    uid = decode_token(tok)["user_id"]

    r = client.post("/api/groups", json={"name": "Checkout"}, headers=_auth(tok))
    assert r.status_code == 201, r.text
    assert r.json()["created_by"] == uid
    assert "visibility" not in r.json()

    listed = client.get("/api/groups", headers=_auth(tok)).json()["groups"]
    folder = next(g for g in listed if g["group_id"] == r.json()["group_id"])
    assert folder["created_by"] == uid
    assert "visibility" not in folder


def test_a_taken_folder_name_is_reported_to_the_second_member(client):
    """Decision D2: names belong to the org, so the SECOND person to reach for
    one is told it already exists rather than quietly getting a duplicate —
    which is what keeps one testcase on one folder name."""
    tok_a, tok_b, _org = _team_of_two(client)
    assert client.post("/api/groups", json={"name": "Login"},
                       headers=_auth(tok_a)).status_code == 201

    r = client.post("/api/groups", json={"name": "login"}, headers=_auth(tok_b))
    assert r.status_code == 409, r.text
    # Named as the COLLEAGUE spelled it, which is how it appears in the list
    # the second member is about to go and look at.
    assert r.json()["detail"] == 'A group named "Login" already exists'
    # And the folder they were told about is one they can actually use.
    listed = client.get("/api/groups", headers=_auth(tok_b)).json()["groups"]
    assert [g["name"] for g in listed] == ["Login"]


def test_patch_with_an_empty_body_is_400(client):
    """No name is nothing to do — a 400, not the registry's ValueError
    surfacing as a 500."""
    tok = _register(client, f"eb-{uuid.uuid4().hex[:8]}@e.com")
    gid = client.post("/api/groups", json={"name": "Untouched"},
                      headers=_auth(tok)).json()["group_id"]

    r = client.patch(f"/api/groups/{gid}", json={}, headers=_auth(tok))
    assert r.status_code == 400, r.text
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


def test_the_delete_flag_the_ui_reads_agrees_with_the_server(client):
    """The auth payload must carry a flag that is True exactly where
    DELETE /api/groups/{id} answers 204 — and it is not is_org_admin.

    Two different meanings of "org admin" live in this codebase and the
    personal org falls between them. is_org_admin is is_team_admin(), which
    is `o.kind = 'team'` only; folder authority is the JWT's org_role, and
    ensure_personal_org seats EVERY user as org_admin of their own personal
    org. So a solo user deletes their own folders (204) while is_org_admin
    is False for them, and the SPA — which gated its Delete control on
    is_org_admin — drew nothing. Every new signup lived in that state.

    The expectations here are not hard-coded against the flag: they are the
    DELETE status codes this same run observed, so the flag is pinned to the
    server's actual answer rather than to a second copy of the rule.
    """
    from src.backend.auth.jwt_utils import decode_token
    from src.backend.auth.org_repository import OrgRepository

    email_a = f"fd-a-{uuid.uuid4().hex[:8]}@e.com"     # team org_admin
    email_b = f"fd-b-{uuid.uuid4().hex[:8]}@e.com"     # plain team member
    email_s = f"fd-s-{uuid.uuid4().hex[:8]}@e.com"     # solo, personal org
    uid_a = decode_token(_register(client, email_a))["user_id"]
    uid_b = decode_token(_register(client, email_b))["user_id"]
    _register(client, email_s)
    org_id = OrgRepository().create_team_org(f"Acme {uuid.uuid4().hex[:6]}", uid_a)
    OrgRepository().add_member(org_id, uid_b)

    # Log in AFTER the membership change — claims are minted at login.
    payload, tok = {}, {}
    for email in (email_a, email_b, email_s):
        r = client.post("/auth/login", json={"email": email, "password": "S3cretpw!"})
        assert r.status_code == 200, r.text
        payload[email] = r.json()["user"]
        tok[email] = r.json()["access_token"]

    # What the server actually does. The member is refused BEFORE the admin
    # deletes the folder, so both answers are about the same live folder.
    team_gid = client.post("/api/groups", json={"name": f"T{uuid.uuid4().hex[:6]}"},
                           headers=_auth(tok[email_a])).json()["group_id"]
    solo_gid = client.post("/api/groups", json={"name": f"S{uuid.uuid4().hex[:6]}"},
                           headers=_auth(tok[email_s])).json()["group_id"]
    may_delete = {
        email_b: client.delete(f"/api/groups/{team_gid}",
                               headers=_auth(tok[email_b])).status_code == 204,
        email_s: client.delete(f"/api/groups/{solo_gid}",
                               headers=_auth(tok[email_s])).status_code == 204,
        email_a: client.delete(f"/api/groups/{team_gid}",
                               headers=_auth(tok[email_a])).status_code == 204,
    }
    assert may_delete == {email_a: True, email_b: False, email_s: True}

    # The login payload and /auth/me must both report exactly that.
    from_login = {e: payload[e].get("can_manage_org_folders") for e in may_delete}
    assert from_login == may_delete
    from_me = {
        e: client.get("/auth/me", headers=_auth(tok[e])).json().get("can_manage_org_folders")
        for e in may_delete
    }
    assert from_me == may_delete

    # is_org_admin keeps its NARROWER team-org meaning: it gates the Team page
    # and the Team nav item, and widening it would hand both to every solo
    # signup. This is the guard against a future "simplification".
    team_admin = {e: payload[e].get("is_org_admin") for e in may_delete}
    assert team_admin == {email_a: True, email_b: False, email_s: False}
    from_me_team = {
        e: client.get("/auth/me", headers=_auth(tok[e])).json().get("is_org_admin")
        for e in may_delete
    }
    assert from_me_team == team_admin


def test_history_scope_all_is_not_a_platform_admin_signal(client):
    """F13: /api/history answers scope='all' to any org_admin, personal org
    included — it means "no per-user narrowing", not "every user on the
    platform". A solo signup is org_admin of their own org, so they get it too.

    The SPA printed "All users' test runs (admin view)" off that field alone,
    which told an ordinary user they were looking at everyone's runs. The DATA
    is right (their org is just them); only the label was wrong. Nothing about
    the response changes — this pins the pair of facts the subtitle has to be
    read from: scope says how wide the filter is, role says who the caller is.
    """
    tok = _register(client, f"f13-{uuid.uuid4().hex[:8]}@e.com")
    _seed_run_for(client, tok)

    body = client.get("/api/history", headers=_auth(tok)).json()
    me = client.get("/auth/me", headers=_auth(tok)).json()
    assert body["scope"] == "all"          # org-wide, and their org is themselves
    assert me["role"] == "user"            # NOT a platform admin
    assert me["is_org_admin"] is False     # NOT even a team org admin

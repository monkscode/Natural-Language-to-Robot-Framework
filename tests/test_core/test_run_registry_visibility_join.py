"""Folder membership now belongs to the test, so every read reaches run_groups
through tests. The predicate itself is unchanged, character for character —
only which row's group_id the join reads.
"""
import uuid

import psycopg
import pytest

from src.backend.core.config import settings

pytestmark = pytest.mark.integration

OWNER = {"user_id": "alice", "org_id": "org-a", "email": "a@x.com"}


@pytest.fixture
def reg():
    name = f"tests_vis_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    admin.execute(f"CREATE SCHEMA {name}")
    admin.execute(f"SET search_path TO {name}, public")
    sep = "&" if "?" in settings.DATABASE_URL else "?"
    dsn = settings.DATABASE_URL + f"{sep}options=-c%20search_path%3D{name}"
    from src.backend.core.run_registry import RunRegistry
    r = RunRegistry(dsn=dsn)
    try:
        yield r, admin
    finally:
        r.close()
        admin.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
        admin.close()


def _folder(admin, folder_id, org="org-a"):
    admin.execute(
        "INSERT INTO run_groups (group_id, name, org_id, created_by)"
        " VALUES (%s, %s, %s, 'alice') ON CONFLICT DO NOTHING",
        (folder_id, "Regression", org))


def _file_test_into_folder(admin, run_id, folder_id, org="org-a"):
    """Put the RUN'S TEST into a folder — the P1 model."""
    _folder(admin, folder_id, org)
    admin.execute(
        "UPDATE tests SET group_id = %s WHERE test_id ="
        " (SELECT test_id FROM test_runs WHERE run_id = %s)",
        (folder_id, run_id))


def _test_id(admin, run_id):
    return admin.execute(
        "SELECT test_id FROM test_runs WHERE run_id = %s",
        (run_id,)).fetchone()[0]


def _test_folders(admin):
    return [r[0] for r in admin.execute(
        "SELECT group_id FROM tests ORDER BY key_n").fetchall()]


def test_a_peer_sees_a_run_whose_TEST_is_filed(reg):
    """The run carries no folder of its own, so a read still looking at
    test_runs.group_id finds nothing and hides the row. Publication is the
    test's now, and the peer must see it through that."""
    r, admin = reg
    r.record_start("run-1", OWNER, "q", "generated", robot_code="c")
    _file_test_into_folder(admin, "run-1", "g-1")

    rows, total = r.list_runs(user_id="bob", org_id="org-a",
                              folder_org_id="org-a")
    assert [x["run_id"] for x in rows] == ["run-1"]
    assert total == 1


def test_an_unfiled_run_stays_private_to_its_owner(reg):
    """Nothing published it, so the org term of the predicate stays false and
    only the owner reads it."""
    r, admin = reg
    r.record_start("run-1", OWNER, "q", "generated", robot_code="c")

    rows, total = r.list_runs(user_id="bob", org_id="org-a",
                              folder_org_id="org-a")
    assert rows == [] and total == 0

    mine, mine_total = r.list_runs(user_id="alice", org_id="org-a",
                                   folder_org_id="org-a")
    assert [x["run_id"] for x in mine] == ["run-1"] and mine_total == 1


def test_a_foreign_orgs_folder_publishes_nothing(reg):
    """The whole org term lives in the join, which binds the CALLER's org, so
    a test filed into another org's folder resolves no folder at all."""
    r, admin = reg
    r.record_start("run-1", OWNER, "q", "generated", robot_code="c")
    _file_test_into_folder(admin, "run-1", "g-1", org="org-OTHER")

    rows, total = r.list_runs(user_id="bob", org_id="org-a",
                              folder_org_id="org-a")
    assert rows == [] and total == 0


def test_get_run_owner_agrees_with_the_list_about_publication(reg):
    """THE reason this is one commit: get_run_owner anchors /reports while the
    list anchors History. A window where one hops and the other does not
    offers a row that the other endpoint then refuses."""
    r, admin = reg
    r.record_start("run-1", OWNER, "q", "generated", robot_code="c")
    _file_test_into_folder(admin, "run-1", "g-1")

    own = r.get_run_owner("run-1")
    assert own.user_id == "alice"
    assert own.org_id == "org-a"
    assert own.group_id == "g-1"


def test_count_ungrouped_agrees_with_the_ungrouped_filter(reg):
    """The chip and the Ungrouped table are one set, and they stay one set
    after the hop because both read g.group_id off the same join."""
    r, admin = reg
    r.record_start("run-1", OWNER, "q1", "generated", robot_code="c")
    r.record_start("run-2", OWNER, "q2", "generated", robot_code="c")
    _file_test_into_folder(admin, "run-1", "g-1")

    n = r.count_ungrouped("alice", "org-a", folder_org_id="org-a")
    rows, total = r.list_runs(user_id="alice", org_id="org-a",
                              folder_org_id="org-a", group="ungrouped")
    assert n == total == len(rows) == 1
    assert rows[0]["run_id"] == "run-2"


def test_the_visible_run_predicate_is_unchanged(reg):
    """The hop moves which row's group_id the JOIN reads. The predicate itself
    must not move a character, or every property its docstring argues for has
    to be re-argued."""
    from src.backend.core.run_registry import _VISIBLE_RUN_SQL

    assert _VISIBLE_RUN_SQL == (
        "(t.user_id = %s"
        " OR (g.group_id IS NOT NULL AND t.user_id IS NOT NULL))"
    )


def test_a_visible_test_predicate_exists_for_reads_over_tests(reg):
    """A read whose ROWS are tests has no result in scope, so it cannot bind
    t.user_id. Same sentence, same fail-closed IS NOT NULL term, over `te`."""
    from src.backend.core.run_registry import _VISIBLE_TEST_SQL

    assert _VISIBLE_TEST_SQL == (
        "(te.user_id = %s"
        " OR (g.group_id IS NOT NULL AND te.user_id IS NOT NULL))"
    )


def test_a_run_with_no_test_is_still_published_by_its_own_folder(reg):
    """The transitional fallback. A run recorded with no robot_code never
    creates a test (owner decision D8) and test_id IS NULL is a permanently
    legal state — so the join must still fall back to the run's own column, or
    such a row could be filed and never published: a silent no-op in the UI.

    This is what keeps the ~40 existing folder tests green, every one of which
    seeds runs without robot_code. P2 removes both the fallback and this test.
    """
    r, admin = reg
    r.record_start("run-1", OWNER, "q", "generated")
    assert _test_id(admin, "run-1") is None, (
        "premise broken: a code-less run now creates a test, so the fallback "
        "this test pins may no longer be the thing keeping such rows visible")

    _folder(admin, "g-1")
    admin.execute("UPDATE test_runs SET group_id = 'g-1' WHERE run_id = 'run-1'")

    rows, total = r.list_runs(user_id="bob", org_id="org-a",
                              folder_org_id="org-a")
    assert [x["run_id"] for x in rows] == ["run-1"] and total == 1


def test_get_run_owner_falls_back_to_the_runs_own_folder(reg):
    """get_run_owner hand-writes the same resolution, so it owes the same
    fallback — otherwise /reports refuses a row History publishes."""
    r, admin = reg
    r.record_start("run-1", OWNER, "q", "generated")
    assert _test_id(admin, "run-1") is None

    _folder(admin, "g-1")
    admin.execute("UPDATE test_runs SET group_id = 'g-1' WHERE run_id = 'run-1'")

    assert r.get_run_owner("run-1").group_id == "g-1"


def test_filing_a_run_files_its_test(reg):
    """Publication belongs to the test, so the WRITE has to move the test.
    A read hop with no write move unpublishes every newly filed run while the
    folder chip still counts it."""
    r, admin = reg
    r.record_start("run-1", OWNER, "q", "generated", robot_code="c")
    r.record_start("run-2", OWNER, "q", "running", robot_code="c",
                   rerun_of="run-1")
    assert _test_id(admin, "run-1") == _test_id(admin, "run-2") is not None
    _folder(admin, "g-1")

    assert r.assign_runs("org-a", "alice", False, ["run-1"], "g-1") is True
    assert _test_folders(admin) == ["g-1"]


def test_filing_one_result_publishes_its_siblings(reg):
    """The target model, and a deliberate consequence of P1: filing ONE result
    of a test publishes that test's other results too.

    It moves no number on any current screen — every run the existing suite
    files has no test at all, and the migration carried each test's folder
    forward from the newest run that had one."""
    r, admin = reg
    r.record_start("run-1", OWNER, "q", "generated", robot_code="c")
    r.record_start("run-2", OWNER, "q", "running", robot_code="c",
                   rerun_of="run-1")
    _folder(admin, "g-1")
    assert r.assign_runs("org-a", "alice", False, ["run-1"], "g-1") is True

    rows, total = r.list_runs(user_id="bob", org_id="org-a",
                              folder_org_id="org-a")
    assert {x["run_id"] for x in rows} == {"run-1", "run-2"} and total == 2

    folder = [g for g in r.list_groups(folder_org_id="org-a",
                                       run_org_id="org-a")
              if g["group_id"] == "g-1"][0]
    assert folder["run_count"] == 2
    assert folder["test_count"] == 1


def test_filing_is_refused_without_authority_and_moves_no_test(reg):
    """The new UPDATE is driven off the SAME authority filter as the parent
    one and lives inside the same transaction, so a refused batch moves no
    test either — not merely no run."""
    r, admin = reg
    r.record_start("run-x", {"user_id": "carol", "org_id": "org-a",
                             "email": "c@x.com"},
                   "q", "generated", robot_code="c")
    _folder(admin, "g-1")

    assert r.assign_runs("org-a", "alice", False, ["run-x"], "g-1") is False
    assert _test_folders(admin) == [None]
    assert admin.execute(
        "SELECT group_id FROM test_runs WHERE run_id = 'run-x'"
    ).fetchone()[0] is None


def test_ungrouping_a_run_unfiles_its_test(reg):
    """Ungrouping travels the same path as filing, so it has to clear the
    test's folder as well as the run's."""
    r, admin = reg
    r.record_start("run-1", OWNER, "q", "generated", robot_code="c")
    _folder(admin, "g-1")
    assert r.assign_runs("org-a", "alice", False, ["run-1"], "g-1") is True
    assert _test_folders(admin) == ["g-1"]

    assert r.assign_runs("org-a", "alice", False, ["run-1"], None) is True
    assert _test_folders(admin) == [None]
    assert admin.execute(
        "SELECT group_id FROM test_runs WHERE run_id = 'run-1'"
    ).fetchone()[0] is None


def test_count_ungrouped_tests_counts_tests_not_results(reg):
    """The Ungrouped chip for the TESTS page counts TESTS. Two results of one
    test are one row, not two — which a count over test_runs cannot say."""
    r, admin = reg
    r.record_start("run-1", OWNER, "q1", "generated", robot_code="c")
    r.record_start("run-2", OWNER, "q1", "running", robot_code="c",
                   rerun_of="run-1")
    r.record_start("run-3", OWNER, "q2", "generated", robot_code="c")
    assert _test_id(admin, "run-1") == _test_id(admin, "run-2")
    assert _test_id(admin, "run-3") != _test_id(admin, "run-1")

    assert r.count_ungrouped_tests("alice", "org-a",
                                   folder_org_id="org-a") == 2

    _file_test_into_folder(admin, "run-1", "g-1")
    assert r.count_ungrouped_tests("alice", "org-a",
                                   folder_org_id="org-a") == 1

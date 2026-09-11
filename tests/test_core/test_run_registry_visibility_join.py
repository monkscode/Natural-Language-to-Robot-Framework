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
    """The COALESCE fallback. A run recorded with no robot_code never creates
    a test (owner decision D8) and test_id IS NULL is a permanently legal
    state — so the join must still fall back to the run's own column, or such
    a row could be filed and never published: a silent no-op in the UI.

    It is what keeps the existing folder tests green: MOST of them seed runs
    without robot_code, though not all — test_rerun_group_inheritance.py's
    _seed_run passes it, which is what gives those runs a test at all. The
    fallback is not transitional either: assign_runs files by run_id and needs
    no test, so while D8 stands "every run has a test" is unreachable and
    neither the fallback nor this test goes away (see _group_join).
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

    It moves no number on any CURRENT screen: the owner's database holds 0
    folders and 46 unfiled runs, and the migration carried each test's
    folder forward from the newest run that had one. It does move one here —
    this test files a run that HAS a test, which is the point."""
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
    """A refused batch moves no TEST either, not merely no run.

    What this pins is the TRANSACTION, not the authority filter. Deleting
    `AND r.{allowed}` from the new UPDATE leaves this test green: the parent
    UPDATE's rowcount check still rolls the whole transaction back, and the
    new statement is joined to rows already restricted to the batch. The
    filter is defence in depth — it keeps the statement correct on its own if
    that check is ever relaxed — and is not what this test proves. What it
    does catch is the new UPDATE being moved outside the transaction, or the
    rollback being dropped."""
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


def test_count_ungrouped_tests_excludes_unowned_when_not_included(reg):
    """The include_unowned=False branch: te.user_id IS NOT NULL. Mirrors
    count_ungrouped's _OWNED_RUN_SQL rule on the tests side — only a platform
    admin (or the token-less caller) may read an unattributed test, so only
    they may count it."""
    r, admin = reg
    r.record_start("run-1", OWNER, "q1", "generated", robot_code="c")
    # No user_id, a concrete org: the shape AUTH_ENFORCED=false or a legacy
    # row leaves behind — attributed to an org with no owning user.
    r.record_start("run-2", {"org_id": "org-a"}, "q2", "generated",
                   robot_code="c")
    assert admin.execute(
        "SELECT user_id, org_id FROM tests WHERE user_query = 'q2'"
    ).fetchone() == (None, "org-a"), "premise: an unattributed test in org-a"

    assert r.count_ungrouped_tests(org_id="org-a", folder_org_id="org-a",
                                   include_unowned=True) == 2
    assert r.count_ungrouped_tests(org_id="org-a", folder_org_id="org-a",
                                   include_unowned=False) == 1


def test_count_ungrouped_tests_for_an_identified_caller_with_no_org(reg):
    """_group_join_for_tests(None, identified=True) resolves ON FALSE: an
    identified caller who owns no org can resolve NO folder at all — not
    even one that happens to file their own test — the same rule
    ownership.py rule 4 states for runs. Filing nomad's test must therefore
    NOT drop it out of nomad's own Ungrouped count: an unfiltered join
    (g.group_id = te.group_id, no org term — the token-less caller's form)
    would instead resolve the real folder and remove it from this count,
    which is the failure this test is built to catch."""
    r, admin = reg
    r.record_start("run-1", {"user_id": "nomad"}, "q1", "generated",
                   robot_code="c")
    r.record_start("run-2", OWNER, "q2", "generated", robot_code="c")
    assert admin.execute(
        "SELECT org_id FROM tests WHERE user_id = 'nomad'"
    ).fetchone()[0] is None, "premise: nomad's test really has no org"

    assert r.count_ungrouped_tests(user_id="nomad") == 1

    _file_test_into_folder(admin, "run-1", "g-1", org="org-a")
    assert r.count_ungrouped_tests(user_id="nomad") == 1, (
        "nomad owns no org, so no folder is theirs to resolve — their own "
        "test must still read as Ungrouped even though it is now filed")


@pytest.mark.xfail(
    raises=AssertionError, strict=True, reason=(
        "Confirmed divergence, not yet resolved: Task 2's platform-admin "
        "write-back guard (owner ruling, 2026-09-08) leaves a cross-org-"
        "rerun's TEST permanently org-less and unowned while the RUN takes "
        "the admin's own org, so count_ungrouped and count_ungrouped_tests "
        "disagree for that caller. Left failing on purpose as the regression "
        "net section 5a asks for; do not widen _VISIBLE_TEST_SQL/"
        "_VISIBLE_RUN_SQL or change the guard to force this green — the "
        "owner is deciding the guard's consequences separately. strict=True "
        "reports an unexpected pass (the day this is fixed) as a failure "
        "instead of silently going green. raises=AssertionError additionally "
        "means an exception of any OTHER type -- e.g. a schema-drift crash "
        "inside count_ungrouped_tests -- also fails loudly rather than being "
        "absorbed as this expected divergence; it does not distinguish "
        "which assert below raised, only that the raise was an "
        "AssertionError."))
def test_visible_tests_and_visible_runs_agree_across_a_cross_org_rerun(reg):
    """Spec section 5a's regression net: _VISIBLE_RUN_SQL and _VISIBLE_TEST_SQL
    must agree, or a result a caller can see could belong to a test absent
    from their Tests page. _VISIBLE_TEST_SQL now has TWO production entry
    points -- count_ungrouped_tests and list_tests (served at /api/tests) --
    the same count _VISIBLE_RUN_SQL's callers (count_ungrouped and list_runs)
    already had. This test pins agreement between the pair it can exercise
    with a plain count comparison: count_ungrouped and count_ungrouped_tests,
    the pair this task wires side by side onto /api/groups. The list_runs/
    list_tests pair carries the identical divergence (same _VISIBLE_*_SQL
    predicates) and is NOT pinned here -- a second regression test over full
    row lists, not just counts, would be needed to cover it. It covers the
    caller shape section 5a names explicitly: a platform admin's cross-org
    re-run.

    Built on the same shape as
    test_a_platform_admin_does_not_write_back_the_org_on_the_rerun_branch
    (tests/test_core/test_run_registry_test_attach.py): a token-less run
    mints an org-less, unowned test; a platform admin then reruns it
    cross-org. D6 makes the RUN take the admin's own org either way, but
    Task 2's write-back guard (owner ruling, 2026-09-08) skips repairing the
    TEST when the caller acts under platform-admin authority, so the test
    stays org-less and unowned forever while the run does not.

    Section 5a's invariant is an IMPLICATION — a visible result's test must
    itself be visible, and the reverse — not a count identity, and the final
    `visible_tests == visible_runs` below is a correct proxy for it only
    because this fixture's scoped caller ("root", an identified member of
    org-ADMIN) has exactly one candidate result and one candidate test in
    view. It is not a general law: at the caller shape /api/groups actually
    passes a validated platform admin (user_id=None, org_id=None,
    include_unowned=True), the same two functions return 2 and 1 on this
    same data — two results of the one test, which is the ordinary
    multiplicity ungrouped_test_count exists to represent, not a violation.
    """
    r, admin = reg
    r.record_start("run-1", None, "q", "generated", robot_code="code-1")
    test_id = _test_id(admin, "run-1")
    assert admin.execute(
        "SELECT user_id, org_id FROM tests WHERE test_id = %s",
        (test_id,)).fetchone() == (None, None), (
        "premise: the test is org-less and unowned")

    admin_user = {"user_id": "root", "org_id": "org-ADMIN", "email": "r@x.com"}
    r.record_start("run-2", admin_user, "q", "running", robot_code="code-1",
                   rerun_of="run-1", is_platform_admin=True)
    assert admin.execute(
        "SELECT org_id FROM test_runs WHERE run_id = 'run-2'"
    ).fetchone()[0] == "org-ADMIN", (
        "premise: the RUN took the admin's own org (D6)")
    assert admin.execute(
        "SELECT org_id FROM tests WHERE test_id = %s",
        (test_id,)).fetchone()[0] is None, (
        "premise: the write-back guard left the TEST org-less")

    visible_runs = r.count_ungrouped("root", "org-ADMIN",
                                     folder_org_id="org-ADMIN")
    visible_tests = r.count_ungrouped_tests("root", "org-ADMIN",
                                            folder_org_id="org-ADMIN")
    assert visible_runs == 1, (
        "premise: root's own re-run is in root's Ungrouped run count")
    assert visible_tests == visible_runs, (
        f"root can see a result (count_ungrouped={visible_runs}) belonging "
        f"to a test root cannot see (count_ungrouped_tests={visible_tests}) "
        "-- the cross-org-rerun divergence section 5a says D6 removes is, "
        "for this platform-admin caller shape, preserved by Task 2's "
        "write-back guard instead")


def test_deleting_a_folder_audits_runs_filed_only_through_their_test(reg):
    """delete_group's audit list has to resolve membership the way every read
    now does. A run whose OWN group_id is NULL is still a member when its test
    is filed — that is exactly what filing one result of a test produces, and
    what the migration leaves on a test's other runs. ON DELETE SET NULL
    correctly returns such a run to Ungrouped, so an audit that cannot see it
    understates the blast radius of a destructive org-admin action."""
    r, admin = reg
    r.record_start("run-1", OWNER, "q", "generated", robot_code="c")
    r.record_start("run-2", OWNER, "q", "running", robot_code="c",
                   rerun_of="run-1")
    _folder(admin, "g-1")
    assert r.assign_runs("org-a", "alice", False, ["run-1"], "g-1") is True
    assert admin.execute(
        "SELECT group_id FROM test_runs WHERE run_id = 'run-2'"
    ).fetchone()[0] is None, "run-2 must be a member through its TEST only"

    audit: list[str] = []
    assert r.delete_group("org-a", "alice", True, "g-1",
                          audit_run_ids=audit) is True
    assert sorted(audit) == ["run-1", "run-2"]


def test_deleting_a_folder_does_not_audit_a_run_from_a_different_org(reg):
    """F7: a test's runs can span two orgs (a documented, ordinary-flow-
    reachable gap -- see _attach_test's NULL-org fallback paragraph in
    run_registry.py). An org-a admin filing their OWN org-a run
    publishes the whole shared test, and a test-branch with no org term at
    all then names a run the caller has no authority over at all.

    Not one no read path shows: that claim is false and was retracted from
    delete_group's docstring. get_run_owner anchors g.org_id = t.org_id and
    list_runs filters t.org_id = %s, so every caller whose row filter binds
    a concrete org is excluded -- but a validated PLATFORM ADMIN calls
    list_runs with org_id None (the admin branch of history_scope()), which is
    exactly that filter, so GET /api/history?group=<this folder> does list
    the foreign run. The org term on the audit rests on the naming argument
    in delete_group's docstring instead: an audit of a destructive action
    must not name another org's run_id, whoever else can see the row."""
    r, admin = reg
    r.record_start("run-a", OWNER, "q", "generated", robot_code="c")
    shared_test = _test_id(admin, "run-a")
    # The write paths in this file cannot produce this shape within one
    # test's lifetime: D6 makes a rerun of a test with a concrete org take
    # THAT org, never the caller's, and a rerun of an org-less test now
    # writes the caller's org back onto `tests` itself (since 2026-09-08)
    # instead of leaving it to happen again on a later rerun -- seeded
    # directly to exercise the audit query against the documented gap, not
    # to re-derive how a live system reaches it.
    admin.execute(
        "INSERT INTO test_runs (run_id, user_id, user_email, user_query,"
        " status, org_id, test_id)"
        " VALUES ('run-b', 'zoe', 'z@x.com', 'q', 'generated', 'org-b', %s)",
        (shared_test,))
    assert admin.execute(
        "SELECT org_id FROM test_runs WHERE run_id = 'run-b'"
    ).fetchone()[0] == "org-b", "premise: run-b is a different, concrete org"

    _folder(admin, "g-1")
    assert r.assign_runs("org-a", "alice", False, ["run-a"], "g-1") is True
    assert r.get_run_owner("run-b").group_id is None, (
        "premise: get_run_owner does not resolve g-1 for run-b -- it anchors "
        "g.org_id = t.org_id. A platform admin's list_runs still would")

    audit: list[str] = []
    assert r.delete_group("org-a", "alice", True, "g-1",
                          audit_run_ids=audit) is True
    assert audit == ["run-a"]


def test_deleting_a_folder_audits_an_org_less_run_filed_through_a_shared_test(reg):
    """F7's third property: a run with org_id IS NULL can never be filed
    directly -- assign_runs and record_start's group_id path both require a
    concrete org match -- but it can share a test with a run that IS filed,
    and the token-less dev caller's join binds no g.org_id term at all, so
    such a run genuinely displays in this folder for that caller (list_runs
    with no caller org applies no t.org_id filter either). Excluding it from
    the audit would under-report a blast radius that is real for that caller
    shape, so it stays admitted -- deliberately, not by the accident a bare
    `org_id = %s` or `IS NOT DISTINCT FROM %s` would have produced."""
    r, admin = reg
    r.record_start("run-a", OWNER, "q", "generated", robot_code="c")
    shared_test = _test_id(admin, "run-a")
    admin.execute(
        "INSERT INTO test_runs (run_id, user_id, user_email, user_query,"
        " status, org_id, test_id)"
        " VALUES ('run-none', 'zoe', 'z@x.com', 'q', 'generated', NULL, %s)",
        (shared_test,))
    assert admin.execute(
        "SELECT org_id FROM test_runs WHERE run_id = 'run-none'"
    ).fetchone()[0] is None, "premise: run-none really has no org"

    _folder(admin, "g-1")
    assert r.assign_runs("org-a", "alice", False, ["run-a"], "g-1") is True
    rows, _ = r.list_runs(group="g-1")
    assert "run-none" in {row["run_id"] for row in rows}, (
        "premise: the token-less caller really is shown run-none in g-1")

    audit: list[str] = []
    assert r.delete_group("org-a", "alice", True, "g-1",
                          audit_run_ids=audit) is True
    assert sorted(audit) == ["run-a", "run-none"]


def test_deleting_a_folder_does_not_audit_a_foreign_run_filed_directly(reg):
    """F9b: the org guard covers the DIRECT-COLUMN arm too, not only the
    test branch. record_start's rerun path can produce this exact shape --
    a foreign-org run whose OWN test_runs.group_id names this folder --
    without ever going through assign_runs: _fileable_group_id validates an
    inherited group_id against the caller's PRE-D6 org, called from
    record_start before its call to _attach_test; _attach_test's D6 rule
    (rerun_of branch) then reassigns the row's FINAL org_id to the shared
    test's own org, and record_start's own INSERT/UPSERT writes the
    pre-D6-checked group_id beside the post-D6 org_id with nothing
    re-validating the pair. Seeded directly by
    SQL here -- driving it through record_start needs a second org's own
    test/run graph built first -- but the shape itself is real, reachable
    on the rerun path just named, not hypothetical."""
    r, admin = reg
    r.record_start("run-a", OWNER, "q", "generated", robot_code="c")
    _folder(admin, "g-1")
    assert r.assign_runs("org-a", "alice", False, ["run-a"], "g-1") is True

    # The direct-column shape: run-x's OWN group_id names g-1 while its
    # own org_id is a different, concrete org, with no test at all linking
    # it to run-a -- so only a `group_id = %s` arm could ever see it.
    admin.execute(
        "INSERT INTO test_runs (run_id, user_id, user_email, user_query,"
        " status, org_id, group_id)"
        " VALUES ('run-x', 'zoe', 'z@x.com', 'q', 'generated', 'org-b',"
        " 'g-1')")
    assert admin.execute(
        "SELECT org_id, group_id, test_id FROM test_runs"
        " WHERE run_id = 'run-x'"
    ).fetchone() == ("org-b", "g-1", None), (
        "premise: run-x is a different, concrete org, filed on its own "
        "column directly, with no shared test involved at all")

    audit: list[str] = []
    assert r.delete_group("org-a", "alice", True, "g-1",
                          audit_run_ids=audit) is True
    assert audit == ["run-a"]
    # The accepted cost the docstring now states plainly: the FK cascade
    # still clears run-x's column, silently, even though the audit never
    # named it.
    assert admin.execute(
        "SELECT group_id FROM test_runs WHERE run_id = 'run-x'"
    ).fetchone()[0] is None

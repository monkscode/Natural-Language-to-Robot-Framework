"""RunRegistry.list_tests — the Tests page's per-test row + total.

Every row aggregates test_versions/test_runs for ONE test. Two different
scopes matter and this file pins the difference: result_count, pass_count,
last_status/last_run_at/last_run_id are WHOLE-TEST (every version, every
result the caller may open -- that narrowing is pinned in
test_run_registry_list_tests_scope.py); health, running and spark are scoped
to the CURRENT version only.
Getting the two scopes crossed is the exact defect the split exists to
remove (a test broken through v1-v9 and fixed at v10 must not render a
mostly-red sparkline).

Referenced by: none (registry unit tests only).
Depends on: src/backend/core/run_registry.py (list_tests, _VISIBLE_TEST_SQL,
_group_join_for_tests).
"""
import uuid

import psycopg
import pytest

from src.backend.core.config import settings

pytestmark = pytest.mark.integration

ORG_A = "org-a"
ORG_B = "org-b"


@pytest.fixture
def reg():
    name = f"tests_list_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    admin.execute(f"CREATE SCHEMA {name}")
    admin.execute(f"SET search_path TO {name}")
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


# ---------------------------------------------------------------------------
# Seeding helpers -- direct SQL, not record_start(). list_tests is a READER;
# these tests need exact multi-version / multi-result-per-version / precise-
# timestamp shapes record_start's write path cannot produce on its own (one
# record_start call mints at most one NEW version, never a second one for an
# existing test), the same reason test_run_registry_visibility_join.py's
# _file_test_into_folder writes straight to the tests table instead of going
# through assign_runs.
# ---------------------------------------------------------------------------

def _test_row(admin, test_id, *, key_n, user_id="alice", org_id=ORG_A,
              user_email="a@x.com", user_query="q", current_version=1,
              group_id=None, name=None):
    admin.execute(
        "INSERT INTO tests (test_id, org_id, key_n, user_id, user_email,"
        " user_query, current_version, group_id, name)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (test_id, org_id, key_n, user_id, user_email, user_query,
         current_version, group_id, name))


def _version_row(admin, test_id, n, robot_code="*** Tasks ***",
                  created_by="alice"):
    version_id = str(uuid.uuid4())
    admin.execute(
        "INSERT INTO test_versions (version_id, test_id, n, user_query,"
        " robot_code, created_by) VALUES (%s, %s, %s, %s, %s, %s)",
        (version_id, test_id, n, "q", robot_code, created_by))
    return version_id


def _run_row(admin, run_id, test_id, version_id, status, created_at,
             user_id="alice", org_id=ORG_A, user_email="a@x.com"):
    admin.execute(
        "INSERT INTO test_runs (run_id, user_id, user_email, user_query,"
        " status, org_id, test_id, test_version_id, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (run_id, user_id, user_email, "q", status, org_id, test_id,
         version_id, created_at))


def _folder(admin, folder_id, org=ORG_A, name="Regression"):
    admin.execute(
        "INSERT INTO run_groups (group_id, name, org_id, created_by)"
        " VALUES (%s, %s, %s, 'alice') ON CONFLICT DO NOTHING",
        (folder_id, name, org))


def _one_test_one_run(admin, *, status="passed", user_id="alice",
                       org_id=ORG_A, group_id=None, key_n=1):
    """The minimal shape: one test, one version, one result."""
    test_id = str(uuid.uuid4())
    _test_row(admin, test_id, key_n=key_n, user_id=user_id, org_id=org_id,
              group_id=group_id)
    version_id = _version_row(admin, test_id, 1)
    _run_row(admin, str(uuid.uuid4()), test_id, version_id, status,
             "2026-01-01T10:00:00Z", user_id=user_id, org_id=org_id)
    return test_id


# ---------------------------------------------------------------------------
# Visibility predicate: owner, published peer, foreign org, unowned row,
# no-org caller, token-less caller.
# ---------------------------------------------------------------------------

def test_owner_sees_their_own_unfiled_test(reg):
    r, admin = reg
    test_id = _one_test_one_run(admin)

    tests, total = r.list_tests(user_id="alice", org_id=ORG_A,
                                folder_org_id=ORG_A)
    assert total == 1
    assert tests[0]["test_id"] == test_id


def test_a_stranger_does_not_see_an_unfiled_test(reg):
    r, admin = reg
    _one_test_one_run(admin, user_id="alice")

    tests, total = r.list_tests(user_id="bob", org_id=ORG_A,
                                folder_org_id=ORG_A)
    assert total == 0
    assert tests == []


def test_a_published_peer_sees_a_test_filed_into_a_folder(reg):
    r, admin = reg
    folder_id = str(uuid.uuid4())
    _folder(admin, folder_id)
    test_id = _one_test_one_run(admin, user_id="alice", group_id=folder_id)

    tests, total = r.list_tests(user_id="bob", org_id=ORG_A,
                                folder_org_id=ORG_A)
    assert total == 1
    assert tests[0]["test_id"] == test_id
    assert tests[0]["group_id"] == folder_id
    assert tests[0]["group_name"] == "Regression"


def test_a_foreign_orgs_member_never_sees_a_filed_test(reg):
    r, admin = reg
    folder_id = str(uuid.uuid4())
    _folder(admin, folder_id, org=ORG_A)
    _one_test_one_run(admin, user_id="alice", org_id=ORG_A,
                       group_id=folder_id)

    tests, total = r.list_tests(user_id="carol", org_id=ORG_B,
                                folder_org_id=ORG_B)
    assert total == 0
    assert tests == []


def test_an_unowned_row_is_invisible_to_an_ordinary_identified_caller(reg):
    r, admin = reg
    _one_test_one_run(admin, user_id=None, org_id=ORG_A)

    tests, total = r.list_tests(user_id="alice", org_id=ORG_A,
                                folder_org_id=ORG_A)
    assert total == 0


def test_an_unowned_row_is_visible_with_no_user_filter_and_include_unowned(reg):
    r, admin = reg
    test_id = _one_test_one_run(admin, user_id=None, org_id=ORG_A)

    tests, total = r.list_tests(user_id=None, org_id=ORG_A,
                                folder_org_id=ORG_A, include_unowned=True)
    assert total == 1
    assert tests[0]["test_id"] == test_id

    tests, total = r.list_tests(user_id=None, org_id=ORG_A,
                                folder_org_id=ORG_A, include_unowned=False)
    assert total == 0


def test_a_caller_with_no_org_still_sees_their_own_test_but_no_folder_name(reg):
    r, admin = reg
    folder_id = str(uuid.uuid4())
    _folder(admin, folder_id)
    test_id = _one_test_one_run(admin, user_id="alice", group_id=folder_id)

    tests, total = r.list_tests(user_id="alice", org_id=None,
                                folder_org_id=None)
    assert total == 1
    assert tests[0]["test_id"] == test_id
    # identified + no org -> _group_join_for_tests' ON FALSE branch: no
    # folder resolves for this caller even though the row IS filed.
    assert tests[0]["group_id"] is None


def test_the_token_less_caller_sees_every_test(reg):
    r, admin = reg
    _one_test_one_run(admin, user_id="alice", org_id=ORG_A, key_n=1)
    _one_test_one_run(admin, user_id="carol", org_id=ORG_B, key_n=1)

    tests, total = r.list_tests(user_id=None, org_id=None, folder_org_id=None)
    assert total == 2


# ---------------------------------------------------------------------------
# Filters and their interaction with total.
# ---------------------------------------------------------------------------

def test_q_filters_by_user_query_and_narrows_total(reg):
    r, admin = reg
    t1 = str(uuid.uuid4())
    _test_row(admin, t1, key_n=1, user_query="search shoes on flipkart")
    v1 = _version_row(admin, t1, 1)
    _run_row(admin, str(uuid.uuid4()), t1, v1, "passed",
             "2026-01-01T10:00:00Z")
    t2 = str(uuid.uuid4())
    _test_row(admin, t2, key_n=2, user_query="checkout flow")
    v2 = _version_row(admin, t2, 1)
    _run_row(admin, str(uuid.uuid4()), t2, v2, "passed",
             "2026-01-01T10:00:00Z")

    tests, total = r.list_tests(user_id="alice", org_id=ORG_A,
                                folder_org_id=ORG_A, q="shoes")
    assert total == 1
    assert tests[0]["test_id"] == t1


def test_group_id_filters_to_one_folder(reg):
    r, admin = reg
    folder_id = str(uuid.uuid4())
    _folder(admin, folder_id)
    filed = _one_test_one_run(admin, group_id=folder_id, key_n=1)
    _one_test_one_run(admin, group_id=None, key_n=2)

    tests, total = r.list_tests(user_id="alice", org_id=ORG_A,
                                folder_org_id=ORG_A, group=folder_id)
    assert total == 1
    assert tests[0]["test_id"] == filed


def test_group_ungrouped_filters_to_unfiled_tests(reg):
    r, admin = reg
    folder_id = str(uuid.uuid4())
    _folder(admin, folder_id)
    _one_test_one_run(admin, group_id=folder_id, key_n=1)
    unfiled = _one_test_one_run(admin, group_id=None, key_n=2)

    tests, total = r.list_tests(user_id="alice", org_id=ORG_A,
                                folder_org_id=ORG_A, group="ungrouped")
    assert total == 1
    assert tests[0]["test_id"] == unfiled


def test_health_passing_excludes_failing_and_not_run(reg):
    r, admin = reg
    passing = _one_test_one_run(admin, status="passed", key_n=1)
    _one_test_one_run(admin, status="failed", key_n=2)
    # not_run: a version whose only run is still 'generated' (no completed
    # result yet).
    not_run_id = str(uuid.uuid4())
    _test_row(admin, not_run_id, key_n=3)
    v = _version_row(admin, not_run_id, 1)
    _run_row(admin, str(uuid.uuid4()), not_run_id, v, "generated",
             "2026-01-01T10:00:00Z")

    tests, total = r.list_tests(user_id="alice", org_id=ORG_A,
                                folder_org_id=ORG_A, health="passing")
    assert total == 1
    assert tests[0]["test_id"] == passing
    assert tests[0]["health"] == "passing"


def test_health_failing_includes_error_status(reg):
    r, admin = reg
    errored = _one_test_one_run(admin, status="error", key_n=1)
    _one_test_one_run(admin, status="passed", key_n=2)

    tests, total = r.list_tests(user_id="alice", org_id=ORG_A,
                                folder_org_id=ORG_A, health="failing")
    assert total == 1
    assert tests[0]["test_id"] == errored
    assert tests[0]["health"] == "failing"


def test_health_all_or_unset_returns_every_test(reg):
    r, admin = reg
    _one_test_one_run(admin, status="passed", key_n=1)
    _one_test_one_run(admin, status="failed", key_n=2)

    _, total = r.list_tests(user_id="alice", org_id=ORG_A,
                            folder_org_id=ORG_A, health="all")
    assert total == 2
    _, total = r.list_tests(user_id="alice", org_id=ORG_A,
                            folder_org_id=ORG_A, health=None)
    assert total == 2


def test_limit_and_offset_page_while_total_stays_whole(reg):
    r, admin = reg
    for i in range(3):
        _one_test_one_run(admin, key_n=i + 1)

    page1, total1 = r.list_tests(user_id="alice", org_id=ORG_A,
                                 folder_org_id=ORG_A, limit=2, offset=0)
    page2, total2 = r.list_tests(user_id="alice", org_id=ORG_A,
                                 folder_org_id=ORG_A, limit=2, offset=2)
    assert total1 == 3 and total2 == 3
    assert len(page1) == 2
    assert len(page2) == 1
    assert {t["test_id"] for t in page1} & {t["test_id"] for t in page2} == set()


# ---------------------------------------------------------------------------
# spark is version-scoped -- the ten-version defect from spec S6.1.
# ---------------------------------------------------------------------------

def test_spark_is_scoped_to_the_current_version_not_the_whole_test(reg):
    r, admin = reg
    test_id = str(uuid.uuid4())
    _test_row(admin, test_id, key_n=1, current_version=2)
    v1 = _version_row(admin, test_id, 1)
    v2 = _version_row(admin, test_id, 2)
    # v1: three failures, oldest.
    for i, ts in enumerate(["2026-01-01T10:00:00Z", "2026-01-02T10:00:00Z",
                            "2026-01-03T10:00:00Z"]):
        _run_row(admin, f"v1-run-{i}", test_id, v1, "failed", ts)
    # v2 (current): two passes, newest.
    _run_row(admin, "v2-run-0", test_id, v2, "passed", "2026-01-04T10:00:00Z")
    _run_row(admin, "v2-run-1", test_id, v2, "passed", "2026-01-05T10:00:00Z")

    tests, _ = r.list_tests(user_id="alice", org_id=ORG_A, folder_org_id=ORG_A)
    assert tests[0]["spark"] == ["pass", "pass"]
    assert tests[0]["health"] == "passing"


def test_spark_is_oldest_first_and_capped_at_ten(reg):
    r, admin = reg
    test_id = str(uuid.uuid4())
    _test_row(admin, test_id, key_n=1)
    v = _version_row(admin, test_id, 1)
    # 12 results alternating fail/pass, oldest to newest by day.
    for i in range(12):
        status = "failed" if i % 2 == 0 else "passed"
        _run_row(admin, f"run-{i:02d}", test_id, v, status,
                 f"2026-01-{i + 1:02d}T10:00:00Z")

    tests, _ = r.list_tests(user_id="alice", org_id=ORG_A, folder_org_id=ORG_A)
    spark = tests[0]["spark"]
    assert len(spark) == 10
    # The last 10 of the 12 (runs 02..11), oldest first: run-02 was a fail.
    assert spark[0] == "fail"
    assert spark[-1] == "pass"  # run-11 (index 11, odd) was a pass


# ---------------------------------------------------------------------------
# Whole-test vs version-scoped fields disagreeing after a regeneration (R2).
# ---------------------------------------------------------------------------

def test_last_status_is_whole_test_even_when_an_older_versions_rerun_is_newest(reg):
    r, admin = reg
    test_id = str(uuid.uuid4())
    _test_row(admin, test_id, key_n=1, current_version=2)
    v1 = _version_row(admin, test_id, 1)
    v2 = _version_row(admin, test_id, 2)
    _run_row(admin, "v1-original", test_id, v1, "passed",
             "2026-01-01T10:00:00Z")
    _run_row(admin, "v2-current", test_id, v2, "passed",
             "2026-01-02T10:00:00Z")
    # A re-run of the OLD version fired after v2 exists (case 14/15): still
    # names v1, and it is the newest row on the whole test.
    _run_row(admin, "v1-rerun-fails", test_id, v1, "failed",
             "2026-01-03T10:00:00Z")

    tests, _ = r.list_tests(user_id="alice", org_id=ORG_A, folder_org_id=ORG_A)
    row = tests[0]
    assert row["last_status"] == "failed"
    assert row["last_run_id"] == "v1-rerun-fails"
    # health stays scoped to the CURRENT version (v2), untouched by the v1
    # rerun -- the two fields disagree on purpose.
    assert row["health"] == "passing"
    assert row["result_count"] == 3
    assert row["pass_count"] == 2


# ---------------------------------------------------------------------------
# running: version-scoped, does not leak from a stuck older version.
# ---------------------------------------------------------------------------

def test_running_is_true_when_the_current_version_has_an_in_flight_result(reg):
    r, admin = reg
    test_id = str(uuid.uuid4())
    _test_row(admin, test_id, key_n=1)
    v = _version_row(admin, test_id, 1)
    _run_row(admin, "run-1", test_id, v, "passed", "2026-01-01T10:00:00Z")
    _run_row(admin, "run-2", test_id, v, "running", "2026-01-02T10:00:00Z")

    tests, _ = r.list_tests(user_id="alice", org_id=ORG_A, folder_org_id=ORG_A)
    row = tests[0]
    assert row["running"] is True
    # A stuck running result must not turn a passing test's health failing/
    # not_run (case 18): the last COMPLETED result (run-1, passed) decides.
    assert row["health"] == "passing"


def test_running_does_not_leak_from_a_superseded_version(reg):
    r, admin = reg
    test_id = str(uuid.uuid4())
    _test_row(admin, test_id, key_n=1, current_version=2)
    v1 = _version_row(admin, test_id, 1)
    v2 = _version_row(admin, test_id, 2)
    _run_row(admin, "v1-stuck", test_id, v1, "running",
             "2026-01-01T10:00:00Z")
    _run_row(admin, "v2-passed", test_id, v2, "passed",
             "2026-01-02T10:00:00Z")

    tests, _ = r.list_tests(user_id="alice", org_id=ORG_A, folder_org_id=ORG_A)
    row = tests[0]
    assert row["running"] is False
    assert row["health"] == "passing"


# ---------------------------------------------------------------------------
# can_run: false for a code-less current version.
# ---------------------------------------------------------------------------

def test_can_run_false_when_the_current_versions_code_is_missing(reg):
    r, admin = reg
    test_id = str(uuid.uuid4())
    _test_row(admin, test_id, key_n=1)
    v = _version_row(admin, test_id, 1, robot_code=None)
    _run_row(admin, "run-1", test_id, v, "error", "2026-01-01T10:00:00Z")

    tests, _ = r.list_tests(user_id="alice", org_id=ORG_A, folder_org_id=ORG_A)
    assert tests[0]["can_run"] is False


def test_can_run_true_when_the_current_version_has_code(reg):
    r, admin = reg
    test_id = _one_test_one_run(admin)

    tests, _ = r.list_tests(user_id="alice", org_id=ORG_A, folder_org_id=ORG_A)
    assert tests[0]["can_run"] is True


# ---------------------------------------------------------------------------
# Fan-out regression: >1 version AND >1 result per version.
# ---------------------------------------------------------------------------

def test_result_and_version_counts_do_not_fan_out(reg):
    r, admin = reg
    test_id = str(uuid.uuid4())
    _test_row(admin, test_id, key_n=1, current_version=2)
    v1 = _version_row(admin, test_id, 1)
    v2 = _version_row(admin, test_id, 2)
    # v1: 2 results. v2 (current): 3 results. 5 total, 2 versions -- a
    # fanned-out double join would read version_count*result_count = 10 for
    # BOTH numbers instead of 2 and 5.
    _run_row(admin, "v1-a", test_id, v1, "passed", "2026-01-01T10:00:00Z")
    _run_row(admin, "v1-b", test_id, v1, "failed", "2026-01-02T10:00:00Z")
    _run_row(admin, "v2-a", test_id, v2, "passed", "2026-01-03T10:00:00Z")
    _run_row(admin, "v2-b", test_id, v2, "passed", "2026-01-04T10:00:00Z")
    _run_row(admin, "v2-c", test_id, v2, "failed", "2026-01-05T10:00:00Z")

    tests, total = r.list_tests(user_id="alice", org_id=ORG_A,
                                folder_org_id=ORG_A)
    assert total == 1
    row = tests[0]
    assert row["version_count"] == 2
    assert row["result_count"] == 5
    assert row["pass_count"] == 3


# ---------------------------------------------------------------------------
# Sort/tiebreak (case 27) and the raw fields the endpoint needs for can_move.
# ---------------------------------------------------------------------------

def test_default_order_is_last_result_time_descending(reg):
    r, admin = reg
    older = str(uuid.uuid4())
    _test_row(admin, older, key_n=1)
    v = _version_row(admin, older, 1)
    _run_row(admin, "older-run", older, v, "passed", "2026-01-01T10:00:00Z")
    newer = str(uuid.uuid4())
    _test_row(admin, newer, key_n=2)
    v2 = _version_row(admin, newer, 1)
    _run_row(admin, "newer-run", newer, v2, "passed", "2026-01-05T10:00:00Z")

    tests, _ = r.list_tests(user_id="alice", org_id=ORG_A, folder_org_id=ORG_A)
    assert [t["test_id"] for t in tests] == [newer, older]


def test_row_carries_user_id_and_org_id_for_the_endpoints_can_move_check(reg):
    """The endpoint computes can_move and pops these; the registry must still
    hand them back, the same layering list_runs uses for RESULT rows."""
    r, admin = reg
    test_id = _one_test_one_run(admin, user_id="alice", org_id=ORG_A)

    tests, _ = r.list_tests(user_id="alice", org_id=ORG_A, folder_org_id=ORG_A)
    row = tests[0]
    assert row["user_id"] == "alice"
    assert row["org_id"] == ORG_A
    assert row["user_email"] == "a@x.com"
    assert row["name"] is None

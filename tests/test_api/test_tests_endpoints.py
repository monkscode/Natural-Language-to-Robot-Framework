"""GET /api/tests -- the Tests page's list endpoint.

Registry-level predicate/filter/aggregate coverage (visibility, health,
spark, fan-out) lives in tests/test_core/test_run_registry_list_tests.py.
This file covers what that one cannot: the HTTP layer -- query-param
parsing/validation mirroring /api/history's conventions, auth wiring
through require_user/history_scope, and can_move computed against a REAL
caller scope (owner / org_admin / peer) end to end.

Referenced by: none (route tests only).
Depends on: src/backend/api/tests_endpoints.py, tests/test_auth/conftest.py
(auth_isolated_schema), tests/test_api/conftest.py (register_active).
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


def _login(client, email):
    r = client.post("/auth/login", json={"email": email, "password": "S3cretpw!"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def _team_of_three(client):
    """A team org: A is org_admin, B and C are plain org_members.

    JWT claims are minted at login, so every user must log in AGAIN after
    the membership change -- their registration tokens still carry the
    personal org ensure_personal_org gave them, not the team."""
    from src.backend.auth.jwt_utils import decode_token
    from src.backend.auth.org_repository import OrgRepository

    email_a = f"t3a-{uuid.uuid4().hex[:8]}@e.com"
    email_b = f"t3b-{uuid.uuid4().hex[:8]}@e.com"
    email_c = f"t3c-{uuid.uuid4().hex[:8]}@e.com"
    uid_a = decode_token(_register(client, email_a))["user_id"]
    uid_b = decode_token(_register(client, email_b))["user_id"]
    uid_c = decode_token(_register(client, email_c))["user_id"]
    org_id = OrgRepository().create_team_org(f"Acme {uuid.uuid4().hex[:6]}", uid_a)
    OrgRepository().add_member(org_id, uid_b)
    OrgRepository().add_member(org_id, uid_c)
    return (_login(client, email_a), _login(client, email_b),
            _login(client, email_c), org_id)


def _seed_test(user_id, org_id, user_email, query="search shoes",
               robot_code="*** Tasks ***", status="passed"):
    """One test/version/run via record_start -- the real _attach_test path,
    not a hand-built row. Returns (test_id, run_id)."""
    from src.backend.core.run_registry import get_run_registry
    run_id = str(uuid.uuid4())
    reg = get_run_registry()
    reg.record_start(
        run_id, {"user_id": user_id, "org_id": org_id, "email": user_email},
        query, status, robot_code=robot_code)
    with reg._pool.connection() as conn:
        row = conn.execute(
            "SELECT test_id FROM test_runs WHERE run_id = %s",
            (run_id,)).fetchone()
    return row["test_id"], run_id


def _file_into_new_folder(org_id, creator_id, test_id):
    from src.backend.core.run_registry import get_run_registry
    reg = get_run_registry()
    group_id = str(uuid.uuid4())
    with reg._pool.connection() as conn:
        conn.execute(
            "INSERT INTO run_groups (group_id, name, org_id, created_by)"
            " VALUES (%s, %s, %s, %s)",
            (group_id, f"Folder-{group_id[:6]}", org_id, creator_id))
        conn.execute(
            "UPDATE tests SET group_id = %s WHERE test_id = %s",
            (group_id, test_id))
    return group_id


# ---------------------------------------------------------------------------
# Response envelope: tests / total / scope, and the field-redaction
# discipline list_history already applies to runs.
# ---------------------------------------------------------------------------

def test_returns_own_test_with_total_scope_and_every_brief_field(client):
    """scope=="all" here, not "own", and that is /api/history's own already-
    pinned F13 behaviour, not something this endpoint invents: a solo signup
    is org_admin of their own personal org, so history_scope gives them the
    "no per-user narrowing" scope too -- their org is just themselves. This
    endpoint reuses history_scope verbatim, so it must answer the same way
    for the same caller shape. test_scope_is_own_for_a_plain_team_member
    below is where "own" actually shows up: it needs a caller who is
    identified, has a concrete org, and is NOT that org's admin."""
    from src.backend.auth.jwt_utils import decode_token
    email = f"lt-{uuid.uuid4().hex[:8]}@e.com"
    tok = _register(client, email)
    claims = decode_token(tok)
    test_id, run_id = _seed_test(claims["user_id"], claims["org_id"], email)

    r = client.get("/api/tests", headers=_auth(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["scope"] == "all"
    row = body["tests"][0]
    assert row["test_id"] == test_id
    assert row["name"] is None
    assert row["user_query"] == "search shoes"
    assert row["group_id"] is None
    assert row["group_name"] is None
    assert row["current_version"] == 1
    assert row["version_count"] == 1
    assert row["result_count"] == 1
    assert row["pass_count"] == 1
    assert row["last_status"] == "passed"
    assert row["last_run_id"] == run_id
    assert row["last_run_at"]
    assert row["health"] == "passing"
    assert row["running"] is False
    assert row["spark"] == ["pass"]
    assert row["user_email"] == email
    assert row["can_move"] is True
    assert row["can_run"] is True
    # org_id backs can_move but is internal; user_id is admin-only (F13's
    # own author column rule) -- list_history pops both the same way.
    assert "org_id" not in row
    assert "user_id" not in row


def test_can_run_is_false_end_to_end_for_a_codeless_current_version(client):
    from src.backend.auth.jwt_utils import decode_token
    from src.backend.core.run_registry import get_run_registry
    email = f"cr-{uuid.uuid4().hex[:8]}@e.com"
    tok = _register(client, email)
    claims = decode_token(tok)
    test_id, _ = _seed_test(claims["user_id"], claims["org_id"], email)
    reg = get_run_registry()
    with reg._pool.connection() as conn:
        conn.execute(
            "UPDATE test_versions SET robot_code = NULL WHERE test_id = %s",
            (test_id,))

    r = client.get("/api/tests", headers=_auth(tok))
    row = next(t for t in r.json()["tests"] if t["test_id"] == test_id)
    assert row["can_run"] is False


def test_platform_admin_sees_a_foreign_orgs_test_with_user_id_visible(client):
    """A validated platform admin is a DIFFERENT flag from is_org_admin --
    scope.is_admin, re-read from the users table by is_validated_admin, not
    derived from any org membership. Mocked the same way
    test_history_and_reports_access.py mocks it (both import sites bind
    their own module-level reference; history_scope's is the one this
    endpoint reads), rather than provisioning a real admin user, which has
    no existing test helper to build on."""
    from unittest.mock import patch
    # A test owned by someone this session never registered, in an org the
    # caller's own token never carries -- only a genuine platform-admin
    # scope (org_id=None, no row-level org filter) can reach it.
    foreign_test_id, _ = _seed_test(
        "someone-else", str(uuid.uuid4()), "stranger@x.com")

    tok = _register(client, f"pa-{uuid.uuid4().hex[:8]}@e.com")
    with patch("src.backend.api.history_scope.is_validated_admin",
               return_value=True):
        r = client.get("/api/tests", headers=_auth(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == "all"
    row = next(t for t in body["tests"] if t["test_id"] == foreign_test_id)
    # Admin view does not redact the internal user id (list_history's own
    # rule, mirrored here).
    assert row["user_id"] == "someone-else"
    # Visible, but neither owned nor administered by this caller.
    assert row["can_move"] is False


def test_scope_is_own_for_a_plain_team_member(client):
    """The counterpart to the F13 note above: a caller who IS identified,
    HAS a concrete org, and is NOT that org's admin gets the per-user
    narrowing -- scope == "own"."""
    from src.backend.auth.jwt_utils import decode_token
    tok_admin, tok_member, _tok_peer, org_id = _team_of_three(client)
    member_claims = decode_token(tok_member)
    _seed_test(member_claims["user_id"], org_id, member_claims["email"])

    body = client.get("/api/tests", headers=_auth(tok_member)).json()
    assert body["scope"] == "own"
    # The org_admin's own view spans the whole org, same as /api/history.
    body_admin = client.get("/api/tests", headers=_auth(tok_admin)).json()
    assert body_admin["scope"] == "all"


# ---------------------------------------------------------------------------
# can_move: owner, org_admin (non-owner), and a plain peer (neither).
# ---------------------------------------------------------------------------

def test_can_move_is_true_for_the_owner_true_for_org_admin_false_for_a_peer(client):
    from src.backend.auth.jwt_utils import decode_token
    tok_admin, tok_member, tok_peer, org_id = _team_of_three(client)
    member_claims = decode_token(tok_member)
    test_id, _ = _file_into_new_folder_test(client, org_id, member_claims)

    def _row_for(tok):
        r = client.get("/api/tests", headers=_auth(tok))
        assert r.status_code == 200, r.text
        return next(t for t in r.json()["tests"] if t["test_id"] == test_id)

    assert _row_for(tok_member)["can_move"] is True    # owner
    assert _row_for(tok_admin)["can_move"] is True     # org_admin, not owner
    assert _row_for(tok_peer)["can_move"] is False      # neither


def _file_into_new_folder_test(client, org_id, member_claims):
    test_id, run_id = _seed_test(
        member_claims["user_id"], org_id, member_claims["email"])
    _file_into_new_folder(org_id, member_claims["user_id"], test_id)
    return test_id, run_id


# ---------------------------------------------------------------------------
# Query-param handling, mirroring /api/history's conventions.
# ---------------------------------------------------------------------------

def test_invalid_group_id_answers_400(client):
    tok = _register(client, f"ig-{uuid.uuid4().hex[:8]}@e.com")
    r = client.get("/api/tests?group=not-a-uuid", headers=_auth(tok))
    assert r.status_code == 400


def test_empty_group_query_param_means_no_filter(client):
    """Same trap list_history documents: a bare ?group= must not become a
    literal group_id = '' filter that matches zero rows."""
    from src.backend.auth.jwt_utils import decode_token
    email = f"eg-{uuid.uuid4().hex[:8]}@e.com"
    tok = _register(client, email)
    claims = decode_token(tok)
    _seed_test(claims["user_id"], claims["org_id"], email)

    r = client.get("/api/tests?group=", headers=_auth(tok))
    assert r.status_code == 200
    assert r.json()["total"] == 1


def test_unknown_health_value_answers_422(client):
    """Also confirms 'flaky' (owner ruling O2 removed it) is a genuinely
    rejected value, not merely an undocumented one."""
    tok = _register(client, f"ih-{uuid.uuid4().hex[:8]}@e.com")
    r = client.get("/api/tests?health=flaky", headers=_auth(tok))
    assert r.status_code == 422


def test_unknown_sort_value_answers_422(client):
    """sort is a validated Literal now, the same shape health already has
    two tests above -- an unrecognised value must be genuinely rejected,
    not silently ignored the way an undeclared query param would be."""
    tok = _register(client, f"is-{uuid.uuid4().hex[:8]}@e.com")
    r = client.get("/api/tests?sort=name", headers=_auth(tok))
    assert r.status_code == 422


def test_health_passing_filters_the_list(client):
    from src.backend.auth.jwt_utils import decode_token
    email = f"hp-{uuid.uuid4().hex[:8]}@e.com"
    tok = _register(client, email)
    claims = decode_token(tok)
    passing_id, _ = _seed_test(claims["user_id"], claims["org_id"], email,
                              query="passing one", status="passed")
    _seed_test(claims["user_id"], claims["org_id"], email,
              query="failing one", status="failed")

    r = client.get("/api/tests?health=passing", headers=_auth(tok))
    body = r.json()
    assert body["total"] == 1
    assert body["tests"][0]["test_id"] == passing_id


def test_limit_is_capped_like_history(client):
    """?limit=99999&offset=-5 must reach RunRegistry.list_tests already
    clamped to [1, 200] / floored at 0 -- the same convention list_history
    applies (history_endpoints.py:127-128). Patches get_run_registry so the
    assertion is on the exact kwargs the endpoint computed and passed down,
    not on how many rows happen to exist in the isolated test schema (which
    would need 200+ seeded rows to tell a capped page apart from an
    uncapped one)."""
    from unittest.mock import MagicMock, patch
    tok = _register(client, f"lc-{uuid.uuid4().hex[:8]}@e.com")

    stub = MagicMock()
    stub.list_tests.return_value = ([], 0)
    with patch("src.backend.api.tests_endpoints.get_run_registry", return_value=stub):
        r = client.get("/api/tests?limit=99999&offset=-5", headers=_auth(tok))

    assert r.status_code == 200
    _, kwargs = stub.list_tests.call_args
    assert kwargs["limit"] == 200
    assert kwargs["offset"] == 0


# ---------------------------------------------------------------------------
# GET /api/tests/{test_id} -- the drawer's data source. Registry-level
# predicate/paging/version coverage lives in
# tests/test_core/test_run_registry_get_test_detail.py; this section covers
# the HTTP layer: path validation, the 404 convention, has_report, and the
# same field redaction the list applies.
# ---------------------------------------------------------------------------

def _add_result(test_id, status, created_at, user_id="someone",
                org_id=None, version_id=None):
    """One extra result against an existing test, written straight to the
    table: record_start cannot mint a second result with a chosen status and
    timestamp for a test that already exists."""
    from src.backend.core.run_registry import get_run_registry
    reg = get_run_registry()
    run_id = str(uuid.uuid4())
    with reg._pool.connection() as conn:
        conn.execute(
            "INSERT INTO test_runs (run_id, user_id, user_email, user_query,"
            " status, org_id, test_id, test_version_id, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (run_id, user_id, "x@x.com", "q", status, org_id, test_id,
             version_id, created_at))
    return run_id


def test_detail_returns_the_test_its_versions_and_its_paged_results(client):
    from src.backend.auth.jwt_utils import decode_token
    email = f"td-{uuid.uuid4().hex[:8]}@e.com"
    tok = _register(client, email)
    claims = decode_token(tok)
    test_id, run_id = _seed_test(claims["user_id"], claims["org_id"], email)

    r = client.get(f"/api/tests/{test_id}", headers=_auth(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["test"]["test_id"] == test_id
    assert body["test"]["user_query"] == "search shoes"
    assert body["test"]["current_version"] == 1
    assert body["test"]["health"] == "passing"
    assert body["test"]["user_email"] == email
    assert body["test"]["created_at"]
    assert [v["n"] for v in body["versions"]] == [1]
    assert body["versions"][0]["robot_code"] == "*** Tasks ***"
    assert body["versions"][0]["user_query"] == "search shoes"
    assert body["results_total"] == 1
    assert body["results"][0]["run_id"] == run_id
    assert body["results"][0]["n"] == 1
    assert body["results"][0]["status"] == "passed"


def test_detail_reaches_a_peers_test_through_a_folder(client):
    """The same publication rule the list applies: a colleague's test filed
    into a folder this caller's org owns."""
    from src.backend.auth.jwt_utils import decode_token
    _tok_admin, tok_member, tok_peer, org_id = _team_of_three(client)
    member_claims = decode_token(tok_member)
    test_id, _ = _file_into_new_folder_test(client, org_id, member_claims)

    r = client.get(f"/api/tests/{test_id}", headers=_auth(tok_peer))
    assert r.status_code == 200, r.text
    assert r.json()["test"]["group_id"]
    # ...and it is NOT reachable once nobody has published it (case 5).
    unfiled_id, _ = _seed_test(member_claims["user_id"], org_id,
                               member_claims["email"], query="unfiled")
    assert client.get(f"/api/tests/{unfiled_id}",
                      headers=_auth(tok_peer)).status_code == 404


def test_detail_answers_404_never_403_for_a_test_in_another_org(client):
    """Rejection must not distinguish "exists but forbidden" from "no such
    test" -- the convention groups_endpoints' assignment handler states."""
    tok = _register(client, f"tf-{uuid.uuid4().hex[:8]}@e.com")
    foreign_id, _ = _seed_test("someone-else", str(uuid.uuid4()),
                               "stranger@x.com")

    r = client.get(f"/api/tests/{foreign_id}", headers=_auth(tok))
    assert r.status_code == 404
    # Byte-identical to the answer for an id that exists nowhere: the body
    # is what would leak existence, not just the code.
    missing = client.get(f"/api/tests/{uuid.uuid4()}", headers=_auth(tok))
    assert missing.status_code == 404
    assert r.json() == missing.json()


def test_detail_answers_400_for_a_non_uuid_test_id(client):
    """Same shape /api/history/{run_id} uses for a malformed id -- every
    test_id this codebase mints is a uuid4 (the mint and the collapse both
    generate one), so a non-uuid is a bad request, not a missing row."""
    tok = _register(client, f"tb-{uuid.uuid4().hex[:8]}@e.com")
    r = client.get("/api/tests/not-a-uuid", headers=_auth(tok))
    assert r.status_code == 400


def test_detail_has_report_uses_the_history_rule_and_nothing_else(client):
    """_REPORT_STATUSES is ("passed", "failed") -- the statuses with a
    log.html on disk. 'error', 'running' and 'generated' have none, and this
    endpoint must not invent a second answer."""
    from src.backend.auth.jwt_utils import decode_token
    email = f"th-{uuid.uuid4().hex[:8]}@e.com"
    tok = _register(client, email)
    claims = decode_token(tok)
    test_id, passed_run = _seed_test(claims["user_id"], claims["org_id"],
                                     email)
    made = {"passed": passed_run}
    for i, status in enumerate(("failed", "error", "running", "generated")):
        made[status] = _add_result(
            test_id, status, f"2026-03-0{i + 1}T10:00:00Z",
            user_id=claims["user_id"], org_id=claims["org_id"])

    body = client.get(f"/api/tests/{test_id}", headers=_auth(tok)).json()
    by_run = {x["run_id"]: x for x in body["results"]}
    assert by_run[made["passed"]]["has_report"] is True
    assert by_run[made["failed"]]["has_report"] is True
    assert by_run[made["error"]]["has_report"] is False
    assert by_run[made["running"]]["has_report"] is False
    assert by_run[made["generated"]]["has_report"] is False


def test_detail_failure_fields_are_present_and_null(client):
    """P3 columns (section 8). The keys exist so the drawer can build its
    seam; nothing writes a value until P3."""
    from src.backend.auth.jwt_utils import decode_token
    email = f"tn-{uuid.uuid4().hex[:8]}@e.com"
    tok = _register(client, email)
    claims = decode_token(tok)
    test_id, _ = _seed_test(claims["user_id"], claims["org_id"], email,
                            status="failed")

    result = client.get(f"/api/tests/{test_id}",
                        headers=_auth(tok)).json()["results"][0]
    assert result["failure_class"] is None
    assert result["failure_locator"] is None


def test_detail_redacts_org_id_from_everyone_and_ids_from_non_admins(client):
    """The list's own field discipline, restated over this payload: org_id is
    internal, the internal user id is admin-only, and the EMAIL stays so a
    shared folder can still say who authored what. created_by is redacted
    alongside user_id because it holds a user id rather than an email, and
    today always the TEST'S author -- so returning it would hand back the
    value the pop above just removed."""
    from unittest.mock import patch
    from src.backend.auth.jwt_utils import decode_token
    email = f"tr-{uuid.uuid4().hex[:8]}@e.com"
    tok = _register(client, email)
    claims = decode_token(tok)
    test_id, _ = _seed_test(claims["user_id"], claims["org_id"], email)

    body = client.get(f"/api/tests/{test_id}", headers=_auth(tok)).json()
    assert "org_id" not in body["test"]
    assert "user_id" not in body["test"]
    assert body["test"]["user_email"] == email
    assert "created_by" not in body["versions"][0]

    with patch("src.backend.api.history_scope.is_validated_admin",
               return_value=True):
        as_admin = client.get(f"/api/tests/{test_id}",
                              headers=_auth(tok)).json()
    assert as_admin["test"]["user_id"] == claims["user_id"]
    assert as_admin["versions"][0]["created_by"] == claims["user_id"]
    # org_id is internal to the server on BOTH paths -- unlike user_id it is
    # never part of this response.
    assert "org_id" not in as_admin["test"]


def test_detail_limit_is_capped_like_history(client):
    """?limit=99999&offset=-5 must reach the registry already clamped to
    [1, 200] / floored at 0, the same convention list_history and
    GET /api/tests apply. Patches get_run_registry so the assertion is on the
    kwargs the endpoint computed rather than on seeded row counts."""
    from unittest.mock import MagicMock, patch
    tok = _register(client, f"tl-{uuid.uuid4().hex[:8]}@e.com")

    stub = MagicMock()
    stub.get_test_detail.return_value = {
        "test": {"test_id": "x", "user_id": None, "org_id": None},
        "versions": [], "results": [], "results_total": 0,
    }
    with patch("src.backend.api.tests_endpoints.get_run_registry",
               return_value=stub):
        r = client.get(f"/api/tests/{uuid.uuid4()}?limit=99999&offset=-5",
                       headers=_auth(tok))

    assert r.status_code == 200, r.text
    _, kwargs = stub.get_test_detail.call_args
    assert kwargs["limit"] == 200
    assert kwargs["offset"] == 0


def test_detail_pages_its_results(client):
    from src.backend.auth.jwt_utils import decode_token
    email = f"tp-{uuid.uuid4().hex[:8]}@e.com"
    tok = _register(client, email)
    claims = decode_token(tok)
    # record_start stamps created_at with now(), so the seeded result is the
    # NEWEST of the four and the dated ones below sit under it, oldest last.
    test_id, newest = _seed_test(claims["user_id"], claims["org_id"], email)
    older = [
        _add_result(test_id, "passed", f"2026-04-0{i}T10:00:00Z",
                    user_id=claims["user_id"], org_id=claims["org_id"])
        for i in (1, 2, 3)
    ]

    page1 = client.get(f"/api/tests/{test_id}?limit=2&offset=0",
                       headers=_auth(tok)).json()
    assert page1["results_total"] == 4
    assert [x["run_id"] for x in page1["results"]] == [newest, older[2]]

    page2 = client.get(f"/api/tests/{test_id}?limit=2&offset=2",
                       headers=_auth(tok)).json()
    assert page2["results_total"] == 4
    assert [x["run_id"] for x in page2["results"]] == [older[1], older[0]]


def test_detail_resolves_folder_names_only_from_the_callers_own_org(client):
    """A validated platform admin's TEST scope is every org while their
    FOLDER scope stays their own (history_scope.folder_org_id). So they can
    READ a foreign org's test and must still not be told which of that org's
    folders it sits in -- the folder join binds their own org and resolves
    nothing. This is the one caller shape where folder_org_id and org_id
    differ: for everyone else `folder_org_id or org_id` makes them the same
    value, so no other test can pin this parameter."""
    from unittest.mock import patch
    foreign_org = str(uuid.uuid4())
    foreign_id, _ = _seed_test("someone-else", foreign_org, "stranger@x.com")
    _file_into_new_folder(foreign_org, "someone-else", foreign_id)

    tok = _register(client, f"tg-{uuid.uuid4().hex[:8]}@e.com")
    with patch("src.backend.api.history_scope.is_validated_admin",
               return_value=True):
        r = client.get(f"/api/tests/{foreign_id}", headers=_auth(tok))
    assert r.status_code == 200, r.text
    assert r.json()["test"]["group_id"] is None
    assert r.json()["test"]["group_name"] is None


# ---------------------------------------------------------------------------
# PUT /api/tests/assignments -- ruling R3's move endpoint. Registry-level
# authority/atomicity coverage lives in
# tests/test_core/test_run_registry_assign_tests.py; this section covers the
# HTTP layer: body validation, the 404 convention, the audit detail, and --
# the point of the endpoint existing here -- that can_move on the list rows
# answers exactly what this route then does.
# ---------------------------------------------------------------------------

def _new_folder(client, tok, name=None):
    r = client.post("/api/groups",
                    json={"name": name or f"F-{uuid.uuid4().hex[:6]}"},
                    headers=_auth(tok))
    assert r.status_code == 201, r.text
    return r.json()["group_id"]


def _row_for(client, tok, test_id):
    r = client.get("/api/tests", headers=_auth(tok))
    assert r.status_code == 200, r.text
    return next((t for t in r.json()["tests"] if t["test_id"] == test_id), None)


def _move(client, tok, test_ids, group_id):
    return client.put("/api/tests/assignments",
                      json={"test_ids": test_ids, "group_id": group_id},
                      headers=_auth(tok))


def test_assignments_files_a_test_and_then_unfiles_it(client):
    from src.backend.auth.jwt_utils import decode_token
    email = f"asg-{uuid.uuid4().hex[:8]}@e.com"
    tok = _register(client, email)
    claims = decode_token(tok)
    test_id, _ = _seed_test(claims["user_id"], claims["org_id"], email)
    group_id = _new_folder(client, tok)

    r = _move(client, tok, [test_id], group_id)
    assert r.status_code == 200, r.text
    assert r.json() == {"assigned": 1, "group_id": group_id}
    assert _row_for(client, tok, test_id)["group_id"] == group_id

    r = _move(client, tok, [test_id], None)
    assert r.status_code == 200, r.text
    assert r.json() == {"assigned": 1, "group_id": None}
    assert _row_for(client, tok, test_id)["group_id"] is None


def test_assignments_let_an_org_admin_file_a_members_test(client):
    from src.backend.auth.jwt_utils import decode_token
    tok_admin, tok_member, _tok_peer, org_id = _team_of_three(client)
    member = decode_token(tok_member)
    test_id, _ = _seed_test(member["user_id"], org_id, member["email"])
    group_id = _new_folder(client, tok_admin)

    assert _move(client, tok_admin, [test_id], group_id).status_code == 200
    assert _row_for(client, tok_member, test_id)["group_id"] == group_id


def test_assignments_answer_404_for_a_peers_test_and_move_nothing(client):
    """Rejection reads as 404, never 403 -- the convention this module's
    GET /api/tests/{test_id} already follows, so a refusal cannot confirm
    that an id is real."""
    from src.backend.auth.jwt_utils import decode_token
    _tok_admin, tok_member, tok_peer, org_id = _team_of_three(client)
    member = decode_token(tok_member)
    test_id, _ = _seed_test(member["user_id"], org_id, member["email"])
    _file_into_new_folder(org_id, member["user_id"], test_id)
    group_id = _new_folder(client, tok_peer)

    r = _move(client, tok_peer, [test_id], group_id)
    assert r.status_code == 404
    # The peer can SEE the test (it is filed into their org's folder) and
    # still cannot move it -- and it did not move.
    assert _row_for(client, tok_peer, test_id) is not None
    assert _row_for(client, tok_member, test_id)["group_id"] != group_id


def test_assignments_answer_404_for_a_folder_in_another_org(client):
    from src.backend.auth.jwt_utils import decode_token
    email = f"afo-{uuid.uuid4().hex[:8]}@e.com"
    tok = _register(client, email)
    claims = decode_token(tok)
    test_id, _ = _seed_test(claims["user_id"], claims["org_id"], email)
    foreign_tok = _register(client, f"afo2-{uuid.uuid4().hex[:8]}@e.com")
    foreign_group = _new_folder(client, foreign_tok)

    assert _move(client, tok, [test_id], foreign_group).status_code == 404
    assert _row_for(client, tok, test_id)["group_id"] is None


def test_assignments_reject_a_mixed_batch_atomically(client):
    from src.backend.auth.jwt_utils import decode_token
    _tok_admin, tok_member, tok_peer, org_id = _team_of_three(client)
    member = decode_token(tok_member)
    peer = decode_token(tok_peer)
    mine, _ = _seed_test(member["user_id"], org_id, member["email"])
    theirs, _ = _seed_test(peer["user_id"], org_id, peer["email"],
                           query="a peer's own test")
    group_id = _new_folder(client, tok_member)

    assert _move(client, tok_member, [mine, theirs], group_id).status_code == 404
    assert _row_for(client, tok_member, mine)["group_id"] is None


def test_assignments_validate_the_body_like_the_groups_route(client):
    tok = _register(client, f"av2-{uuid.uuid4().hex[:8]}@e.com")
    group_id = _new_folder(client, tok)

    assert client.put("/api/tests/assignments",
                      json={"test_ids": [], "group_id": None},
                      headers=_auth(tok)).status_code == 400
    assert client.put("/api/tests/assignments",
                      json={"test_ids": ["not-a-uuid"], "group_id": None},
                      headers=_auth(tok)).status_code == 400
    assert client.put("/api/tests/assignments",
                      json={"test_ids": [str(uuid.uuid4())],
                            "group_id": "not-a-uuid"},
                      headers=_auth(tok)).status_code == 400
    from src.backend.api.tests_endpoints import _TEST_IDS_MAX
    too_many = [str(uuid.uuid4()) for _ in range(_TEST_IDS_MAX + 1)]
    assert client.put("/api/tests/assignments",
                      json={"test_ids": too_many, "group_id": group_id},
                      headers=_auth(tok)).status_code == 400


def test_assignments_audit_the_move_only_when_it_happened(client, monkeypatch):
    """The floor writes one row per mutating request whatever the status, so
    detail on a refused move would read as a move that happened."""
    from src.backend.auth.jwt_utils import decode_token
    from src.backend.core import audit_log
    rows: list = []
    monkeypatch.setattr(audit_log, "write_audit_log",
                        lambda **kw: rows.append(kw))

    email = f"aud-{uuid.uuid4().hex[:8]}@e.com"
    tok = _register(client, email)
    claims = decode_token(tok)
    test_id, _ = _seed_test(claims["user_id"], claims["org_id"], email)
    group_id = _new_folder(client, tok)

    rows.clear()
    assert _move(client, tok, [test_id], group_id).status_code == 200
    written = [r for r in rows if r["path"] == "/api/tests/assignments"]
    assert len(written) == 1 and written[0]["detail"] is not None
    assert test_id in written[0]["detail"] and group_id in written[0]["detail"]

    rows.clear()
    assert _move(client, tok, [str(uuid.uuid4())], group_id).status_code == 404
    refused = [r for r in rows if r["path"] == "/api/tests/assignments"]
    assert len(refused) == 1 and refused[0]["detail"] is None


# ---------------------------------------------------------------------------
# can_move agrees with the endpoint -- the whole reason ruling R3's move
# route was built beside the flag rather than in groups_endpoints.py.
# ---------------------------------------------------------------------------

def test_can_move_answers_exactly_what_the_move_endpoint_then_does(client):
    """Owner, org_admin and peer, each asked both questions about the same
    test. Task 4's review found the flag and the server disagreeing; this
    pins that they no longer can."""
    from src.backend.auth.jwt_utils import decode_token
    tok_admin, tok_member, tok_peer, org_id = _team_of_three(client)
    member = decode_token(tok_member)
    test_id, _ = _seed_test(member["user_id"], org_id, member["email"])
    _file_into_new_folder(org_id, member["user_id"], test_id)

    for tok in (tok_member, tok_admin, tok_peer):
        flag = _row_for(client, tok, test_id)["can_move"]
        # Each caller moves it into a folder they created themselves, so the
        # folder's own org can never be the term that refuses.
        allowed = _move(client, tok, [test_id],
                        _new_folder(client, tok)).status_code == 200
        assert flag is allowed, f"can_move {flag} but the move said {allowed}"


def test_an_org_less_caller_is_offered_no_move_and_is_refused(client):
    """Task 4's widest disagreement, closed. history_scope gives an
    identified caller whose token carries no org the same org_id=None a
    validated platform admin gets, so list_tests appends no te.org_id row
    filter and the old mirrored flag read True on every row they owned --
    while the move is refused for every one of them. Login normally
    provisions an org before minting a token; a stale pre-tenancy token is
    the shape that still reaches this, which spec section 10 case 38 names."""
    from src.backend.auth.jwt_utils import create_access_token, decode_token
    email = f"noorg-{uuid.uuid4().hex[:8]}@e.com"
    claims = decode_token(_register(client, email))
    test_id, _ = _seed_test(claims["user_id"], claims["org_id"], email)
    orgless = create_access_token({
        "id": claims["user_id"], "email": email, "role": "user",
        "display_name": "", "org_id": None, "org_role": None,
        "token_version": claims["token_version"], "status": "active",
    })

    row = _row_for(client, orgless, test_id)
    assert row is not None, "the org-less caller still sees their own test"
    # This test carries a CONCRETE org, so what refuses here is the org
    # EQUALITY term (concrete != None), not the folder_org_id guard beside
    # it -- see the next test for the shape only that guard refuses.
    assert row["can_move"] is False
    # 403, not 404: the org guard runs before the registry is reached, and
    # the SPA must not read it as an expired session (that is why it is not
    # a 401 either).
    assert _move(client, orgless, [test_id], str(uuid.uuid4())).status_code == 403


def test_an_org_less_test_is_not_movable_by_its_org_less_author(client):
    """The one shape can_move's `folder_org_id is not None` term refuses on
    its own: both sides of the org comparison are None, and two NULLs must
    not compare equal. Reached by mutation rather than by reasoning -- with
    that term removed every other assertion in this file still passed.

    The population is real rather than hypothetical: a test minted while
    _lookup_org_id swallowed a failure stays org-less and, since Task 4.5,
    is repairable only by its own author; a stale pre-tenancy token is what
    puts that author in an org-less scope (spec section 10 case 38).

    record_start cannot mint this row directly -- it looks an org up from
    the user_id whenever the token carries none -- so the column is cleared
    afterwards, with a fresh key_n because idx_tests_org_key is UNIQUE
    (org_id, key_n) NULLS NOT DISTINCT."""
    from src.backend.auth.jwt_utils import create_access_token, decode_token
    from src.backend.core.run_registry import get_run_registry
    email = f"noorg2-{uuid.uuid4().hex[:8]}@e.com"
    claims = decode_token(_register(client, email))
    test_id, _ = _seed_test(claims["user_id"], claims["org_id"], email,
                            query="an org-less test")
    reg = get_run_registry()
    with reg._pool.connection() as conn:
        conn.execute(
            "UPDATE tests SET org_id = NULL, key_n = ("
            "  SELECT coalesce(max(key_n), 0) + 1 FROM tests"
            "   WHERE org_id IS NULL) WHERE test_id = %s",
            (test_id,))
    orgless = create_access_token({
        "id": claims["user_id"], "email": email, "role": "user",
        "display_name": "", "org_id": None, "org_role": None,
        "token_version": claims["token_version"], "status": "active",
    })

    row = _row_for(client, orgless, test_id)
    assert row is not None, "an org-less author still sees their org-less test"
    assert row["can_move"] is False
    assert _move(client, orgless, [test_id], str(uuid.uuid4())).status_code == 403


def test_a_token_less_caller_is_refused_403_not_401(client):
    """AUTH_ENFORCED is off in this suite, so require_user yields None for a
    request with no Authorization header. A mutation there must be 403: the
    SPA reads every 401 as "session expired", clears the token and
    hard-redirects to /login, so a 401 here logs the whole app out on a
    click. The same rule groups_endpoints states for its own mutations,
    which is why this route imports its guard rather than restating it."""
    r = client.put("/api/tests/assignments",
                   json={"test_ids": [str(uuid.uuid4())], "group_id": None})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Result-level visibility through the route.
#
# auth/ownership.py states the contract: no list may offer a row
# caller_can_access then refuses. Passing the TEST gate is not authority over
# every result underneath it, and this route sets has_report off `status`
# alone -- so a row it should not have listed also renders a report link that
# the /reports guard answers 404 for.
# ---------------------------------------------------------------------------

def test_detail_does_not_offer_a_result_that_history_hides(client):
    """An UNATTRIBUTED result under a published test. The peer reaches the
    test through the folder, so the drawer opens -- but this row is
    platform-admin-only (ownership rule 3 requires owner_id non-NULL), and
    /api/history already hides it from the same caller."""
    from src.backend.auth.jwt_utils import decode_token
    from src.backend.core.run_registry import get_run_registry
    _tok_admin, tok_member, tok_peer, org_id = _team_of_three(client)
    member_claims = decode_token(tok_member)
    test_id, authored_run = _file_into_new_folder_test(
        client, org_id, member_claims)

    orphan = str(uuid.uuid4())
    reg = get_run_registry()
    with reg._pool.connection() as conn:
        version_id = conn.execute(
            "SELECT test_version_id FROM test_runs WHERE run_id = %s",
            (authored_run,)).fetchone()["test_version_id"]
        conn.execute(
            "INSERT INTO test_runs (run_id, user_id, user_email, user_query,"
            " status, org_id, test_id, test_version_id)"
            " VALUES (%s, NULL, NULL, 'q', 'passed', %s, %s, %s)",
            (orphan, org_id, test_id, version_id))

    r = client.get(f"/api/tests/{test_id}", headers=_auth(tok_peer))
    assert r.status_code == 200, r.text
    body = r.json()
    assert [x["run_id"] for x in body["results"]] == [authored_run]
    # The count has to apply the same predicate as the page.
    assert body["results_total"] == 1

    # ...which is exactly what /api/history answers for the same caller.
    hist = client.get("/api/history", headers=_auth(tok_peer))
    assert hist.status_code == 200, hist.text
    assert orphan not in [x["run_id"] for x in hist.json()["runs"]]

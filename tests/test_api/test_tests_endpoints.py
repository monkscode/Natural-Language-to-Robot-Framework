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
    tok = _register(client, f"lc-{uuid.uuid4().hex[:8]}@e.com")
    r = client.get("/api/tests?limit=99999&offset=-5", headers=_auth(tok))
    assert r.status_code == 200

"""POST /execute-test with a test_id -- the Tests page's Run button (Task 7).

A RE-RUN of the test's CURRENT version, and the first execute entry point
that names a test rather than carrying code. Four things this file pins:

- the field combination. test_id beside rerun_of, robot_code, workflow_id or
  user_query is a 400, not a silent precedence: workflow_id reuse is the
  exact door Task 4.5's adoption hole went through, and a client-supplied id
  must never select the row this path writes;
- authority is VISIBILITY of the test (decision D5 -- a team publishes a
  test into a folder so the team can run it), which is a strictly weaker
  question than updating it;
- what the run inherits. The description comes from the test so the row is
  recognisable in History, learning stays skipped as it is for every re-run
  (spec section 9), rerun_of stays NULL because results carry no lineage
  between them (case 1), and no folder is written on the run -- a NULL
  column can never go stale;
- case 31, from the server side: a current version with no code answers 409
  with the reason instead of starting a container.

Referenced by: none (route tests only).
Depends on: src/backend/api/endpoints.py (execute_test_only,
_run_current_version), tests/test_auth/conftest.py (auth_isolated_schema),
tests/test_api/conftest.py (register_active).
"""
import contextlib
import uuid
from unittest.mock import patch

import pytest

pytestmark = [pytest.mark.integration,
              pytest.mark.usefixtures("auth_isolated_schema")]


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from src.backend.main import app
    return TestClient(app)


def _register(client, email):
    from tests.test_api.conftest import register_active
    return register_active(client, email)


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def _seed_test(user_id, org_id, user_email, query="search shoes",
               robot_code="*** Tasks *** v1"):
    """One test at version 1 through the real mint path."""
    from src.backend.core.run_registry import get_run_registry
    run_id = str(uuid.uuid4())
    reg = get_run_registry()
    reg.record_start(
        run_id, {"user_id": user_id, "org_id": org_id, "email": user_email},
        query, "generated", robot_code=robot_code)
    with reg._pool.connection() as conn:
        row = conn.execute(
            "SELECT test_id FROM test_runs WHERE run_id = %s",
            (run_id,)).fetchone()
    return row["test_id"]


@contextlib.contextmanager
def _execution_captured():
    """Replace the execution stream, keeping every argument it was given."""
    from src.backend.api import endpoints as ep
    calls = []

    async def _fake(robot_code, user_query=None, workflow_id=None, **kw):
        calls.append({"robot_code": robot_code, "user_query": user_query,
                      "workflow_id": workflow_id, **kw})
        yield 'data: {"stage": "execution"}\n\n'

    with patch.object(ep, "stream_execute_only", _fake):
        yield calls


def _run_test(client, tok, body):
    return client.post("/execute-test", json=body,
                       headers=_auth(tok) if tok else {})


def _own_test(client, prefix="rt"):
    from src.backend.auth.jwt_utils import decode_token
    email = f"{prefix}-{uuid.uuid4().hex[:8]}@e.com"
    tok = _register(client, email)
    claims = decode_token(tok)
    return tok, claims, _seed_test(claims["user_id"], claims["org_id"], email)


# ---------------------------------------------------------------------------
# The happy path and what the run inherits
# ---------------------------------------------------------------------------

def test_running_a_test_executes_its_current_versions_code(client):
    from src.backend.core.run_registry import get_run_registry
    tok, claims, test_id = _own_test(client)
    reg = get_run_registry()
    reg.record_start(str(uuid.uuid4()),
                     {"user_id": claims["user_id"], "org_id": claims["org_id"],
                      "email": claims["email"]},
                     "search boots", "generated",
                     robot_code="*** Tasks *** v2",
                     test_id=test_id, version_reason="edited")

    with _execution_captured() as calls:
        r = _run_test(client, tok, {"test_id": test_id})
    assert r.status_code == 200, r.text
    assert calls[0]["robot_code"] == "*** Tasks *** v2"
    assert calls[0]["test_id"] == test_id
    with reg._pool.connection() as conn:
        current = conn.execute(
            "SELECT version_id FROM test_versions WHERE test_id = %s"
            " AND n = 2", (test_id,)).fetchone()["version_id"]
    assert calls[0]["test_version_id"] == current


def test_the_run_takes_its_description_from_the_test_and_learns_nothing(client):
    """history_query populates the History row; user_query stays None, which
    is the guard that keeps every re-run out of the learning store."""
    tok, _claims, test_id = _own_test(client)
    with _execution_captured() as calls:
        assert _run_test(client, tok, {"test_id": test_id}).status_code == 200
    assert calls[0]["history_query"] == "search shoes"
    assert calls[0]["user_query"] is None


def test_the_run_carries_no_lineage_and_no_folder_of_its_own(client):
    """Spec case 1: results have no lineage between them, they all point at
    the test. The folder follows the TEST by join, so writing one here could
    only ever go stale."""
    tok, _claims, test_id = _own_test(client)
    with _execution_captured() as calls:
        assert _run_test(client, tok, {"test_id": test_id}).status_code == 200
    assert calls[0].get("rerun_of") is None
    assert calls[0].get("group_id") is None
    assert calls[0]["workflow_id"] is None


def test_a_peer_may_run_a_published_test(client):
    """Decision D5: a team publishes a test into a folder so the team can run
    it. Running is a strictly weaker power than updating -- the same peer is
    refused a version append."""
    from src.backend.auth.jwt_utils import decode_token
    from src.backend.auth.org_repository import OrgRepository
    from src.backend.core.run_registry import get_run_registry
    email_a = f"pm-{uuid.uuid4().hex[:8]}@e.com"
    email_b = f"pp-{uuid.uuid4().hex[:8]}@e.com"
    uid_a = decode_token(_register(client, email_a))["user_id"]
    uid_b = decode_token(_register(client, email_b))["user_id"]
    org_id = OrgRepository().create_team_org(
        f"Acme {uuid.uuid4().hex[:6]}", uid_a)
    OrgRepository().add_member(org_id, uid_b)
    test_id = _seed_test(uid_a, org_id, email_a)
    reg = get_run_registry()
    group_id = str(uuid.uuid4())
    with reg._pool.connection() as conn:
        conn.execute(
            "INSERT INTO run_groups (group_id, name, org_id, created_by)"
            " VALUES (%s, %s, %s, %s)",
            (group_id, f"F-{group_id[:6]}", org_id, uid_a))
        conn.execute("UPDATE tests SET group_id = %s WHERE test_id = %s",
                     (group_id, test_id))
    r = client.post("/auth/login",
                    json={"email": email_b, "password": "S3cretpw!"})
    tok_peer = r.json()["access_token"]

    with _execution_captured() as calls:
        assert _run_test(client, tok_peer,
                         {"test_id": test_id}).status_code == 200
    assert calls[0]["test_id"] == test_id


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

def test_a_test_in_another_org_answers_404(client):
    from src.backend.auth.jwt_utils import decode_token
    tok, _claims, _mine = _own_test(client)
    foreign_email = f"rtf-{uuid.uuid4().hex[:8]}@e.com"
    foreign = decode_token(_register(client, foreign_email))
    foreign_test = _seed_test(foreign["user_id"], foreign["org_id"],
                              foreign_email)

    with _execution_captured() as calls:
        r = _run_test(client, tok, {"test_id": foreign_test})
    assert r.status_code == 404
    assert calls == []


def test_a_test_that_does_not_exist_answers_the_same_404(client):
    tok, _claims, _test_id = _own_test(client)
    with _execution_captured() as calls:
        r = _run_test(client, tok, {"test_id": str(uuid.uuid4())})
    assert r.status_code == 404
    assert calls == []


def test_a_malformed_test_id_answers_400(client):
    tok, _claims, _test_id = _own_test(client)
    with _execution_captured():
        assert _run_test(client, tok,
                         {"test_id": "not-a-uuid"}).status_code == 400


def test_a_current_version_with_no_code_answers_409_with_the_reason(client):
    """Spec case 31. The Tests page disables the button, but the server is
    the half that binds every client."""
    from src.backend.core.run_registry import get_run_registry
    tok, _claims, test_id = _own_test(client)
    with get_run_registry()._pool.connection() as conn:
        conn.execute("UPDATE test_versions SET robot_code = NULL"
                     " WHERE test_id = %s", (test_id,))

    with _execution_captured() as calls:
        r = _run_test(client, tok, {"test_id": test_id})
    assert r.status_code == 409
    assert "regenerate" in r.json()["detail"].lower()
    assert calls == []


def test_a_missing_current_version_row_answers_409(client):
    """current_version naming no row at all -- answerable only because the
    pre-flight read LEFT-joins the version rather than requiring it."""
    from src.backend.core.run_registry import get_run_registry
    tok, _claims, test_id = _own_test(client)
    with get_run_registry()._pool.connection() as conn:
        conn.execute("UPDATE tests SET current_version = 9"
                     " WHERE test_id = %s", (test_id,))

    with _execution_captured() as calls:
        assert _run_test(client, tok, {"test_id": test_id}).status_code == 409
    assert calls == []


# ---------------------------------------------------------------------------
# The field combination
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("extra", [
    {"rerun_of": "11111111-1111-1111-1111-111111111111"},
    {"robot_code": "*** Tasks ***"},
    {"workflow_id": "22222222-2222-2222-2222-222222222222"},
    {"user_query": "search shoes"},
])
def test_test_id_may_not_be_combined_with_another_selector(client, extra):
    """Each of these names a DIFFERENT thing to run or a different id to
    write under. Silent precedence is how the adoption hole worked: a
    client-supplied workflow_id selected the row record_start then wrote."""
    tok, _claims, test_id = _own_test(client)
    with _execution_captured() as calls:
        r = _run_test(client, tok, dict({"test_id": test_id}, **extra))
    assert r.status_code == 400, r.text
    assert calls == []


def test_the_body_with_none_of_the_three_still_answers_the_old_400(client):
    tok, _claims, _test_id = _own_test(client)
    with _execution_captured():
        r = _run_test(client, tok, {})
    assert r.status_code == 400
    assert "Robot code not provided" in r.json()["detail"]


def test_a_rerun_still_ignores_extra_fields(client):
    """rerun_of's tolerance is unchanged: it is an existing contract and
    only the NEW field tightens."""
    tok, _claims, _test_id = _own_test(client)
    with _execution_captured():
        r = _run_test(client, tok, {"rerun_of": str(uuid.uuid4()),
                                    "robot_code": "*** Tasks ***"})
    assert r.status_code == 404, r.text

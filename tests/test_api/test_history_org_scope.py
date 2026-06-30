"""History is org-scoped: a member sees own org's runs, not another org's.

The org-dimension tests verify:
1. list_runs accepts an org_id kwarg and filters by it (unit-level: proves
   the org column is wired, not just the user_id column).
2. list_history endpoint scopes by the caller's org — an org-admin of their
   own personal org cannot see another org's runs via the list path.
3. run_detail uses caller_can_read (org-aware predicate) instead of the
   old hand-rolled is_owner comparison.

Referenced by: src/backend/api/history_endpoints.py,
               src/backend/core/run_registry.py.
Depends on: tests/test_auth/conftest.py (auth_isolated_schema).
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
# Unit: list_runs(org_id=...) must accept and apply the filter
# ---------------------------------------------------------------------------

class TestListRunsOrgFilter:
    """Direct RunRegistry unit tests for the org_id filter param.

    These fail before implementation with TypeError: unexpected keyword
    argument 'org_id' — which is the genuine RED signal that the ORG
    dimension hasn't been wired yet (not a coincidental user_id pass)."""

    @pytest.fixture(scope="class")
    def reg(self):
        """Isolated RunRegistry on its own schema."""
        import psycopg
        from src.backend.core.config import settings
        from src.backend.core.run_registry import RunRegistry

        schema = "hist_org_test"
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
        admin.execute("TRUNCATE hist_org_test.test_runs")
        admin.close()

    def test_list_runs_accepts_org_id_kwarg(self, reg):
        """list_runs must not raise TypeError on org_id= — this is the RED gate."""
        # Raises TypeError before the param is added.
        rows, total = reg.list_runs(org_id="some-org")
        assert total == 0 and rows == []

    def test_org_id_filter_isolates_runs(self, reg):
        """list_runs(org_id=X) returns only runs whose org_id column is X."""
        org_a = str(uuid.uuid4())
        org_b = str(uuid.uuid4())
        user_a = {"user_id": "ua", "email": "a@e.com", "org_id": org_a}
        user_b = {"user_id": "ub", "email": "b@e.com", "org_id": org_b}

        reg.record_start("run-a", user_a, "alpha query", "passed")
        reg.record_start("run-b", user_b, "beta query", "passed")

        rows_a, total_a = reg.list_runs(org_id=org_a)
        assert total_a == 1 and rows_a[0]["run_id"] == "run-a"

        rows_b, total_b = reg.list_runs(org_id=org_b)
        assert total_b == 1 and rows_b[0]["run_id"] == "run-b"

    def test_org_id_combines_with_user_id(self, reg):
        """Both org_id AND user_id filters can be applied together."""
        org = str(uuid.uuid4())
        user1 = {"user_id": "u1", "email": "u1@e.com", "org_id": org}
        user2 = {"user_id": "u2", "email": "u2@e.com", "org_id": org}

        reg.record_start("run-1", user1, "q1", "passed")
        reg.record_start("run-2", user2, "q2", "passed")

        rows, total = reg.list_runs(user_id="u1", org_id=org)
        assert total == 1 and rows[0]["run_id"] == "run-1"

    def test_org_id_none_returns_all(self, reg):
        """list_runs(org_id=None) is unchanged — admin scope."""
        org = str(uuid.uuid4())
        user = {"user_id": "ua", "email": "a@e.com", "org_id": org}
        reg.record_start("run-x", user, "q", "passed")

        rows, total = reg.list_runs(org_id=None)
        assert total == 1 and rows[0]["run_id"] == "run-x"


# ---------------------------------------------------------------------------
# Integration: /api/history list endpoint — org scoping via real JWT tokens
# ---------------------------------------------------------------------------

def test_history_does_not_leak_across_orgs(client):
    """User B (different personal org) cannot see user A's run in the list."""
    from src.backend.auth.jwt_utils import decode_token
    from src.backend.core.run_registry import get_run_registry

    tok_a = _register(client, f"ha-{uuid.uuid4().hex[:8]}@e.com")
    tok_b = _register(client, f"hb-{uuid.uuid4().hex[:8]}@e.com")
    a = decode_token(tok_a)

    rid = str(uuid.uuid4())
    get_run_registry().record_start(
        rid,
        {"user_id": a["user_id"], "email": a["email"], "org_id": a["org_id"]},
        "alpha run",
        "passed",
    )

    seen = client.get(
        "/api/history", headers={"Authorization": f"Bearer {tok_a}"}
    ).json()
    assert any(r["run_id"] == rid for r in seen["runs"])

    other = client.get(
        "/api/history", headers={"Authorization": f"Bearer {tok_b}"}
    ).json()
    assert all(r["run_id"] != rid for r in other["runs"])


def test_org_admin_of_own_org_cannot_see_other_org_run(client):
    """Every personal-org user is org_admin of their own org.
    Org-scoping (not just user_id) is what keeps org B from seeing org A's run.
    This test is the org-dimension RED gate: before org_id is added to the
    WHERE clause an org_admin with scope_user_id=None sees ALL rows."""
    from src.backend.auth.jwt_utils import decode_token
    from src.backend.core.run_registry import get_run_registry

    tok_a = _register(client, f"oa-{uuid.uuid4().hex[:8]}@e.com")
    tok_b = _register(client, f"ob-{uuid.uuid4().hex[:8]}@e.com")
    a = decode_token(tok_a)

    rid = str(uuid.uuid4())
    get_run_registry().record_start(
        rid,
        {"user_id": a["user_id"], "email": a["email"], "org_id": a["org_id"]},
        "secret run",
        "passed",
    )

    # B is an org_admin of their personal org — before the org_id filter is
    # wired, the endpoint would set scope_user_id=None (org_admin path) and
    # return every row in the DB.  After wiring, scope_org_id=B's org filters
    # it down to B's org only, so A's rid is not visible.
    other = client.get(
        "/api/history", headers={"Authorization": f"Bearer {tok_b}"}
    ).json()
    assert all(r["run_id"] != rid for r in other["runs"]), (
        "org-admin of org B must NOT see a run from org A (org_id filter not applied)"
    )


# ---------------------------------------------------------------------------
# Integration: /api/history/{run_id} detail — caller_can_read predicate
# ---------------------------------------------------------------------------

def test_run_detail_foreign_org_is_404(client):
    """User B cannot fetch the detail of user A's run by its UUID."""
    from src.backend.auth.jwt_utils import decode_token
    from src.backend.core.run_registry import get_run_registry

    tok_a = _register(client, f"da-{uuid.uuid4().hex[:8]}@e.com")
    tok_b = _register(client, f"db-{uuid.uuid4().hex[:8]}@e.com")
    a = decode_token(tok_a)

    rid = str(uuid.uuid4())
    get_run_registry().record_start(
        rid,
        {"user_id": a["user_id"], "email": a["email"], "org_id": a["org_id"]},
        "private run",
        "passed",
    )

    # A can see their own run.
    r_a = client.get(f"/api/history/{rid}", headers={"Authorization": f"Bearer {tok_a}"})
    assert r_a.status_code == 200

    # B gets 404 (existence not leaked).
    r_b = client.get(f"/api/history/{rid}", headers={"Authorization": f"Bearer {tok_b}"})
    assert r_b.status_code == 404

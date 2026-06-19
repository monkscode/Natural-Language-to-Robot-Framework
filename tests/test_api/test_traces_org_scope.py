"""Org-scoped trace dashboards: org-admin sees only their org's traces; member gets 403.

Scenario:
- User A and user B register (each becomes org-admin of their personal org).
- A trace row is inserted under org A's org_id.
- GET /api/admin/traces/ with A's token: must include A's trace.
- GET /api/admin/traces/ with B's token: must return 200 but NOT include A's trace.
- GET /api/admin/traces/ with a plain org-member token: must return 403.
- GET /api/admin/traces/stats/cost with org-member token: must return 403.

The route paths are /api/admin/traces/ and /api/admin/traces/stats/cost
(prefix="/api" in main.py + prefix="/admin/traces" in the router).

Referenced by: src/backend/api/trace_endpoints.py, src/backend/core/trace_store.py.
Depends on: tests/test_auth/conftest.py (auth_isolated_schema).
"""

import uuid

import psycopg
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]

_TRACE_SCHEMA = "traces_org_scope_test"


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from src.backend.main import app
    return TestClient(app)


@pytest.fixture(scope="module")
def trace_schema():
    """Create an isolated llm_traces schema and seed one trace row for org A."""
    from src.backend.core.config import settings
    from src.backend.core.trace_store import _ensure_schema

    admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    admin.execute(f"DROP SCHEMA IF EXISTS {_TRACE_SCHEMA} CASCADE")
    admin.execute(f"CREATE SCHEMA {_TRACE_SCHEMA}")
    sep = "&" if "?" in settings.DATABASE_URL else "?"
    dsn = settings.DATABASE_URL + f"{sep}options=-c%20search_path%3D{_TRACE_SCHEMA}"
    conn = psycopg.connect(dsn)
    try:
        _ensure_schema(conn)
    finally:
        conn.close()
    yield dsn
    admin.execute(f"DROP SCHEMA IF EXISTS {_TRACE_SCHEMA} CASCADE")
    admin.close()


def _register(client, email):
    r = client.post("/auth/register", json={"email": email, "password": "S3cretpw!"})
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


# ---------------------------------------------------------------------------
# Helper: mint a token for an org-member (same org as user A, but role=member)
# ---------------------------------------------------------------------------

def _member_token(org_id: str) -> str:
    """Mint a JWT for a synthetic org_member without touching the DB."""
    from src.backend.auth.jwt_utils import create_access_token
    return create_access_token({
        "id": str(uuid.uuid4()),
        "email": f"member-{uuid.uuid4().hex[:6]}@e.com",
        "role": "user",
        "display_name": "Member",
        "org_id": org_id,
        "org_role": "org_member",
    })


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestTracesOrgScope:
    """Integration tests for org-scoped trace dashboard access."""

    @pytest.fixture(scope="class")
    def setup(self, client, trace_schema):
        """Register two users and insert a trace belonging to org A."""
        from src.backend.auth.jwt_utils import decode_token
        from src.backend.crew_ai.optimization import pg_compat

        tok_a = _register(client, f"ta-{uuid.uuid4().hex[:8]}@e.com")
        tok_b = _register(client, f"tb-{uuid.uuid4().hex[:8]}@e.com")
        a = decode_token(tok_a)
        b = decode_token(tok_b)

        org_a = a["org_id"]

        # Insert a trace row that belongs to org A
        span_id = f"scope-span-{uuid.uuid4().hex[:8]}"
        workflow_id = str(uuid.uuid4())
        conn = pg_compat.connect(trace_schema, autocommit=True)
        conn.execute(
            "INSERT INTO llm_traces "
            "(id, trace_id, name, start_time_ns, end_time_ns, duration_ms, status, "
            " model, prompt_tokens, completion_tokens, total_tokens, cost_usd, "
            " workflow_id, org_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                span_id, f"trace-{uuid.uuid4().hex[:8]}",
                "gemini/gemini-2.5-flash.litellm",
                1_000_000, 2_000_000, 100.0, "OK",
                "gemini/gemini-2.5-flash",
                10, 5, 15, 0.001,
                workflow_id,
                org_a,
            ),
        )
        conn.close()

        # Patch _get_db so the endpoint reads from our isolated schema
        yield {
            "tok_a": tok_a,
            "tok_b": tok_b,
            "org_a": org_a,
            "org_b": b["org_id"],
            "span_id": span_id,
            "workflow_id": workflow_id,
            "trace_dsn": trace_schema,
        }

    def test_org_admin_a_sees_own_trace(self, client, setup):
        """Org-admin A gets 200 and their trace is in the list."""
        from unittest.mock import patch
        from src.backend.crew_ai.optimization import pg_compat

        def _test_db():
            return pg_compat.connect(setup["trace_dsn"], autocommit=True)

        with patch("src.backend.api.trace_endpoints._get_db", side_effect=_test_db):
            resp = client.get(
                "/api/admin/traces/?llm_only=false",
                headers={"Authorization": f"Bearer {setup['tok_a']}"},
            )
        assert resp.status_code == 200
        ids = [t["id"] for t in resp.json()["traces"]]
        assert setup["span_id"] in ids, f"A's trace not found; got ids={ids}"

    def test_org_admin_b_excluded_from_a_trace(self, client, setup):
        """Org-admin B gets 200 but cannot see org A's trace."""
        from unittest.mock import patch
        from src.backend.crew_ai.optimization import pg_compat

        def _test_db():
            return pg_compat.connect(setup["trace_dsn"], autocommit=True)

        with patch("src.backend.api.trace_endpoints._get_db", side_effect=_test_db):
            resp = client.get(
                "/api/admin/traces/?llm_only=false",
                headers={"Authorization": f"Bearer {setup['tok_b']}"},
            )
        assert resp.status_code == 200
        ids = [t["id"] for t in resp.json()["traces"]]
        assert setup["span_id"] not in ids, f"B must NOT see A's trace; got ids={ids}"

    def test_org_member_gets_403_list(self, client, setup):
        """A plain org-member is denied access to the trace list (403)."""
        from unittest.mock import patch
        from src.backend.crew_ai.optimization import pg_compat

        member_tok = _member_token(setup["org_a"])

        def _test_db():
            return pg_compat.connect(setup["trace_dsn"], autocommit=True)

        with patch("src.backend.api.trace_endpoints._get_db", side_effect=_test_db):
            resp = client.get(
                "/api/admin/traces/",
                headers={"Authorization": f"Bearer {member_tok}"},
            )
        assert resp.status_code == 403

    def test_org_member_gets_403_stats(self, client, setup):
        """A plain org-member is denied access to cost stats (403)."""
        from unittest.mock import patch
        from src.backend.crew_ai.optimization import pg_compat

        member_tok = _member_token(setup["org_a"])

        def _test_db():
            return pg_compat.connect(setup["trace_dsn"], autocommit=True)

        with patch("src.backend.api.trace_endpoints._get_db", side_effect=_test_db):
            resp = client.get(
                "/api/admin/traces/stats/cost",
                headers={"Authorization": f"Bearer {member_tok}"},
            )
        assert resp.status_code == 403

    # -------------------------------------------------------------------------
    # Per-item cross-org isolation tests (fix for critical cross-org read leak)
    # -------------------------------------------------------------------------

    def test_org_admin_b_cannot_read_a_workflow_traces(self, client, setup):
        """Org-admin B gets the empty shape for org A's workflow — no prompt/response leak."""
        from unittest.mock import patch
        from src.backend.crew_ai.optimization import pg_compat

        def _test_db():
            return pg_compat.connect(setup["trace_dsn"], autocommit=True)

        with patch("src.backend.api.trace_endpoints._get_db", side_effect=_test_db):
            resp = client.get(
                f"/api/admin/traces/workflow/{setup['workflow_id']}",
                headers={"Authorization": f"Bearer {setup['tok_b']}"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["llm_calls"] == 0, f"B must see 0 calls for A's workflow; got {body}"
        assert body["traces"] == [], f"B must see empty traces for A's workflow; got {body}"

    def test_org_admin_b_cannot_read_a_span_detail(self, client, setup):
        """Org-admin B gets 404 for org A's span id — no prompt/response leak."""
        from unittest.mock import patch
        from src.backend.crew_ai.optimization import pg_compat

        def _test_db():
            return pg_compat.connect(setup["trace_dsn"], autocommit=True)

        with patch("src.backend.api.trace_endpoints._get_db", side_effect=_test_db):
            resp = client.get(
                f"/api/admin/traces/{setup['span_id']}",
                headers={"Authorization": f"Bearer {setup['tok_b']}"},
            )
        assert resp.status_code == 404, f"B must get 404 for A's span; got {resp.status_code} {resp.text}"

    def test_org_admin_a_can_read_own_workflow_traces(self, client, setup):
        """Org-admin A gets 200 with their trace data via workflow route (positive control)."""
        from unittest.mock import patch
        from src.backend.crew_ai.optimization import pg_compat

        def _test_db():
            return pg_compat.connect(setup["trace_dsn"], autocommit=True)

        with patch("src.backend.api.trace_endpoints._get_db", side_effect=_test_db):
            resp = client.get(
                f"/api/admin/traces/workflow/{setup['workflow_id']}",
                headers={"Authorization": f"Bearer {setup['tok_a']}"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["llm_calls"] >= 1, f"A must see their own workflow trace; got {body}"
        ids = [t["id"] for t in body["traces"]]
        assert setup["span_id"] in ids, f"A's span not in workflow traces; ids={ids}"

    def test_org_admin_a_can_read_own_span_detail(self, client, setup):
        """Org-admin A gets 200 with their span detail by id (positive control)."""
        from unittest.mock import patch
        from src.backend.crew_ai.optimization import pg_compat

        def _test_db():
            return pg_compat.connect(setup["trace_dsn"], autocommit=True)

        with patch("src.backend.api.trace_endpoints._get_db", side_effect=_test_db):
            resp = client.get(
                f"/api/admin/traces/{setup['span_id']}",
                headers={"Authorization": f"Bearer {setup['tok_a']}"},
            )
        assert resp.status_code == 200
        assert resp.json()["id"] == setup["span_id"]

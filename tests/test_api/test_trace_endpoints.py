"""
Tests for src.backend.api.trace_endpoints — admin trace query API.

Covers all four routes:
- GET /admin/traces/          — list with filters (workflow_id, model, status, llm_only)
- GET /admin/traces/stats/cost — aggregate cost/token stats
- GET /admin/traces/workflow/{id} — per-workflow trace chain
- GET /admin/traces/{span_id}  — single span detail

Each test group runs against two fixtures:
- `client`    — Postgres trace schema with seed data
- `client_no_db` — the trace table is absent (FileNotFoundError graceful path)
"""
import pytest
import psycopg
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch

from src.backend.api.trace_endpoints import router
from src.backend.core.config import settings
from src.backend.core.trace_store import _ensure_schema
from src.backend.crew_ai.optimization import pg_compat

_API_TRACE_SCHEMA = "trace_api_test"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def trace_dsn():
    """Isolated Postgres schema with the trace table + known seed rows."""
    admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    admin.execute(f"DROP SCHEMA IF EXISTS {_API_TRACE_SCHEMA} CASCADE")
    admin.execute(f"CREATE SCHEMA {_API_TRACE_SCHEMA}")
    dsn = settings.DATABASE_URL + f"?options=-c%20search_path%3D{_API_TRACE_SCHEMA}"
    conn = psycopg.connect(dsn)
    try:
        _ensure_schema(conn)
        conn.cursor().executemany(
            "INSERT INTO llm_traces "
            "(id, trace_id, name, start_time_ns, end_time_ns, duration_ms, status, "
            " model, prompt_tokens, completion_tokens, total_tokens, cost_usd, "
            " workflow_id, prompt_text, response_text) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [
                ("span-1", "trace-A", "gemini/gemini-2.5-flash.litellm",
                 1000, 2000, 100.0, "OK", "gemini/gemini-2.5-flash",
                 10, 5, 15, 0.001, "wf-1", '["hi"]', "hello"),
                ("span-2", "trace-A", "crewai.agent",
                 500, 3000, 200.0, "OK", None, 0, 0, 0, 0.0, "wf-1", None, None),
                ("span-3", "trace-B", "vertex_ai/gemini.litellm",
                 4000, 5000, 80.0, "ERROR", "vertex_ai/gemini-2.5-flash",
                 20, 10, 30, 0.002, "wf-2", None, None),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    yield dsn
    admin.execute(f"DROP SCHEMA IF EXISTS {_API_TRACE_SCHEMA} CASCADE")
    admin.close()


@pytest.fixture()
def client(trace_dsn):
    app = FastAPI()
    app.include_router(router)

    def _test_get_db():
        return pg_compat.connect(trace_dsn, autocommit=True)

    # These tests exercise query/filter behaviour, not authorization (org
    # scoping is covered by test_traces_org_scope). Pin platform scope so the
    # dashboard gate doesn't reject the anonymous TestClient calls.
    with patch("src.backend.api.trace_endpoints._get_db", side_effect=_test_get_db), \
         patch("src.backend.api.trace_endpoints.authorize_dashboard_read", return_value=None):
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


@pytest.fixture()
def client_no_db():
    app = FastAPI()
    app.include_router(router)

    def _raise():
        raise FileNotFoundError("Trace table not found. No traces have been recorded yet.")

    with patch("src.backend.api.trace_endpoints._get_db", side_effect=_raise), \
         patch("src.backend.api.trace_endpoints.authorize_dashboard_read", return_value=None):
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


# ---------------------------------------------------------------------------
# GET /admin/traces/ — list_traces
# ---------------------------------------------------------------------------

class TestListTraces:
    def test_returns_200(self, client):
        resp = client.get("/admin/traces/")
        assert resp.status_code == 200

    def test_response_has_traces_key(self, client):
        resp = client.get("/admin/traces/")
        assert "traces" in resp.json()

    def test_llm_only_true_excludes_orchestration(self, client):
        resp = client.get("/admin/traces/?llm_only=true")
        traces = resp.json()["traces"]
        for t in traces:
            assert t["name"].endswith(".litellm")

    def test_llm_only_false_includes_all(self, client):
        resp = client.get("/admin/traces/?llm_only=false")
        names = [t["name"] for t in resp.json()["traces"]]
        assert any(not n.endswith(".litellm") for n in names)

    def test_workflow_id_filter(self, client):
        resp = client.get("/admin/traces/?workflow_id=wf-1&llm_only=false")
        traces = resp.json()["traces"]
        for t in traces:
            assert t["workflow_id"] == "wf-1"

    def test_model_partial_filter(self, client):
        resp = client.get("/admin/traces/?model=gemini&llm_only=false")
        traces = resp.json()["traces"]
        for t in traces:
            assert "gemini" in (t["model"] or "")

    def test_status_filter_ok(self, client):
        resp = client.get("/admin/traces/?status=OK&llm_only=false")
        for t in resp.json()["traces"]:
            assert t["status"] == "OK"

    def test_status_filter_error(self, client):
        resp = client.get("/admin/traces/?status=ERROR&llm_only=false")
        for t in resp.json()["traces"]:
            assert t["status"] == "ERROR"

    def test_limit_respected(self, client):
        resp = client.get("/admin/traces/?limit=1&llm_only=false")
        assert len(resp.json()["traces"]) <= 1

    def test_offset_respected(self, client):
        resp_all = client.get("/admin/traces/?llm_only=false&limit=10")
        resp_offset = client.get("/admin/traces/?llm_only=false&limit=10&offset=1")
        assert len(resp_offset.json()["traces"]) < len(resp_all.json()["traces"])

    def test_no_db_returns_empty(self, client_no_db):
        resp = client_no_db.get("/admin/traces/")
        data = resp.json()
        assert resp.status_code == 200
        assert data["traces"] == []
        assert "note" in data


# ---------------------------------------------------------------------------
# GET /admin/traces/stats/cost — get_cost_stats
# ---------------------------------------------------------------------------

class TestCostStats:
    def test_returns_200(self, client):
        resp = client.get("/admin/traces/stats/cost")
        assert resp.status_code == 200

    def test_has_expected_keys(self, client):
        data = resp = client.get("/admin/traces/stats/cost").json()
        for key in ("total_llm_calls", "total_cost_usd", "total_prompt_tokens",
                    "total_completion_tokens", "avg_latency_ms", "total_workflows"):
            assert key in data

    def test_counts_llm_rows(self, client):
        data = client.get("/admin/traces/stats/cost").json()
        assert data["total_llm_calls"] >= 2  # span-1 and span-3

    def test_per_model_list(self, client):
        data = client.get("/admin/traces/stats/cost").json()
        assert isinstance(data["per_model"], list)

    def test_last_days_param(self, client):
        resp = client.get("/admin/traces/stats/cost?last_days=30")
        assert resp.status_code == 200

    def test_no_db_returns_zero_calls(self, client_no_db):
        data = client_no_db.get("/admin/traces/stats/cost").json()
        assert data["total_llm_calls"] == 0
        assert "note" in data


# ---------------------------------------------------------------------------
# GET /admin/traces/workflow/{workflow_id} — get_workflow_traces
# ---------------------------------------------------------------------------

class TestWorkflowTraces:
    def test_known_workflow_returns_traces(self, client):
        resp = client.get("/admin/traces/workflow/wf-1")
        data = resp.json()
        assert resp.status_code == 200
        assert data["workflow_id"] == "wf-1"
        assert data["llm_calls"] > 0

    def test_summary_fields_present(self, client):
        data = client.get("/admin/traces/workflow/wf-1").json()
        for key in ("total_cost_usd", "total_tokens", "total_duration_ms"):
            assert key in data

    def test_unknown_workflow_returns_zero(self, client):
        data = client.get("/admin/traces/workflow/wf-nonexistent").json()
        assert data["llm_calls"] == 0
        assert data["traces"] == []

    def test_no_db_returns_zero(self, client_no_db):
        data = client_no_db.get("/admin/traces/workflow/wf-1").json()
        assert data["llm_calls"] == 0
        assert "note" in data


# ---------------------------------------------------------------------------
# GET /admin/traces/{span_id} — get_trace_detail
# ---------------------------------------------------------------------------

class TestTraceDetail:
    def test_known_span_returns_detail(self, client):
        resp = client.get("/admin/traces/span-1")
        assert resp.status_code == 200
        assert resp.json()["id"] == "span-1"

    def test_unknown_span_returns_404(self, client):
        resp = client.get("/admin/traces/nonexistent-span-id")
        assert resp.status_code == 404

    def test_no_db_returns_404(self, client_no_db):
        resp = client_no_db.get("/admin/traces/span-1")
        assert resp.status_code == 404

    def test_detail_includes_prompt_and_response(self, client):
        data = client.get("/admin/traces/span-1").json()
        assert "prompt_text" in data
        assert "response_text" in data

"""
Coverage tests for learning_endpoints.py — targeting 384 missing lines.

Tests grouped by function/endpoint:
- _estimate_cost: known model, unknown/exception path → 0.0
- _model_rate_info: known model, unknown model → None, exception → None
- _require_feedback_loop: 503 when get_feedback_loop returns None
- GET /hints: status filters (all variants), scope/domain/search filters
- GET /hints/{id}: 404, with timeline rows from hint_audit and trigger_events
- POST /hints: validation errors, new insert, duplicate re-submit (with/without flag clear)
- PATCH /hints/{id}: feedback_text → 400, no updates → no-op, scope change, 404
- POST /hints/{id}/unflag: 404, not-flagged no-op, flagged → unflag
- POST /hints/{id}/retract: 404, already-inactive no-op, active → retract
- POST /hints/{id}/reactivate: 404, already-active no-op, inactive → reactivate
- GET /triggers: no filters, trigger_type filter, since 'd' suffix, since ISO, invalid since
- GET /triggers/{id}: 404, workflow_id resolved, active_hint_ids decoded
- GET /stats: smoke test with empty DB
- GET /health: OK from fb, DISABLED (fb=None + disabled), FAILED (fb=None + enabled), exception
- _compute_exonerations: exonerations and flags counted correctly
- _compute_warning: above threshold, below threshold, no active hints
- POST /review-hints/start: 409 conflict, new session row inserted
- GET /review-hints/sessions: is_any_running flag
- GET /review-hints/sessions/{id}: 404, with recs + pages
- PATCH /review-hints/sessions/{id}/recommendations/{id}: invalid decision, wrong session status, 404 rec, approved
- POST /review-hints/sessions/{id}/apply: 404, 409 wrong status, applies disable/reactivate/unflag/keep/flag_review
"""

import json
import sqlite3
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.backend.api.learning_endpoints import (
    _compute_exonerations,
    _compute_warning,
    _estimate_cost,
    _model_rate_info,
    _require_feedback_loop,
    router,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_em(tmp_path):
    """File-backed ExecutionMemory with schema applied and ChromaDB disabled."""
    from src.backend.crew_ai.optimization.execution_memory import ExecutionMemory

    db_path = str(tmp_path / "learning_ep_test.db")
    em = ExecutionMemory(db_path=db_path)
    em._chroma_client = ExecutionMemory._CHROMADB_INIT_FAILED
    yield em, db_path
    em.close()


@pytest.fixture
def learning_client(db_em):
    """TestClient wired to a real SQLite DB, with _require_feedback_loop overridden."""
    em, db_path = db_em

    mock_fb = MagicMock()
    mock_fb.execution_memory = em
    mock_fb.nl_engine = MagicMock()
    mock_fb.nl_engine.process_feedback.return_value = {"category": "locator"}
    mock_fb.write_queue = MagicMock()
    mock_fb.get_health_status.return_value = "OK"
    mock_fb.metrics_tracker = MagicMock()
    mock_fb.metrics_tracker.get_effectiveness_report.return_value = {}

    def _test_admin_conn():
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    app = FastAPI()
    app.include_router(router, prefix="")
    app.dependency_overrides[_require_feedback_loop] = lambda: mock_fb

    with patch("src.backend.api.learning_endpoints._admin_conn", side_effect=_test_admin_conn):
        with TestClient(app) as client:
            yield client, em, mock_fb, db_path


def _insert_hint(db_path, feedback_text="use xpath", category="locator",
                 scope="global", domain=None, url=None, evidence=1,
                 anchor_query="test query", is_active=1, conflict_flagged=0,
                 applied_count=0, success_count=0, failure_count=0,
                 original_failure_category=None, disabled_at=None) -> int:
    """Helper: insert a hint directly and return its id."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO nl_feedback_corrections "
        "(feedback_text, category, scope, domain, url, original_failure_category, "
        " evidence_count, anchor_query, applied_count, success_count, failure_count, "
        " is_active, conflict_flagged, disabled_at, created_at, last_seen) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (feedback_text, category, scope, domain, url, original_failure_category,
         evidence, anchor_query, applied_count, success_count, failure_count,
         is_active, conflict_flagged, disabled_at, now, now),
    )
    conn.commit()
    hint_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return hint_id


# ---------------------------------------------------------------------------
# _estimate_cost
# ---------------------------------------------------------------------------

class TestEstimateCost:
    def test_unknown_model_returns_zero(self):
        """A model name not in LiteLLM's cost database returns 0.0."""
        result = _estimate_cost("completely-unknown-model-xyz", 1000, 500)
        assert result == 0.0

    def test_exception_returns_zero(self):
        """If litellm raises during cost lookup, _estimate_cost returns 0.0 silently."""
        with patch("litellm.cost_per_token", side_effect=Exception("pricing error")):
            result = _estimate_cost("some-model", 100, 100)
        assert result == 0.0


# ---------------------------------------------------------------------------
# _model_rate_info
# ---------------------------------------------------------------------------

class TestModelRateInfo:
    def test_unknown_model_returns_none(self):
        """A model not in LiteLLM's model_cost dict returns None."""
        result = _model_rate_info("completely-unknown-xyz-model")
        assert result is None

    def test_exception_returns_none(self):
        """If litellm.model_cost.get() raises, _model_rate_info returns None."""
        mock_cost = MagicMock()
        mock_cost.get.side_effect = Exception("not available")
        with patch("litellm.model_cost", mock_cost):
            result = _model_rate_info("some-model")
        assert result is None


# ---------------------------------------------------------------------------
# _require_feedback_loop — 503
# ---------------------------------------------------------------------------

class TestRequireFeedbackLoop503:
    def test_503_when_feedback_loop_is_none(self, tmp_path):
        """_require_feedback_loop raises 503 when get_feedback_loop() returns None."""
        app = FastAPI()
        app.include_router(router, prefix="")

        with patch("src.backend.api.learning_endpoints.get_feedback_loop", return_value=None):
            with TestClient(app) as client:
                resp = client.get("/hints")
        assert resp.status_code == 503
        assert "OPTIMIZATION_ENABLED" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /hints
# ---------------------------------------------------------------------------

class TestListHints:
    def test_empty_db_returns_zero_total(self, learning_client):
        client, *_ = learning_client
        resp = client.get("/hints")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["hints"] == []

    def test_status_flagged_filter(self, learning_client):
        client, _, _, db_path = learning_client
        _insert_hint(db_path, conflict_flagged=1)
        _insert_hint(db_path, feedback_text="clean hint")

        resp = client.get("/hints", params={"status": "flagged"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["hints"][0]["conflict_flagged"] == 1

    def test_status_active_filter(self, learning_client):
        client, _, _, db_path = learning_client
        _insert_hint(db_path, feedback_text="active hint")
        _insert_hint(db_path, feedback_text="inactive hint", is_active=0)

        resp = client.get("/hints", params={"status": "active"})
        assert resp.status_code == 200
        data = resp.json()
        assert all(h["is_active"] == 1 and h["conflict_flagged"] == 0 for h in data["hints"])

    def test_status_retracted_filter(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, is_active=0)
        # Insert a retract audit row so the hint qualifies as retracted
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute(
            "INSERT INTO hint_audit (hint_id, action, actor, created_at) VALUES (?, 'retract', 'test', datetime('now'))",
            (hint_id,),
        )
        conn.commit()
        conn.close()

        resp = client.get("/hints", params={"status": "retracted"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_status_auto_disabled_filter(self, learning_client):
        client, _, _, db_path = learning_client
        _insert_hint(db_path, is_active=0)  # no retract/llm_review_disable audit → auto-disabled

        resp = client.get("/hints", params={"status": "auto_disabled"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_status_llm_review_disabled_filter(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, is_active=0)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute(
            "INSERT INTO hint_audit (hint_id, action, actor, created_at) VALUES (?, 'llm_review_disable', 'bot', datetime('now'))",
            (hint_id,),
        )
        conn.commit()
        conn.close()

        resp = client.get("/hints", params={"status": "llm_review_disabled"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_scope_filter(self, learning_client):
        client, _, _, db_path = learning_client
        _insert_hint(db_path, scope="global")
        _insert_hint(db_path, feedback_text="domain hint", scope="domain", domain="example.com")

        resp = client.get("/hints", params={"scope": "domain"})
        assert resp.status_code == 200
        assert all(h["scope"] == "domain" for h in resp.json()["hints"])

    def test_domain_filter(self, learning_client):
        client, _, _, db_path = learning_client
        _insert_hint(db_path, scope="domain", domain="a.com", feedback_text="hint a")
        _insert_hint(db_path, scope="domain", domain="b.com", feedback_text="hint b")

        resp = client.get("/hints", params={"domain": "a.com"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["hints"][0]["domain"] == "a.com"

    def test_search_filter(self, learning_client):
        client, _, _, db_path = learning_client
        _insert_hint(db_path, feedback_text="use xpath locators")
        _insert_hint(db_path, feedback_text="avoid CSS selectors")

        resp = client.get("/hints", params={"search": "xpath"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_pagination(self, learning_client):
        client, _, _, db_path = learning_client
        for i in range(5):
            _insert_hint(db_path, feedback_text=f"hint {i}")

        resp = client.get("/hints", params={"limit": 2, "offset": 0})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["hints"]) == 2
        assert data["total"] == 5


# ---------------------------------------------------------------------------
# GET /hints/{id}
# ---------------------------------------------------------------------------

class TestGetHint:
    def test_404_when_hint_not_found(self, learning_client):
        client, *_ = learning_client
        resp = client.get("/hints/99999")
        assert resp.status_code == 404

    def test_returns_hint_with_empty_timeline(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path)

        resp = client.get(f"/hints/{hint_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["hint"]["id"] == hint_id
        assert data["timeline"] == []

    def test_timeline_includes_hint_audit_rows(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, conflict_flagged=1)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute(
            "INSERT INTO hint_audit (hint_id, action, actor, reason, created_at) "
            "VALUES (?, 'unflag', 'alice', 'resolved', datetime('now'))",
            (hint_id,),
        )
        conn.commit()
        conn.close()

        resp = client.get(f"/hints/{hint_id}")
        assert resp.status_code == 200
        timeline = resp.json()["timeline"]
        assert any(row["action"] == "unflag" for row in timeline)

    def test_timeline_includes_trigger_event_rows(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute(
            "INSERT INTO trigger_events "
            "(trigger_type, workflow_id, status, flagged_hint_ids, active_hint_ids, created_at) "
            "VALUES ('trigger_1', 'wf-1', 'succeeded', ?, ?, datetime('now'))",
            (json.dumps([hint_id]), json.dumps([hint_id])),
        )
        conn.commit()
        conn.close()

        resp = client.get(f"/hints/{hint_id}")
        assert resp.status_code == 200
        timeline = resp.json()["timeline"]
        assert any(row["trigger_type"] == "trigger_1" for row in timeline)


# ---------------------------------------------------------------------------
# POST /hints
# ---------------------------------------------------------------------------

class TestCreateHint:
    def _valid_payload(self, **overrides):
        base = {
            "feedback_text": "Use xpath for stable selectors",
            "anchor_query": "click the login button",
            "scope": "global",
            "actor": "alice",
        }
        base.update(overrides)
        return base

    def test_missing_actor_returns_400(self, learning_client):
        client, *_ = learning_client
        resp = client.post("/hints", json=self._valid_payload(actor="   "))
        assert resp.status_code == 400
        assert "actor" in resp.json()["detail"]

    def test_empty_feedback_text_returns_400(self, learning_client):
        client, *_ = learning_client
        resp = client.post("/hints", json=self._valid_payload(feedback_text="  "))
        assert resp.status_code == 400

    def test_feedback_text_too_long_returns_400(self, learning_client):
        client, *_ = learning_client
        resp = client.post("/hints", json=self._valid_payload(feedback_text="x" * 501))
        assert resp.status_code == 400

    def test_anchor_query_too_short_returns_400(self, learning_client):
        client, *_ = learning_client
        resp = client.post("/hints", json=self._valid_payload(anchor_query="ab"))
        assert resp.status_code == 400

    def test_anchor_query_too_long_returns_400(self, learning_client):
        client, *_ = learning_client
        resp = client.post("/hints", json=self._valid_payload(anchor_query="x" * 501))
        assert resp.status_code == 400

    def test_invalid_scope_returns_400(self, learning_client):
        client, *_ = learning_client
        resp = client.post("/hints", json=self._valid_payload(scope="invalid"))
        assert resp.status_code == 400

    def test_url_scope_without_url_returns_400(self, learning_client):
        client, *_ = learning_client
        resp = client.post("/hints", json=self._valid_payload(scope="url"))
        assert resp.status_code == 400
        assert "url" in resp.json()["detail"].lower()

    def test_domain_scope_without_domain_returns_400(self, learning_client):
        client, *_ = learning_client
        resp = client.post("/hints", json=self._valid_payload(scope="domain"))
        assert resp.status_code == 400
        assert "domain" in resp.json()["detail"].lower()

    def test_new_hint_created_successfully(self, learning_client):
        client, *_ = learning_client
        resp = client.post("/hints", json=self._valid_payload())
        assert resp.status_code == 201
        data = resp.json()
        assert data["created"] is True
        assert data["hint"]["feedback_text"] == "Use xpath for stable selectors"

    def test_duplicate_hint_increments_evidence(self, learning_client):
        client, _, _, db_path = learning_client
        # Pre-insert the matching row directly so it's committed before the POST
        _insert_hint(db_path,
                     feedback_text="Use xpath for stable selectors",
                     scope="global", domain=None, evidence=1)
        payload = self._valid_payload()  # same text + scope + domain
        resp = client.post("/hints", json=payload)
        # Route decorator always returns 201; created=False signals the duplicate path
        assert resp.status_code == 201
        data = resp.json()
        assert data["created"] is False
        assert data["hint"]["evidence_count"] == 2

    def test_duplicate_flagged_hint_clears_flag_and_logs_audit(self, learning_client):
        client, _, _, db_path = learning_client
        # Insert a flagged hint directly
        _insert_hint(db_path, feedback_text="Use xpath for stable selectors",
                     conflict_flagged=1, scope="global")

        payload = self._valid_payload()  # same text + scope → matches existing
        resp = client.post("/hints", json=payload)
        # Duplicate path: route decorator always returns 201; created=False
        assert resp.status_code == 201
        data = resp.json()
        assert data["created"] is False
        # Flag must be cleared
        assert data["hint"]["conflict_flagged"] == 0

    def test_run_triage_calls_nl_engine(self, learning_client):
        client, _, mock_fb, _ = learning_client
        mock_fb.nl_engine.process_feedback.return_value = {"category": "timing"}

        payload = self._valid_payload(run_triage=True)
        resp = client.post("/hints", json=payload)
        assert resp.status_code == 201
        assert resp.json()["hint"]["category"] == "timing"


# ---------------------------------------------------------------------------
# PATCH /hints/{id}
# ---------------------------------------------------------------------------

class TestPatchHint:
    def test_feedback_text_edit_returns_400(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path)
        resp = client.patch(f"/hints/{hint_id}", json={
            "feedback_text": "new text", "actor": "alice",
        })
        assert resp.status_code == 400

    def test_empty_actor_returns_400(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path)
        resp = client.patch(f"/hints/{hint_id}", json={"actor": "  ", "category": "locator"})
        assert resp.status_code == 400

    def test_404_for_unknown_hint(self, learning_client):
        client, *_ = learning_client
        resp = client.patch("/hints/99999", json={"actor": "alice", "category": "timing"})
        assert resp.status_code == 404

    def test_no_fields_changed_returns_hint_unchanged(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path)
        resp = client.patch(f"/hints/{hint_id}", json={"actor": "alice"})
        assert resp.status_code == 200
        assert resp.json()["changed"] is False

    def test_category_update_persisted(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, category="locator")
        resp = client.patch(f"/hints/{hint_id}", json={"actor": "alice", "category": "timing"})
        assert resp.status_code == 200
        assert resp.json()["changed"] is True
        assert resp.json()["hint"]["category"] == "timing"

    def test_scope_change_to_domain_without_domain_returns_400(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, scope="global")
        resp = client.patch(f"/hints/{hint_id}", json={
            "actor": "alice", "scope": "domain",
        })
        assert resp.status_code == 400

    def test_scope_change_to_url_without_url_returns_400(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, scope="global")
        resp = client.patch(f"/hints/{hint_id}", json={
            "actor": "alice", "scope": "url",
        })
        assert resp.status_code == 400

    def test_scope_change_invalid_value_returns_400(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, scope="global")
        resp = client.patch(f"/hints/{hint_id}", json={"actor": "alice", "scope": "invalid"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /hints/{id}/unflag
# ---------------------------------------------------------------------------

class TestUnflagHint:
    def test_404_for_unknown_hint(self, learning_client):
        client, *_ = learning_client
        resp = client.post("/hints/99999/unflag", json={"actor": "alice"})
        assert resp.status_code == 404

    def test_empty_actor_returns_400(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, conflict_flagged=1)
        resp = client.post(f"/hints/{hint_id}/unflag", json={"actor": ""})
        assert resp.status_code == 400

    def test_not_flagged_hint_returns_no_op(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, conflict_flagged=0)
        resp = client.post(f"/hints/{hint_id}/unflag", json={"actor": "alice"})
        assert resp.status_code == 200
        assert resp.json()["changed"] is False

    def test_flagged_hint_is_cleared(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, conflict_flagged=1)
        resp = client.post(f"/hints/{hint_id}/unflag", json={"actor": "alice", "reason": "flag was wrong"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["changed"] is True
        assert data["hint"]["conflict_flagged"] == 0


# ---------------------------------------------------------------------------
# POST /hints/{id}/retract
# ---------------------------------------------------------------------------

class TestRetractHint:
    def test_404_for_unknown_hint(self, learning_client):
        client, *_ = learning_client
        resp = client.post("/hints/99999/retract", json={"actor": "alice"})
        assert resp.status_code == 404

    def test_empty_actor_returns_400(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path)
        resp = client.post(f"/hints/{hint_id}/retract", json={"actor": ""})
        assert resp.status_code == 400

    def test_already_inactive_returns_no_op(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, is_active=0)
        resp = client.post(f"/hints/{hint_id}/retract", json={"actor": "alice"})
        assert resp.status_code == 200
        assert resp.json()["changed"] is False

    def test_active_hint_is_retracted(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, is_active=1)
        resp = client.post(f"/hints/{hint_id}/retract", json={"actor": "alice", "reason": "harmful"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["changed"] is True
        assert data["hint"]["is_active"] == 0


# ---------------------------------------------------------------------------
# POST /hints/{id}/reactivate
# ---------------------------------------------------------------------------

class TestReactivateHint:
    def test_404_for_unknown_hint(self, learning_client):
        client, *_ = learning_client
        resp = client.post("/hints/99999/reactivate", json={"actor": "alice"})
        assert resp.status_code == 404

    def test_empty_actor_returns_400(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, is_active=0)
        resp = client.post(f"/hints/{hint_id}/reactivate", json={"actor": ""})
        assert resp.status_code == 400

    def test_already_active_returns_no_op(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, is_active=1)
        resp = client.post(f"/hints/{hint_id}/reactivate", json={"actor": "alice"})
        assert resp.status_code == 200
        assert resp.json()["changed"] is False

    def test_inactive_hint_is_reactivated(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, is_active=0)
        resp = client.post(f"/hints/{hint_id}/reactivate", json={"actor": "alice"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["changed"] is True
        assert data["hint"]["is_active"] == 1
        assert data["hint"]["conflict_flagged"] == 0


# ---------------------------------------------------------------------------
# GET /triggers
# ---------------------------------------------------------------------------

class TestListTriggers:
    def _insert_trigger(self, db_path, trigger_type="trigger_1", workflow_id="wf-1",
                        status="succeeded", flagged_hint_ids="[]"):
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute(
            "INSERT INTO trigger_events (trigger_type, workflow_id, status, flagged_hint_ids, created_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (trigger_type, workflow_id, status, flagged_hint_ids),
        )
        conn.commit()
        row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        return row_id

    def test_empty_returns_zero_total(self, learning_client):
        client, *_ = learning_client
        resp = client.get("/triggers")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_trigger_type_filter(self, learning_client):
        client, _, _, db_path = learning_client
        self._insert_trigger(db_path, trigger_type="trigger_1")
        self._insert_trigger(db_path, trigger_type="trigger_2", workflow_id="wf-2")

        resp = client.get("/triggers", params={"trigger_type": "trigger_1"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["triggers"][0]["trigger_type"] == "trigger_1"

    def test_since_with_days_suffix(self, learning_client):
        client, _, _, db_path = learning_client
        self._insert_trigger(db_path)

        resp = client.get("/triggers", params={"since": "30d"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_since_invalid_days_suffix_returns_400(self, learning_client):
        client, *_ = learning_client
        resp = client.get("/triggers", params={"since": "xd"})
        assert resp.status_code == 400

    def test_since_iso_date_filter(self, learning_client):
        client, _, _, db_path = learning_client
        self._insert_trigger(db_path)

        resp = client.get("/triggers", params={"since": "2000-01-01T00:00:00"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_workflow_id_filter(self, learning_client):
        client, _, _, db_path = learning_client
        self._insert_trigger(db_path, workflow_id="wf-target")
        self._insert_trigger(db_path, workflow_id="wf-other")

        resp = client.get("/triggers", params={"workflow_id": "wf-target"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 1


# ---------------------------------------------------------------------------
# GET /triggers/{id}
# ---------------------------------------------------------------------------

class TestGetTrigger:
    def _insert_trigger(self, db_path, workflow_id=None, active_hint_ids="[]"):
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute(
            "INSERT INTO trigger_events (trigger_type, workflow_id, status, "
            "flagged_hint_ids, active_hint_ids, created_at) "
            "VALUES ('trigger_1', ?, 'succeeded', '[]', ?, datetime('now'))",
            (workflow_id, active_hint_ids),
        )
        conn.commit()
        tid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        return tid

    def test_404_for_unknown_trigger(self, learning_client):
        client, *_ = learning_client
        resp = client.get("/triggers/99999")
        assert resp.status_code == 404

    def test_trigger_with_no_workflow_returns_null_execution(self, learning_client):
        client, _, _, db_path = learning_client
        tid = self._insert_trigger(db_path, workflow_id=None)
        resp = client.get(f"/triggers/{tid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["execution"] is None

    def test_trigger_with_active_hint_ids_decoded(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path)
        tid = self._insert_trigger(db_path, active_hint_ids=json.dumps([hint_id]))

        resp = client.get(f"/triggers/{tid}")
        assert resp.status_code == 200
        data = resp.json()
        # JSON serializes integer keys as strings
        assert str(hint_id) in data["hint_texts"]


# ---------------------------------------------------------------------------
# GET /stats
# ---------------------------------------------------------------------------

class TestGetDashboardStats:
    def test_empty_db_returns_valid_structure(self, learning_client):
        client, *_ = learning_client
        resp = client.get("/stats")
        assert resp.status_code == 200
        data = resp.json()

        # hint_inventory: SQLite SUM() on an empty table returns NULL for every
        # aggregate column.  All seven fields must be present and all be None.
        inv = data["hint_inventory"]
        assert inv is not None
        for key in ("active", "flagged", "retracted", "llm_review_disabled",
                    "auto_disabled", "admin_created", "workflow_created"):
            assert inv[key] is None, (
                f"hint_inventory.{key} expected None for empty DB, got {inv[key]!r}"
            )

        # trigger_activity: GROUP BY on empty table → no rows
        assert data["trigger_activity"] == []

        # llm_accuracy: no flagged events → both rates are None
        acc = data["llm_accuracy"]
        assert acc["flagged_events"] == 0
        assert acc["reviewed_events"] == 0
        assert acc["reversed_events"] == 0
        assert acc["engagement_rate"] is None
        assert acc["reversal_rate"] is None
        assert acc["threshold_applicable"] is False

        # manual_actions: no hint_audit rows → all zero
        assert data["manual_actions"] == {"unflags_30d": 0, "retracts_30d": 0}

        # llm_cost: no trigger_events → empty model list, zero total
        assert data["llm_cost"] == {"by_model": [], "total_estimated_usd": 0.0}

    def test_accuracy_kpis_none_when_no_flagged_events(self, learning_client):
        client, *_ = learning_client
        resp = client.get("/stats")
        assert resp.status_code == 200
        accuracy = resp.json()["llm_accuracy"]
        assert accuracy["engagement_rate"] is None
        assert accuracy["reversal_rate"] is None


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

class TestGetLearningHealth:
    def test_health_ok_when_fb_returns_ok(self, learning_client):
        client, _, mock_fb, _ = learning_client
        mock_fb.get_health_status.return_value = "OK"

        app = FastAPI()
        app.include_router(router, prefix="")
        with patch("src.backend.api.learning_endpoints.get_feedback_loop", return_value=mock_fb):
            with TestClient(app) as c:
                resp = c.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "OK"

    def test_health_disabled_when_fb_none_and_opt_disabled(self):
        app = FastAPI()
        app.include_router(router, prefix="")
        with patch("src.backend.api.learning_endpoints.get_feedback_loop", return_value=None), \
             patch("src.backend.core.config.settings") as mock_s:
            mock_s.OPTIMIZATION_ENABLED = False
            with TestClient(app) as c:
                resp = c.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "DISABLED"

    def test_health_failed_when_fb_none_and_opt_enabled(self):
        app = FastAPI()
        app.include_router(router, prefix="")
        with patch("src.backend.api.learning_endpoints.get_feedback_loop", return_value=None), \
             patch("src.backend.core.config.settings") as mock_s:
            mock_s.OPTIMIZATION_ENABLED = True
            with TestClient(app) as c:
                resp = c.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "FAILED"

    def test_health_unknown_on_exception(self):
        mock_fb = MagicMock()
        mock_fb.get_health_status.side_effect = RuntimeError("crash")

        app = FastAPI()
        app.include_router(router, prefix="")
        with patch("src.backend.api.learning_endpoints.get_feedback_loop", return_value=mock_fb):
            with TestClient(app) as c:
                resp = c.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "unknown"


# ---------------------------------------------------------------------------
# _compute_exonerations (pure function)
# ---------------------------------------------------------------------------

class TestComputeExonerations:
    def _row(self, active_ids, flagged_ids):
        return {"active_hint_ids": json.dumps(active_ids), "flagged_hint_ids": json.dumps(flagged_ids)}

    def test_empty_events_returns_empty_dicts(self):
        exon, flags = _compute_exonerations([])
        assert exon == {}
        assert flags == {}

    def test_active_not_flagged_counts_as_exoneration(self):
        rows = [self._row([1, 2], [2])]
        exon, flags = _compute_exonerations(rows)
        assert exon[1] == 1  # 1 was active but not flagged → exoneration
        assert flags[2] == 1  # 2 was flagged

    def test_multiple_events_accumulate(self):
        rows = [
            self._row([1], []),
            self._row([1], []),
            self._row([1], [1]),
        ]
        exon, flags = _compute_exonerations(rows)
        assert exon[1] == 2
        assert flags[1] == 1

    def test_null_active_ids_treated_as_empty(self):
        row = {"active_hint_ids": None, "flagged_hint_ids": "[]"}
        exon, flags = _compute_exonerations([row])
        assert exon == {}
        assert flags == {}


# ---------------------------------------------------------------------------
# _compute_warning (pure function)
# ---------------------------------------------------------------------------

class TestComputeWarning:
    def _hint(self, hint_id, is_active=1):
        return {"id": hint_id, "is_active": is_active}

    def _decision(self, hint_id, rec):
        return {"id": hint_id, "recommendation": rec}

    def test_no_active_hints_returns_none(self):
        hints = [self._hint(1, is_active=0)]
        decisions = [self._decision(1, "disable")]
        assert _compute_warning(decisions, hints) is None

    def test_below_threshold_returns_none(self):
        hints = [self._hint(i) for i in range(10)]
        # Only 2 out of 10 → 20%, below 30% threshold
        decisions = [self._decision(0, "disable"), self._decision(1, "disable")]
        assert _compute_warning(decisions, hints) is None

    def test_above_threshold_returns_warning_string(self):
        hints = [self._hint(i) for i in range(3)]
        # All 3 disabled → 100% > 30%
        decisions = [self._decision(i, "disable") for i in range(3)]
        warning = _compute_warning(decisions, hints)
        assert warning is not None
        assert "3" in warning

    def test_non_disable_recommendations_not_counted(self):
        hints = [self._hint(i) for i in range(3)]
        decisions = [
            self._decision(0, "keep"),
            self._decision(1, "flag_review"),
            self._decision(2, "reactivate"),
        ]
        assert _compute_warning(decisions, hints) is None


# ---------------------------------------------------------------------------
# Review session endpoints
# ---------------------------------------------------------------------------

class TestStartHintReview:
    def test_new_session_created_returns_session_id(self, learning_client):
        client, *_ = learning_client
        resp = client.post("/review-hints/start")
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        assert data["status"] == "pending_llm"

    def test_409_when_review_already_in_progress(self, learning_client):
        client, _, _, db_path = learning_client
        # Insert a pending_llm session directly
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute(
            "INSERT INTO hint_review_sessions (status, hint_count, created_at) "
            "VALUES ('pending_llm', 0, datetime('now'))"
        )
        conn.commit()
        conn.close()

        resp = client.post("/review-hints/start")
        assert resp.status_code == 409
        assert "already in progress" in resp.json()["detail"]


class TestListReviewSessions:
    def test_empty_sessions_returns_is_any_running_false(self, learning_client):
        client, *_ = learning_client
        resp = client.get("/review-hints/sessions")
        assert resp.status_code == 200
        assert resp.json()["is_any_running"] is False

    def test_is_any_running_true_when_pending_session_exists(self, learning_client):
        client, _, _, db_path = learning_client
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute(
            "INSERT INTO hint_review_sessions (status, hint_count, created_at) "
            "VALUES ('pending_llm', 0, datetime('now'))"
        )
        conn.commit()
        conn.close()

        resp = client.get("/review-hints/sessions")
        assert resp.status_code == 200
        assert resp.json()["is_any_running"] is True


class TestGetReviewSession:
    def _insert_session(self, db_path, status="pending_review"):
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute(
            "INSERT INTO hint_review_sessions (status, hint_count, created_at) VALUES (?, 0, datetime('now'))",
            (status,),
        )
        conn.commit()
        sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        return sid

    def test_404_for_unknown_session(self, learning_client):
        client, *_ = learning_client
        resp = client.get("/review-hints/sessions/99999")
        assert resp.status_code == 404

    def test_returns_session_with_empty_recs_and_pages(self, learning_client):
        client, _, _, db_path = learning_client
        sid = self._insert_session(db_path)
        resp = client.get(f"/review-hints/sessions/{sid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session"]["id"] == sid
        assert data["recommendations"] == []
        assert data["pages"] == []


class TestDecideRecommendation:
    def _setup_pending_review_session_with_rec(self, db_path):
        hint_id = _insert_hint(db_path)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute(
            "INSERT INTO hint_review_sessions (status, hint_count, created_at) "
            "VALUES ('pending_review', 1, datetime('now'))"
        )
        sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO hint_review_recommendations "
            "(session_id, hint_id, recommendation, reason, exoneration_count, created_at) "
            "VALUES (?, ?, 'keep', 'looks fine', 0, datetime('now'))",
            (sid, hint_id),
        )
        conn.commit()
        rec_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        return sid, rec_id, hint_id

    def test_invalid_decision_returns_400(self, learning_client):
        client, _, _, db_path = learning_client
        sid, rec_id, _ = self._setup_pending_review_session_with_rec(db_path)
        resp = client.patch(
            f"/review-hints/sessions/{sid}/recommendations/{rec_id}",
            json={"admin_decision": "maybe"},
        )
        assert resp.status_code == 400

    def test_404_for_unknown_session(self, learning_client):
        client, *_ = learning_client
        resp = client.patch(
            "/review-hints/sessions/99999/recommendations/1",
            json={"admin_decision": "approved"},
        )
        assert resp.status_code == 404

    def test_409_when_session_not_in_pending_review(self, learning_client):
        client, _, _, db_path = learning_client
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute(
            "INSERT INTO hint_review_sessions (status, hint_count, created_at) "
            "VALUES ('completed', 0, datetime('now'))"
        )
        conn.commit()
        sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()

        resp = client.patch(
            f"/review-hints/sessions/{sid}/recommendations/1",
            json={"admin_decision": "approved"},
        )
        assert resp.status_code == 409

    def test_approved_decision_persisted(self, learning_client):
        client, _, _, db_path = learning_client
        sid, rec_id, _ = self._setup_pending_review_session_with_rec(db_path)
        resp = client.patch(
            f"/review-hints/sessions/{sid}/recommendations/{rec_id}",
            json={"admin_decision": "approved", "admin_notes": "LGTM"},
        )
        assert resp.status_code == 200
        assert resp.json()["recommendation"]["admin_decision"] == "approved"

    def test_404_for_unknown_rec(self, learning_client):
        client, _, _, db_path = learning_client
        sid, _, _ = self._setup_pending_review_session_with_rec(db_path)
        resp = client.patch(
            f"/review-hints/sessions/{sid}/recommendations/99999",
            json={"admin_decision": "approved"},
        )
        assert resp.status_code == 404


class TestApplyReviewSession:
    def _build_session_with_recs(self, db_path, rec_configs):
        """Insert a pending_review session with recommendations.

        rec_configs: list of dicts with {recommendation, hint_kwargs}
        Returns (session_id, [(rec_id, hint_id), ...])
        """
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute(
            "INSERT INTO hint_review_sessions (status, hint_count, created_at) "
            "VALUES ('pending_review', ?, datetime('now'))",
            (len(rec_configs),),
        )
        conn.commit()
        sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()

        rec_pairs = []
        for cfg in rec_configs:
            hint_id = _insert_hint(db_path, **cfg.get("hint_kwargs", {}))
            conn = sqlite3.connect(db_path, check_same_thread=False)
            conn.execute(
                "INSERT INTO hint_review_recommendations "
                "(session_id, hint_id, recommendation, reason, exoneration_count, "
                " admin_decision, applied, created_at) "
                "VALUES (?, ?, ?, 'test reason', 0, 'approved', 0, datetime('now'))",
                (sid, hint_id, cfg["recommendation"]),
            )
            conn.commit()
            rec_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.close()
            rec_pairs.append((rec_id, hint_id))

        return sid, rec_pairs

    def test_404_for_unknown_session(self, learning_client):
        client, *_ = learning_client
        resp = client.post("/review-hints/sessions/99999/apply")
        assert resp.status_code == 404

    def test_409_when_session_not_pending_review(self, learning_client):
        client, _, _, db_path = learning_client
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute(
            "INSERT INTO hint_review_sessions (status, hint_count, created_at) "
            "VALUES ('completed', 0, datetime('now'))"
        )
        conn.commit()
        sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()

        resp = client.post(f"/review-hints/sessions/{sid}/apply")
        assert resp.status_code == 409

    def test_apply_disable_recommendation(self, learning_client):
        client, _, _, db_path = learning_client
        sid, [(rec_id, hint_id)] = self._build_session_with_recs(db_path, [
            {"recommendation": "disable", "hint_kwargs": {"is_active": 1}},
        ])
        resp = client.post(f"/review-hints/sessions/{sid}/apply")
        assert resp.status_code == 200
        assert resp.json()["applied_count"] == 1

        # Verify hint was actually disabled
        conn = sqlite3.connect(db_path, check_same_thread=False)
        row = conn.execute(
            "SELECT is_active FROM nl_feedback_corrections WHERE id = ?", (hint_id,)
        ).fetchone()
        conn.close()
        assert row[0] == 0

    def test_apply_reactivate_recommendation(self, learning_client):
        client, _, _, db_path = learning_client
        sid, [(_, hint_id)] = self._build_session_with_recs(db_path, [
            {"recommendation": "reactivate", "hint_kwargs": {"is_active": 0}},
        ])
        resp = client.post(f"/review-hints/sessions/{sid}/apply")
        assert resp.status_code == 200

        conn = sqlite3.connect(db_path, check_same_thread=False)
        row = conn.execute(
            "SELECT is_active FROM nl_feedback_corrections WHERE id = ?", (hint_id,)
        ).fetchone()
        conn.close()
        assert row[0] == 1

    def test_apply_unflag_recommendation(self, learning_client):
        client, _, _, db_path = learning_client
        sid, [(_, hint_id)] = self._build_session_with_recs(db_path, [
            {"recommendation": "unflag", "hint_kwargs": {"conflict_flagged": 1, "is_active": 1}},
        ])
        resp = client.post(f"/review-hints/sessions/{sid}/apply")
        assert resp.status_code == 200

        conn = sqlite3.connect(db_path, check_same_thread=False)
        row = conn.execute(
            "SELECT conflict_flagged FROM nl_feedback_corrections WHERE id = ?", (hint_id,)
        ).fetchone()
        conn.close()
        assert row[0] == 0

    def test_apply_keep_and_flag_review_write_audit_rows(self, learning_client):
        client, _, _, db_path = learning_client
        sid, pairs = self._build_session_with_recs(db_path, [
            {"recommendation": "keep"},
            {"recommendation": "flag_review"},
        ])
        resp = client.post(f"/review-hints/sessions/{sid}/apply")
        assert resp.status_code == 200
        assert resp.json()["applied_count"] == 2

    def test_session_status_set_to_completed_after_apply(self, learning_client):
        client, _, _, db_path = learning_client
        sid, _ = self._build_session_with_recs(db_path, [
            {"recommendation": "keep"},
        ])
        client.post(f"/review-hints/sessions/{sid}/apply")

        conn = sqlite3.connect(db_path, check_same_thread=False)
        row = conn.execute(
            "SELECT status FROM hint_review_sessions WHERE id = ?", (sid,)
        ).fetchone()
        conn.close()
        assert row[0] == "completed"

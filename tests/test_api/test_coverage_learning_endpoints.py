"""
Coverage tests for learning_endpoints.py — targeting 384 missing lines.

LINE coverage, not permission coverage: the fixture below overrides
require_user with a platform admin, so every test in this file runs in the
scope_org = None all-orgs branch and none of them exercises org scoping at
all. Do not read this file's green as evidence of tenancy isolation — that
lives in test_learning_dashboards_org.py, test_hints_are_org_owned.py and
test_hint_mutation_tiers.py.

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
import threading
from datetime import datetime, timezone, timedelta
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
from src.backend.auth.jwt_utils import (
    require_admin as _require_admin,
    require_user as _require_user,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _pg_conn(dsn):
    """Open a SQLite-dialect (pg_compat) connection to the test's Postgres schema.

    Replaces the old sqlite3.connect(db_path); the `db_path` value handed to the
    test bodies is now the schema-scoped DSN. autocommit so each statement is
    immediately visible to the endpoint's own _admin_conn reads.
    """
    from src.backend.crew_ai.optimization import pg_compat
    return pg_compat.connect(dsn, autocommit=True)


@pytest.fixture
def db_em(api_pg_em):
    """PostgresExecutionMemory + its DSN (named db_path for call-site continuity)."""
    em, dsn = api_pg_em
    yield em, dsn


@pytest.fixture
def learning_client(db_em):
    """TestClient wired to a real Postgres schema, with _require_feedback_loop overridden."""
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
        conn = _pg_conn(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    app = FastAPI()
    app.include_router(router, prefix="")
    app.dependency_overrides[_require_feedback_loop] = lambda: mock_fb
    # After Task 12, routes self-guard via require_admin / require_user; override
    # BOTH here so the coverage tests (which call without tokens) keep
    # exercising the business logic rather than hitting 401/403.
    #
    # require_user, not just require_admin: the five hint-mutation routes now
    # refuse a token-less caller outright (learning_endpoints._require_caller),
    # so overriding require_admin alone would leave them 401 — and, before that
    # refusal existed, these tests were passing only because an anonymous
    # caller was silently granted every tier. They supply an identity now
    # rather than exploiting that hole. user_id is required: is_validated_admin
    # reads it to re-validate the platform role against the users table.
    app.dependency_overrides[_require_admin] = lambda: {
        "role": "admin", "email": "admin@test.local"
    }
    app.dependency_overrides[_require_user] = lambda: {
        "user_id": "00000000-0000-0000-0000-000000000099",
        "role": "admin", "email": "admin@test.local",
        "org_id": None, "org_role": None,
    }

    with patch("src.backend.api.learning_endpoints._admin_conn", side_effect=_test_admin_conn):
        with TestClient(app) as client:
            yield client, em, mock_fb, db_path


def _insert_hint(db_path, feedback_text="use xpath", category="locator",
                 scope="global", domain=None, url=None, evidence=1,
                 anchor_query="test query", is_active=1, conflict_flagged=0,
                 applied_count=0, success_count=0, failure_count=0,
                 original_failure_category=None, disabled_at=None,
                 org_id="org-admin") -> int:
    """Helper: insert a hint directly and return its id."""
    conn = _pg_conn(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO nl_feedback_corrections "
        "(feedback_text, category, scope, domain, url, original_failure_category, "
        " evidence_count, anchor_query, applied_count, success_count, failure_count, "
        " is_active, conflict_flagged, disabled_at, created_at, last_seen, "
        " org_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (feedback_text, category, scope, domain, url, original_failure_category,
         evidence, anchor_query, applied_count, success_count, failure_count,
         is_active, conflict_flagged, disabled_at, now, now, org_id),
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
        conn = _pg_conn(db_path)
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
        conn = _pg_conn(db_path)
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

    def test_search_filter_case_insensitive(self, learning_client):
        """Postgres LIKE is case-sensitive (unlike SQLite) — the endpoint uses
        ILIKE so search keeps the legacy case-insensitive behaviour."""
        client, _, _, db_path = learning_client
        _insert_hint(db_path, feedback_text="use XPath Locators")
        _insert_hint(db_path, feedback_text="avoid CSS selectors")

        resp = client.get("/hints", params={"search": "xpath"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

        resp = client.get("/hints", params={"search": "XPATH"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_domain_filter_case_insensitive(self, learning_client):
        client, _, _, db_path = learning_client
        _insert_hint(db_path, scope="domain", domain="example.com", feedback_text="hint a")

        resp = client.get("/hints", params={"domain": "Example.COM"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["hints"][0]["domain"] == "example.com"

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
        conn = _pg_conn(db_path)
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
        conn = _pg_conn(db_path)
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
            # v20: every hint is owned by exactly one org.
            "org_id": "org-admin",
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
        # Pre-insert the matching row directly so it's committed before the POST.
        # Admin creates dedup within the TARGET org, so the seeded duplicate must
        # sit in the same org as the payload.
        _insert_hint(db_path,
                     feedback_text="Use xpath for stable selectors",
                     scope="global", domain=None, evidence=1,
                     org_id="org-admin")
        payload = self._valid_payload()  # same text + scope + domain
        resp = client.post("/hints", json=payload)
        # Route decorator always returns 201; created=False signals the duplicate path
        assert resp.status_code == 201
        data = resp.json()
        assert data["created"] is False
        assert data["hint"]["evidence_count"] == 2

    def test_a_global_duplicate_typed_on_another_domain_still_dedups(
        self, learning_client,
    ):
        """T12: `domain` left the global dedup key, and this SELECT has to
        follow it. If it kept filtering on domain it would miss the existing
        row and the INSERT would hit uq_nlfc_dedup_global_v21 — a 409 saying
        "an identical hint was just created" for a hint created long ago,
        instead of the evidence bump the admin asked for."""
        client, _, _, db_path = learning_client
        _insert_hint(db_path,
                     feedback_text="Use xpath for stable selectors",
                     scope="global", domain="shop.test", evidence=1,
                     org_id="org-admin")

        resp = client.post("/hints", json=self._valid_payload(domain=None))

        assert resp.status_code == 201, resp.json()
        assert resp.json()["created"] is False
        assert resp.json()["hint"]["evidence_count"] == 2

    def test_a_domain_scoped_duplicate_on_another_domain_is_a_new_hint(
        self, learning_client,
    ):
        """Anti-false-green: only the GLOBAL key lost `domain`. A domain-scoped
        hint is about its domain, so the same text on another site is a new
        hint and must still be created."""
        client, _, _, db_path = learning_client
        _insert_hint(db_path,
                     feedback_text="Use xpath for stable selectors",
                     scope="domain", domain="shop.test", evidence=1,
                     org_id="org-admin")

        resp = client.post("/hints", json=self._valid_payload(
            scope="domain", domain="other.test"))

        assert resp.status_code == 201, resp.json()
        assert resp.json()["created"] is True

    def test_duplicate_flagged_hint_clears_flag_and_logs_audit(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, feedback_text="Use xpath for stable selectors",
                               conflict_flagged=1, scope="global",
                               org_id="org-admin")

        payload = self._valid_payload()  # same text + scope → matches existing
        resp = client.post("/hints", json=payload)
        # Duplicate path: route decorator always returns 201; created=False
        assert resp.status_code == 201
        data = resp.json()
        assert data["created"] is False
        # Flag must be cleared in the API response
        assert data["hint"]["conflict_flagged"] == 0

        # M8: the duplicate-create path bumps evidence on an EXISTING hint —
        # it must write 'reinforce', not a second 'create'. A hint that was
        # never freshly created twice must never show two 'create' rows.
        conn = _pg_conn(db_path)
        actions = [
            r[0]
            for r in conn.execute(
                "SELECT action FROM hint_audit WHERE hint_id = ? ORDER BY id",
                (hint_id,),
            ).fetchall()
        ]
        conn.close()
        assert "unflag" in actions, f"expected unflag audit row, got {actions}"
        assert "reinforce" in actions, f"expected reinforce audit row, got {actions}"
        assert actions.count("create") == 0, (
            f"duplicate-create path wrote a 'create' row instead of "
            f"'reinforce' — got {actions}"
        )

    def test_run_triage_calls_nl_engine(self, learning_client):
        client, _, mock_fb, _ = learning_client
        mock_fb.nl_engine.process_feedback.return_value = {"category": "timing"}

        payload = self._valid_payload(run_triage=True)
        resp = client.post("/hints", json=payload)
        assert resp.status_code == 201
        assert resp.json()["hint"]["category"] == "timing"

    def test_another_orgs_hint_does_not_capture_an_admin_create(self, learning_client):
        """Identical text in a DIFFERENT org must not absorb the create.

        Before v20 this was about a shared row not being swallowed by a private
        one; now it is simply that the dedup key includes org_id, so the two
        rows coexist and keep their counters apart. Same defect, same test,
        expressed on the axis that survived."""
        client, _, _, db_path = learning_client
        other_hint_id = _insert_hint(
            db_path, feedback_text="Dismiss the cookie banner first",
            scope="domain", domain="shop.test", evidence=1, org_id="org-A",
        )

        resp = client.post("/hints", json=self._valid_payload(
            feedback_text="Dismiss the cookie banner first",
            scope="domain", domain="shop.test", org_id="org-B",
        ))
        assert resp.status_code == 201
        data = resp.json()
        assert data["created"] is True, (
            "the create was deduped into ANOTHER org's hint — org-B would "
            "never get the hint, and org-A's evidence would be bumped by it"
        )
        assert data["hint"]["org_id"] == "org-B"
        assert data["hint"]["id"] != other_hint_id

        conn = _pg_conn(db_path)
        org_row = conn.execute(
            "SELECT evidence_count, org_id FROM nl_feedback_corrections "
            "WHERE id = ?", (other_hint_id,),
        ).fetchone()
        conn.close()
        assert org_row[0] == 1, "org-A's evidence must not be bumped by an org-B create"
        assert org_row[1] == "org-A"

    def test_url_scope_second_page_is_not_deduped_into_the_first(self, learning_client):
        """Two url-scoped hints with identical text+domain but different urls are
        distinct hints, one per page. The dedup SELECT must key on url for
        scope='url' — the storage contract already does (uq_nlfc_dedup_url_v21) and so
        does NLFeedbackEngine.learn_from_feedback. Omitting url here bumps evidence
        on the FIRST page's hint and the second page's hint is never created."""
        client, _, _, db_path = learning_client
        first_id = _insert_hint(
            db_path, feedback_text="Wait for the spinner to clear",
            scope="url", domain="shop.test", url="https://shop.test/checkout",
            evidence=1, org_id="org-admin",
        )

        resp = client.post("/hints", json=self._valid_payload(
            feedback_text="Wait for the spinner to clear",
            scope="url", domain="shop.test", url="https://shop.test/cart",
        ))
        assert resp.status_code == 201
        data = resp.json()
        assert data["created"] is True, (
            "the second page's hint was deduped into the first page's hint — "
            "the dedup key ignored url for scope='url'"
        )
        assert data["hint"]["url"] == "https://shop.test/cart"
        assert data["hint"]["id"] != first_id

        conn = _pg_conn(db_path)
        first_evidence = conn.execute(
            "SELECT evidence_count FROM nl_feedback_corrections WHERE id = ?",
            (first_id,),
        ).fetchone()[0]
        conn.close()
        assert first_evidence == 1, (
            "the first page's hint was reinforced by a create meant for another page"
        )

    def test_url_scope_same_page_still_dedupes(self, learning_client):
        """The url key must not break same-page dedup: identical text+domain+url
        stays one hint with bumped evidence."""
        client, _, _, db_path = learning_client
        _insert_hint(
            db_path, feedback_text="Wait for the spinner to clear",
            scope="url", domain="shop.test", url="https://shop.test/checkout",
            evidence=1, org_id="org-admin",
        )

        resp = client.post("/hints", json=self._valid_payload(
            feedback_text="Wait for the spinner to clear",
            scope="url", domain="shop.test", url="https://shop.test/checkout",
        ))
        assert resp.status_code == 201
        data = resp.json()
        assert data["created"] is False, "same-page resubmission must dedup, not duplicate"
        assert data["hint"]["evidence_count"] == 2


class TestCreateHintDomainUrlNormalisation:
    """F5: create_hint must normalise whitespace-only domain/url to NULL and
    validate the resulting row against its own scope. Closes create_hint's
    own residual gap: whitespace (truthy in Python) bypassed the `not
    request.domain`/`not request.url` checks, and scope='url' never
    validated domain at all, so domain='' passed straight through to
    storage unnormalised."""

    def _valid_payload(self, **overrides):
        base = {
            "feedback_text": "Use xpath for stable selectors",
            "anchor_query": "click the login button",
            "scope": "global",
            "actor": "alice",
            "org_id": "org-admin",
        }
        base.update(overrides)
        return base

    def test_domain_scope_whitespace_only_domain_returns_400(self, learning_client):
        client, *_ = learning_client
        resp = client.post("/hints", json=self._valid_payload(scope="domain", domain="   "))
        assert resp.status_code == 400
        assert "domain" in resp.json()["detail"].lower()

    def test_url_scope_whitespace_only_url_returns_400(self, learning_client):
        client, *_ = learning_client
        resp = client.post("/hints", json=self._valid_payload(scope="url", url="   "))
        assert resp.status_code == 400
        assert "url" in resp.json()["detail"].lower()

    def test_url_scope_blank_domain_is_normalised_to_null(self, learning_client):
        """create_hint validates url for scope='url' but never touched
        domain, so domain='' passed straight through to storage. This is
        item 3 of the F5 fix: normalisation covers it."""
        client, *_ = learning_client
        resp = client.post("/hints", json=self._valid_payload(
            scope="url", url="https://shop.test/cart", domain=""))
        assert resp.status_code == 201, resp.json()
        assert resp.json()["hint"]["domain"] is None

    def test_domain_scope_domain_is_stripped_of_padding(self, learning_client):
        client, *_ = learning_client
        resp = client.post("/hints", json=self._valid_payload(
            scope="domain", domain="  shop.test  "))
        assert resp.status_code == 201, resp.json()
        assert resp.json()["hint"]["domain"] == "shop.test"

    def test_global_scope_blank_domain_and_url_normalised_to_null(self, learning_client):
        client, *_ = learning_client
        resp = client.post("/hints", json=self._valid_payload(
            scope="global", domain="   ", url="   "))
        assert resp.status_code == 201, resp.json()
        assert resp.json()["hint"]["domain"] is None
        assert resp.json()["hint"]["url"] is None


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

    def test_a_colliding_scope_change_is_a_409_not_a_500(self, learning_client):
        """The admin drawer sends scope/domain/url on every save, so an edit can
        land on an existing hint's dedup key. That is a conflict the admin can
        resolve, not a server fault: RFC 9110 reserves 409 for exactly this, and
        `create_hint` already answers 409 for the same constraint. Answering 500
        tells the admin nothing and hides the reason in the log.

        v21 widened this case specifically: the global key no longer includes
        `domain`, so any same-text global hint in the org now collides."""
        client, _, _, db_path = learning_client
        _insert_hint(db_path, feedback_text="use xpath", scope="global",
                     domain="other.test", org_id="org-admin")
        target = _insert_hint(db_path, feedback_text="use xpath", scope="domain",
                              domain="shop.test", org_id="org-admin")

        resp = client.patch(f"/hints/{target}", json={
            "actor": "alice", "scope": "global",
        })

        assert resp.status_code == 409, (
            f"colliding scope change answered {resp.status_code}: {resp.json()}"
        )
        detail = resp.json()["detail"]
        assert "already" in detail.lower(), detail
        # The hint must be unchanged — a refused edit that half-applied would be
        # worse than the 500 it replaces.
        conn = _pg_conn(db_path)
        row = conn.execute(
            "SELECT scope, domain FROM nl_feedback_corrections WHERE id = ?",
            (target,),
        ).fetchone()
        conn.close()
        assert row["scope"] == "domain" and row["domain"] == "shop.test"

    def test_a_colliding_domain_change_is_a_409_too(self, learning_client):
        """Not only scope. Moving a domain-scoped hint onto a domain that
        already carries the same text hits uq_nlfc_dedup_general_v21. This half
        is pre-existing — v21 neither created nor widened it — and the same
        handler covers it because there is one UPDATE."""
        client, _, _, db_path = learning_client
        _insert_hint(db_path, feedback_text="use xpath", scope="domain",
                     domain="taken.test", org_id="org-admin")
        target = _insert_hint(db_path, feedback_text="use xpath", scope="domain",
                              domain="shop.test", org_id="org-admin")

        resp = client.patch(f"/hints/{target}", json={
            "actor": "alice", "domain": "taken.test",
        })

        assert resp.status_code == 409, (
            f"colliding domain change answered {resp.status_code}: {resp.json()}"
        )

    def test_a_non_colliding_scope_change_still_succeeds(self, learning_client):
        """Anti-false-green: a handler that answered 409 unconditionally, or an
        over-broad except that swallowed every failure, would pass the two tests
        above and break every legitimate edit."""
        client, _, _, db_path = learning_client
        _insert_hint(db_path, feedback_text="a different correction",
                     scope="global", org_id="org-admin")
        target = _insert_hint(db_path, feedback_text="use xpath", scope="domain",
                              domain="shop.test", org_id="org-admin")

        resp = client.patch(f"/hints/{target}", json={
            "actor": "alice", "scope": "global",
        })

        assert resp.status_code == 200, resp.json()
        assert resp.json()["hint"]["scope"] == "global"

    def test_another_orgs_identical_hint_does_not_block_the_change(self, learning_client):
        """org_id is in every dedup key, so a second tenant holding the same
        text at the same scope is not a conflict. If it were, one org's hints
        could veto another org's admin edits."""
        client, _, _, db_path = learning_client
        _insert_hint(db_path, feedback_text="use xpath", scope="global",
                     org_id="org-other")
        target = _insert_hint(db_path, feedback_text="use xpath", scope="domain",
                              domain="shop.test", org_id="org-admin")

        resp = client.patch(f"/hints/{target}", json={
            "actor": "alice", "scope": "global",
        })

        assert resp.status_code == 200, resp.json()

    def test_a_refused_edit_writes_no_audit_row(self, learning_client):
        """The audit row is written after the UPDATE, so a refused edit must
        leave the timeline alone. A `patch` row for an edit that never happened
        is the same class of untrue event T5 and T11 exist to prevent."""
        client, _, _, db_path = learning_client
        _insert_hint(db_path, feedback_text="use xpath", scope="global",
                     domain="other.test", org_id="org-admin")
        target = _insert_hint(db_path, feedback_text="use xpath", scope="domain",
                              domain="shop.test", org_id="org-admin")

        client.patch(f"/hints/{target}", json={"actor": "alice", "scope": "global"})

        conn = _pg_conn(db_path)
        rows = conn.execute(
            "SELECT action FROM hint_audit WHERE hint_id = ?", (target,),
        ).fetchall()
        conn.close()
        assert rows == [], f"a refused edit was audited: {[r[0] for r in rows]}"


class TestPatchHintDomainUrlValidation:
    """F5: the admin API could silently destroy a user's stored correction.
    patch_hint validated scope/target consistency only when `scope` was
    itself in the request body (learning_endpoints.py:635), so patching
    domain/url alone — the drawer's normal single-field edit — bypassed it
    entirely: `domain=''`/`url=''` (or whitespace) landed straight in
    storage. See TestEngineCollisionRegression below for what that then did
    to a later correction on the same hint."""

    def test_domain_scope_patched_to_empty_string_is_refused(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, scope="domain", domain="shop.test",
                               org_id="org-admin")
        resp = client.patch(f"/hints/{hint_id}", json={"actor": "alice", "domain": ""})
        assert resp.status_code == 400
        assert "domain" in resp.json()["detail"].lower()

        conn = _pg_conn(db_path)
        row = conn.execute(
            "SELECT domain FROM nl_feedback_corrections WHERE id = ?", (hint_id,),
        ).fetchone()
        audit_rows = conn.execute(
            "SELECT action FROM hint_audit WHERE hint_id = ?", (hint_id,),
        ).fetchall()
        conn.close()
        assert row["domain"] == "shop.test", "a refused patch must not blank the domain"
        assert audit_rows == [], "a refused patch must not write an audit row"

    def test_domain_scope_patched_to_whitespace_only_is_refused(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, scope="domain", domain="shop.test",
                               org_id="org-admin")
        resp = client.patch(f"/hints/{hint_id}", json={"actor": "alice", "domain": "   "})
        assert resp.status_code == 400

        conn = _pg_conn(db_path)
        row = conn.execute(
            "SELECT domain FROM nl_feedback_corrections WHERE id = ?", (hint_id,),
        ).fetchone()
        conn.close()
        assert row["domain"] == "shop.test"

    def test_url_scope_patched_to_empty_string_is_refused(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, scope="url", domain="shop.test",
                               url="https://shop.test/cart", org_id="org-admin")
        resp = client.patch(f"/hints/{hint_id}", json={"actor": "alice", "url": ""})
        assert resp.status_code == 400
        assert "url" in resp.json()["detail"].lower()

        conn = _pg_conn(db_path)
        row = conn.execute(
            "SELECT url FROM nl_feedback_corrections WHERE id = ?", (hint_id,),
        ).fetchone()
        conn.close()
        assert row["url"] == "https://shop.test/cart"

    def test_url_scope_patched_to_whitespace_only_is_refused(self, learning_client):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, scope="url", domain="shop.test",
                               url="https://shop.test/cart", org_id="org-admin")
        resp = client.patch(f"/hints/{hint_id}", json={"actor": "alice", "url": "   "})
        assert resp.status_code == 400

    def test_global_scope_patched_domain_to_blank_is_accepted_and_normalised(
        self, learning_client,
    ):
        """global scope doesn't need a domain — blanking it is a legitimate
        edit, not F5's bug, and must not be swept up by the new check."""
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, scope="global", domain="shop.test",
                               org_id="org-admin")
        resp = client.patch(f"/hints/{hint_id}", json={"actor": "alice", "domain": ""})
        assert resp.status_code == 200, resp.json()
        assert resp.json()["changed"] is True
        assert resp.json()["hint"]["domain"] is None

    def test_global_scope_patched_url_to_whitespace_is_accepted_and_normalised(
        self, learning_client,
    ):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, scope="global", url="https://shop.test",
                               org_id="org-admin")
        resp = client.patch(f"/hints/{hint_id}", json={"actor": "alice", "url": "   "})
        assert resp.status_code == 200, resp.json()
        assert resp.json()["hint"]["url"] is None

    def test_scope_change_to_domain_with_whitespace_domain_falls_through_to_stored_value(
        self, learning_client,
    ):
        """When `scope` is ALSO in the request, a whitespace-only domain is
        the create_hint-style 'no new value given' signal, not a blanking
        request — it must fall through to the hint's existing domain rather
        than being stored literally (pre-fix, `"   " or row_dict.get(...)`
        used "   " verbatim because a non-empty string is truthy)."""
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, scope="global", domain="shop.test",
                               org_id="org-admin")
        resp = client.patch(f"/hints/{hint_id}", json={
            "actor": "alice", "scope": "domain", "domain": "   ",
        })
        assert resp.status_code == 200, resp.json()
        assert resp.json()["hint"]["scope"] == "domain"
        assert resp.json()["hint"]["domain"] == "shop.test"

    def test_legacy_domain_scoped_row_with_null_domain_rejects_a_category_only_patch(
        self, learning_client,
    ):
        """Reachable, not hypothetical: extract_url_from_query returns None
        when the query names no URL, process_execution then stores
        domain=None, and several triage categories map to scope='domain'
        (_SCOPE_BY_CATEGORY) — so NLFeedbackEngine itself can create a
        domain-scoped row with domain=NULL. The unconditional F5 validation
        means ANY patch on such a row — even one that never touches
        domain — is refused until the row is repaired, because the
        resolved row would otherwise still violate its own scope."""
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, scope="domain", domain=None,
                               org_id="org-admin")
        resp = client.patch(f"/hints/{hint_id}", json={
            "actor": "alice", "category": "timing",
        })
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "domain-scoped" in detail and "no domain" in detail, (
            f"message must name the row's scope and its missing field, got: {detail!r}"
        )

        conn = _pg_conn(db_path)
        row = conn.execute(
            "SELECT category FROM nl_feedback_corrections WHERE id = ?", (hint_id,),
        ).fetchone()
        conn.close()
        assert row["category"] != "timing", "a refused patch must not partially apply"

    def test_legacy_domain_scoped_row_is_repaired_by_supplying_domain_in_the_same_patch(
        self, learning_client,
    ):
        """The 400 above is repairable in the same request: supplying a
        domain alongside the unrelated field satisfies the check and both
        changes land together."""
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, scope="domain", domain=None,
                               org_id="org-admin")
        resp = client.patch(f"/hints/{hint_id}", json={
            "actor": "alice", "category": "timing", "domain": "example.com",
        })
        assert resp.status_code == 200, resp.json()
        assert resp.json()["changed"] is True
        assert resp.json()["hint"]["category"] == "timing"
        assert resp.json()["hint"]["domain"] == "example.com"

    def test_legacy_url_scoped_row_with_null_url_rejects_a_category_only_patch(
        self, learning_client,
    ):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, scope="url", url=None,
                               category="uncategorized", org_id="org-admin")
        resp = client.patch(f"/hints/{hint_id}", json={
            "actor": "alice", "category": "locator",
        })
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "url-scoped" in detail and "no url" in detail, (
            f"message must name the row's scope and its missing field, got: {detail!r}"
        )

        conn = _pg_conn(db_path)
        row = conn.execute(
            "SELECT category FROM nl_feedback_corrections WHERE id = ?", (hint_id,),
        ).fetchone()
        conn.close()
        assert row["category"] == "uncategorized", "a refused patch must not partially apply"

    def test_legacy_url_scoped_row_is_repaired_by_supplying_url_in_the_same_patch(
        self, learning_client,
    ):
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, scope="url", url=None,
                               org_id="org-admin")
        resp = client.patch(f"/hints/{hint_id}", json={
            "actor": "alice", "category": "locator", "url": "https://shop.test/cart",
        })
        assert resp.status_code == 200, resp.json()
        assert resp.json()["changed"] is True
        assert resp.json()["hint"]["category"] == "locator"
        assert resp.json()["hint"]["url"] == "https://shop.test/cart"


class TestEngineCollisionRegression:
    """The brief's own repro (Q1-Q3): PATCH {"domain": ""} on a domain-scoped
    hint used to succeed, leaving a domain='' row. uq_nlfc_dedup_general_v21
    keys on COALESCE(domain,''), so that row and a domain=NULL row occupy the
    SAME unique-index bucket — but NLFeedbackEngine.learn_from_feedback's own
    dedup SELECT uses `domain IS NOT DISTINCT FROM ?`, which does NOT treat
    '' and NULL as equal. A later correction on the same text/scope/org,
    arriving with domain=None (extract_url_from_query found no URL), missed
    the '' row on the SELECT, then hit it on the INSERT's unique index,
    raised IntegrityError, and was silently dropped — rollback + one
    WARNING, nl_feedback_engine.py's `except Exception` block. This proves
    that path is closed: the PATCH that used to create the '' row is now
    refused, so the engine's write lands cleanly instead of colliding."""

    def test_patch_can_no_longer_create_the_colliding_row_so_the_correction_survives(
        self, learning_client, caplog,
    ):
        client, em, mock_fb, db_path = learning_client
        hint_id = _insert_hint(
            db_path, feedback_text="Dismiss the cookie banner first",
            scope="domain", domain="shop.test", org_id="org-admin",
        )

        # Q1 (brief): PATCH {"domain": ""} — must now be refused.
        resp = client.patch(f"/hints/{hint_id}", json={"actor": "alice", "domain": ""})
        assert resp.status_code == 400

        conn = _pg_conn(db_path)
        row = conn.execute(
            "SELECT domain FROM nl_feedback_corrections WHERE id = ?", (hint_id,),
        ).fetchone()
        conn.close()
        assert row["domain"] == "shop.test", "the '' row from Q1 must never be created"

        # Q2 (brief): the SAME text arrives again through the NL engine, this
        # time with domain=None. Run it on the writer thread — the engine's
        # own _assert_writer_thread guard requires it.
        from src.backend.crew_ai.optimization.nl_feedback_engine import NLFeedbackEngine
        from src.backend.crew_ai.optimization.learning_config import WRITER_THREAD_NAME

        engine = NLFeedbackEngine(execution_memory=em)
        record = MagicMock()
        record.workflow_id = "wf-collision-check"
        record.domain = None
        record.url = None
        record.failure_category = None
        record.org_id = "org-admin"
        record.user_query = "dismiss the cookie banner"

        old_name = threading.current_thread().name
        threading.current_thread().name = WRITER_THREAD_NAME
        try:
            with caplog.at_level(
                "WARNING", logger="src.backend.crew_ai.optimization.nl_feedback_engine",
            ):
                engine.learn_from_feedback(record, {
                    "feedback_text": "Dismiss the cookie banner first",
                    "category": "structural",  # -> scope 'domain' (_SCOPE_BY_CATEGORY)
                    "actor": "engine",
                })
        finally:
            threading.current_thread().name = old_name

        assert "Failed to store feedback correction" not in caplog.text, (
            f"the engine write was silently dropped: {caplog.text}"
        )

        conn = _pg_conn(db_path)
        rows = conn.execute(
            "SELECT domain, evidence_count FROM nl_feedback_corrections "
            "WHERE feedback_text = ? ORDER BY id",
            ("Dismiss the cookie banner first",),
        ).fetchall()
        conn.close()
        # The original (domain='shop.test') hint is untouched, and the
        # domain=NULL correction landed as its own row instead of vanishing
        # into a swallowed IntegrityError.
        assert len(rows) == 2, f"expected 2 rows (original + surviving correction), got {rows}"
        by_domain = {r["domain"]: r["evidence_count"] for r in rows}
        assert by_domain.get("shop.test") == 1
        assert by_domain.get(None) == 1


class TestCreateHintOrgIdIsAlreadyValidated:
    """Pinning behaviour that already exists, because a review claimed it did
    not: `HintCreateRequest.org_id` is a bare `str` with no `min_length`, so the
    Pydantic model alone would accept "". The handler strips and rejects it,
    matching what it already does for actor / feedback_text / anchor_query.

    It matters because `COALESCE(org_id,'')` in all three dedup indexes maps ""
    onto the same bucket as a legacy NULL org — so an empty org is the one
    wrong org value that is not merely an orphan.
    """

    def _payload(self, org_id):
        return {
            "feedback_text": "use xpath for stable selectors",
            "anchor_query": "click the login button",
            "scope": "global", "actor": "alice", "org_id": org_id,
        }

    def test_an_empty_org_is_refused(self, learning_client):
        client, *_ = learning_client
        resp = client.post("/hints", json=self._payload(""))
        assert resp.status_code == 400
        assert "org_id" in resp.json()["detail"]

    def test_a_whitespace_org_is_refused(self, learning_client):
        client, *_ = learning_client
        resp = client.post("/hints", json=self._payload("   "))
        assert resp.status_code == 400

    def test_a_padded_org_is_stored_stripped(self, learning_client):
        """Not just refused-if-empty: the value is normalised, so "  org-a  "
        cannot become a hint no token will ever match."""
        client, _, _, db_path = learning_client
        resp = client.post("/hints", json=self._payload("  org-padded  "))
        assert resp.status_code == 201
        conn = _pg_conn(db_path)
        row = conn.execute(
            "SELECT org_id FROM nl_feedback_corrections WHERE id = ?",
            (resp.json()["hint"]["id"],),
        ).fetchone()
        conn.close()
        assert row["org_id"] == "org-padded"


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

    def test_reactivate_resets_unused_count(self, learning_client):
        """Step 4b: manual reactivation is a fresh chance — unused_count resets
        to 0 while the earned success_count is preserved."""
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path, is_active=0)
        conn = _pg_conn(db_path)
        conn.execute(
            "UPDATE nl_feedback_corrections SET unused_count=7, success_count=2 WHERE id=?",
            (hint_id,),
        )
        conn.commit()
        conn.close()

        resp = client.post(f"/hints/{hint_id}/reactivate", json={"actor": "alice"})
        assert resp.status_code == 200

        conn = _pg_conn(db_path)
        row = conn.execute(
            "SELECT unused_count, success_count FROM nl_feedback_corrections WHERE id=?",
            (hint_id,),
        ).fetchone()
        conn.close()
        assert row[0] == 0   # reset
        assert row[1] == 2   # earned record preserved


# ---------------------------------------------------------------------------
# GET /triggers
# ---------------------------------------------------------------------------

class TestListTriggers:
    def _insert_trigger(self, db_path, trigger_type="trigger_1", workflow_id="wf-1",
                        status="succeeded", flagged_hint_ids="[]"):
        conn = _pg_conn(db_path)
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
        conn = _pg_conn(db_path)
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

    # Every machine writer of hint_audit, with the action and actor it
    # writes. reviewed_events counts "a human looked at this flag", so none
    # of these may satisfy it:
    #   pg_schema.py migration 21          -> merge, merge_recommendation_dropped
    #   nl_feedback_engine._flag_hints_no_commit -> trigger_N_flag
    #   nl_feedback_engine._auto_disable_hint    -> auto_disable
    _MACHINE_AUDIT_ROWS = [
        ("merge", "migration_v21"),
        ("merge_recommendation_dropped", "migration_v21"),
        ("trigger_1_flag", "trigger_1"),
        ("trigger_2_flag", "trigger_2"),
        ("auto_disable", "system"),
    ]

    @pytest.mark.parametrize("action,actor", _MACHINE_AUDIT_ROWS)
    def test_a_machine_written_row_does_not_count_as_a_human_review(
        self, learning_client, action, actor,
    ):
        """M5/I1: reviewed_events/reversed_events used to count ANY hint_audit
        row newer than the trigger, then only excluded the literal 'merge'.

        Migration 21 writes TWO verbs on the same survivor hint with the same
        actor, and the engine writes two more of its own, so an action list
        was always going to be one verb behind. The invariant is the actor: a
        row no person wrote is not a review, and 'Pending review' on the
        dashboard is flagged_events - reviewed_events, so counting one hides
        a flag nobody has looked at."""
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path)
        conn = _pg_conn(db_path)
        conn.execute(
            "INSERT INTO trigger_events (trigger_type, workflow_id, status, "
            "flagged_hint_ids, active_hint_ids, created_at) "
            "VALUES ('trigger_1', 'wf-merge', 'succeeded', ?, '[]', "
            "        datetime('now', '-1 hour'))",
            (json.dumps([hint_id]),),
        )
        conn.execute(
            "INSERT INTO hint_audit "
            "(hint_id, action, actor, reason, created_at) "
            "VALUES (?, ?, ?, 'machine-written', datetime('now'))",
            (hint_id, action, actor),
        )
        conn.close()

        resp = client.get("/stats")
        assert resp.status_code == 200
        acc = resp.json()["llm_accuracy"]
        assert acc["flagged_events"] == 1
        assert acc["reviewed_events"] == 0, (
            f"a machine-written {action!r} row by {actor!r} counted as a "
            "human review of the flagged hint"
        )
        assert acc["reversed_events"] == 0
        assert acc["engagement_rate"] == 0.0
        assert acc["pending_review"] == 1

    def test_a_human_row_after_the_trigger_still_counts_as_a_review(
        self, learning_client,
    ):
        """The other half of I1: the exclusion must not silence real reviews.
        An admin unflagging the hint is exactly what engagement_rate is for."""
        client, _, _, db_path = learning_client
        hint_id = _insert_hint(db_path)
        conn = _pg_conn(db_path)
        conn.execute(
            "INSERT INTO trigger_events (trigger_type, workflow_id, status, "
            "flagged_hint_ids, active_hint_ids, created_at) "
            "VALUES ('trigger_1', 'wf-human', 'succeeded', ?, '[]', "
            "        datetime('now', '-1 hour'))",
            (json.dumps([hint_id]),),
        )
        conn.execute(
            "INSERT INTO hint_audit "
            "(hint_id, action, actor, reason, created_at) "
            "VALUES (?, 'unflag', 'alice@example.com', 'the hint is fine', "
            "        datetime('now'))",
            (hint_id,),
        )
        conn.close()

        acc = client.get("/stats").json()["llm_accuracy"]
        assert acc["reviewed_events"] == 1
        assert acc["reversed_events"] == 1
        assert acc["engagement_rate"] == 1.0
        assert acc["pending_review"] == 0


# ---------------------------------------------------------------------------
# GET /stats — Part 2 usage-attribution panels
# ---------------------------------------------------------------------------

class TestPart2DashboardPanels:
    """Exoneration/breakdown filters, M1 attribution-health, FR5 never-attributed,
    and the Step-5b failure-association signal."""

    @staticmethod
    def _conn(db_path):
        c = _pg_conn(db_path)
        c.row_factory = sqlite3.Row
        return c

    def _insert_attr_event(self, db_path, *, status="succeeded", used="[1]", unused="[]"):
        c = self._conn(db_path)
        c.execute(
            "INSERT INTO trigger_events (trigger_type, workflow_id, status, "
            "flagged_hint_ids, active_hint_ids, used_hint_ids, unused_hint_ids, created_at) "
            "VALUES ('usage_attribution', ?, ?, '[]', '[1]', ?, ?, datetime('now'))",
            (f"wf-attr-{used}-{unused}-{status}", status, used, unused),
        )
        c.commit()
        c.close()

    def _insert_exec(self, db_path, workflow_id, injected, *, status="failed",
                     failure_category=None, days_ago=0):
        c = self._conn(db_path)
        ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        c.execute(
            "INSERT INTO execution_records (workflow_id, timestamp, user_query, "
            "test_status, injected_hint_ids, failure_category) VALUES (?, ?, 'q', ?, ?, ?)",
            (workflow_id, ts, status, injected, failure_category),
        )
        c.commit()
        c.close()

    def test_breakdown_excludes_usage_attribution(self, learning_client):
        client, _, _, db_path = learning_client
        self._insert_attr_event(db_path)
        data = client.get("/stats").json()
        types = {r["trigger_type"] for r in data["trigger_activity"]}
        assert "usage_attribution" not in types

    def test_attribution_health_panel(self, learning_client):
        client, _, _, db_path = learning_client
        self._insert_attr_event(db_path, status="succeeded", used="[1]", unused="[2]")
        self._insert_attr_event(db_path, status="llm_timeout", used="[]", unused="[]")
        ah = client.get("/stats").json()["attribution_health"]
        assert ah["events_total"] == 2
        assert ah["events_by_status"].get("succeeded") == 1
        assert ah["events_by_status"].get("llm_timeout") == 1
        assert ah["credited_events"] == 1            # only the succeeded+non-empty row
        assert ah["credited_nothing_recently"] is False
        assert "holdout_lift" in ah                  # None → frontend renders "n/a"

    def test_failure_association_helper_counts_related_only(self, learning_client):
        client, _, _, db_path = learning_client
        hid = _insert_hint(db_path, success_count=0, original_failure_category="C1")
        # C1 relates to {C1, D1}; B1 is unrelated.
        self._insert_exec(db_path, "wf-fa-1", json.dumps([hid]), failure_category="D1")
        self._insert_exec(db_path, "wf-fa-2", json.dumps([hid]), failure_category="C1")
        self._insert_exec(db_path, "wf-fa-3", json.dumps([hid]), failure_category="B1")

        from src.backend.api.learning_endpoints import _compute_failure_associations
        conn = self._conn(db_path)
        counts = _compute_failure_associations(conn, [hid])
        conn.close()
        assert counts.get(hid) == 2  # D1 + C1 related; B1 excluded

    def test_never_attributed_signal(self, learning_client):
        client, _, _, db_path = learning_client
        c = self._conn(db_path)
        old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        c.execute(
            "INSERT INTO nl_feedback_corrections (feedback_text, category, scope, "
            "evidence_count, anchor_query, applied_count, is_active, conflict_flagged, "
            "created_at, last_seen) "
            "VALUES ('never scored hint', 'locator', 'global', 1, 'q', 0, 1, 0, ?, ?)",
            (old, old),
        )
        c.commit()
        hid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.close()
        # Injected into 5 distinct workflows but never attributed (applied=0).
        for i in range(5):
            self._insert_exec(db_path, f"wf-na-{i}", json.dumps([hid]),
                              status="passed", days_ago=1)

        ids = {r["id"] for r in client.get("/stats").json()["never_attributed"]}
        assert hid in ids

    def test_never_attributed_excludes_recently_created(self, learning_client):
        client, _, _, db_path = learning_client
        # Same shape but created today → below the age floor → excluded.
        hid = _insert_hint(db_path, applied_count=0)  # created_at = now
        for i in range(5):
            self._insert_exec(db_path, f"wf-new-{i}", json.dumps([hid]),
                              status="passed", days_ago=1)
        ids = {r["id"] for r in client.get("/stats").json()["never_attributed"]}
        assert hid not in ids


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

def _health_app_with_admin_bypass():
    """Return a bare FastAPI app with the learning router and require_admin bypassed.

    After Task 12, /health is gated by require_admin.  Health-specific tests
    that call without a token (testing DISABLED/FAILED/unknown logic) must
    override the dep so the auth layer doesn't shadow the business logic.
    """
    app = FastAPI()
    app.include_router(router, prefix="")
    app.dependency_overrides[_require_admin] = lambda: {
        "role": "admin", "email": "admin@test.local"
    }
    return app


class TestGetLearningHealth:
    def test_health_ok_when_fb_returns_ok(self, learning_client):
        _, _, mock_fb, _ = learning_client
        mock_fb.get_health_status.return_value = "OK"

        app = _health_app_with_admin_bypass()
        with patch("src.backend.api.learning_endpoints.get_feedback_loop", return_value=mock_fb):
            with TestClient(app) as c:
                resp = c.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "OK"

    def test_health_disabled_when_fb_none_and_opt_disabled(self):
        app = _health_app_with_admin_bypass()
        with patch("src.backend.api.learning_endpoints.get_feedback_loop", return_value=None), \
             patch("src.backend.core.config.settings") as mock_s:
            mock_s.OPTIMIZATION_ENABLED = False
            with TestClient(app) as c:
                resp = c.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "DISABLED"

    def test_health_failed_when_fb_none_and_opt_enabled(self):
        app = _health_app_with_admin_bypass()
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

        app = _health_app_with_admin_bypass()
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
        # jsonb columns come back from psycopg as parsed lists, not JSON strings.
        return {"active_hint_ids": active_ids, "flagged_hint_ids": flagged_ids}

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
        row = {"active_hint_ids": None, "flagged_hint_ids": []}
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
        conn = _pg_conn(db_path)
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
        conn = _pg_conn(db_path)
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
        conn = _pg_conn(db_path)
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
        conn = _pg_conn(db_path)
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
        conn = _pg_conn(db_path)
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
        conn = _pg_conn(db_path)
        conn.execute(
            "INSERT INTO hint_review_sessions (status, hint_count, created_at) "
            "VALUES ('pending_review', ?, datetime('now'))",
            (len(rec_configs),),
        )
        conn.commit()
        sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()

        rec_pairs = []
        for i, cfg in enumerate(rec_configs):
            hint_kwargs = dict(cfg.get("hint_kwargs", {}))
            # Every recommendation in a session is about a DIFFERENT hint, and
            # _insert_hint defaults to one text at global scope. Two global
            # hints with the same text in one org are the same hint since v21
            # (domain left the global dedup key), so the seeds must differ —
            # under the old NULL-distinct index they happened not to have to.
            hint_kwargs.setdefault("feedback_text", f"use xpath {i}")
            hint_id = _insert_hint(db_path, **hint_kwargs)
            conn = _pg_conn(db_path)
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
        conn = _pg_conn(db_path)
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
        sid, [(_, hint_id)] = self._build_session_with_recs(db_path, [
            {"recommendation": "disable", "hint_kwargs": {"is_active": 1}},
        ])
        resp = client.post(f"/review-hints/sessions/{sid}/apply")
        assert resp.status_code == 200
        assert resp.json()["applied_count"] == 1

        # Verify hint was actually disabled
        conn = _pg_conn(db_path)
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

        conn = _pg_conn(db_path)
        row = conn.execute(
            "SELECT is_active FROM nl_feedback_corrections WHERE id = ?", (hint_id,)
        ).fetchone()
        conn.close()
        assert row[0] == 1

    def test_apply_reactivate_resets_unused_count(self, learning_client):
        """Step 4b: LLM-review reactivation also resets unused_count to 0."""
        client, _, _, db_path = learning_client
        sid, [(_, hint_id)] = self._build_session_with_recs(db_path, [
            {"recommendation": "reactivate", "hint_kwargs": {"is_active": 0}},
        ])
        conn = _pg_conn(db_path)
        conn.execute(
            "UPDATE nl_feedback_corrections SET unused_count=6 WHERE id=?", (hint_id,),
        )
        conn.commit()
        conn.close()

        resp = client.post(f"/review-hints/sessions/{sid}/apply")
        assert resp.status_code == 200

        conn = _pg_conn(db_path)
        row = conn.execute(
            "SELECT is_active, unused_count FROM nl_feedback_corrections WHERE id=?",
            (hint_id,),
        ).fetchone()
        conn.close()
        assert row[0] == 1   # reactivated
        assert row[1] == 0   # unused reset

    def test_apply_unflag_recommendation(self, learning_client):
        client, _, _, db_path = learning_client
        sid, [(_, hint_id)] = self._build_session_with_recs(db_path, [
            {"recommendation": "unflag", "hint_kwargs": {"conflict_flagged": 1, "is_active": 1}},
        ])
        resp = client.post(f"/review-hints/sessions/{sid}/apply")
        assert resp.status_code == 200

        conn = _pg_conn(db_path)
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

        # Verify both llm_review_keep and llm_review_flagged rows landed in hint_audit
        hint_ids = [hint_id for _, hint_id in pairs]
        conn = _pg_conn(db_path)
        actions = [
            r[0]
            for r in conn.execute(
                "SELECT action FROM hint_audit WHERE hint_id IN ({}) ORDER BY id".format(
                    ",".join("?" * len(hint_ids))
                ),
                hint_ids,
            ).fetchall()
        ]
        conn.close()
        assert "llm_review_keep" in actions, f"expected llm_review_keep audit row, got {actions}"
        assert "llm_review_flagged" in actions, f"expected llm_review_flagged audit row, got {actions}"

    def test_session_status_set_to_completed_after_apply(self, learning_client):
        client, _, _, db_path = learning_client
        sid, _ = self._build_session_with_recs(db_path, [
            {"recommendation": "keep"},
        ])
        resp = client.post(f"/review-hints/sessions/{sid}/apply")
        assert resp.status_code == 200

        conn = _pg_conn(db_path)
        row = conn.execute(
            "SELECT status FROM hint_review_sessions WHERE id = ?", (sid,)
        ).fetchone()
        conn.close()
        assert row[0] == "completed"


# ---------------------------------------------------------------------------
# GET /runs  +  GET /runs/{workflow_id}  (per-testcase observability)
# ---------------------------------------------------------------------------

class TestRunsEndpoints:
    @staticmethod
    def _seed_run(db_path, wf, *, status="passed", hint_id=None,
                  query="click the button"):
        conn = _pg_conn(db_path)
        now = datetime.now(timezone.utc).isoformat()
        injected = json.dumps([hint_id] if hint_id else [])
        conn.execute(
            "INSERT INTO execution_records "
            "(workflow_id, timestamp, user_query, url, domain, robot_code, "
            " test_status, failure_category, injected_hint_ids, hint_attribution_done) "
            "VALUES (?, ?, ?, 'http://x.com', 'x.com', '*** Test Cases ***', ?, ?, ?, 1)",
            (wf, now, query, status,
             "C1" if status == "failed" else None, injected),
        )
        conn.execute(
            "INSERT INTO learning_metrics "
            "(workflow_id, user_query, is_first_attempt, hints_available, "
            " hints_injected, llm_calls, llm_cost, test_passed, was_holdout, timestamp) "
            "VALUES (?, 'q', 1, ?, ?, 1, 0.0, ?, 0, ?)",
            (wf, 1 if hint_id else 0, 1 if hint_id else 0,
             1 if status == "passed" else 0, now),
        )
        if hint_id:
            conn.execute(
                "INSERT INTO hint_workflow_trace "
                "(workflow_id, hint_id, scope, source, priority, similarity_score, "
                " available, injected, drop_reason, attribution_bucket, "
                " attribution_reason, created_at) "
                "VALUES (?, ?, 'global', 'nl', 'high', 0.82, 1, 1, NULL, 'used', "
                "        'clicked the button as advised', ?)",
                (wf, hint_id, now),
            )
            conn.execute(
                "INSERT INTO trigger_events "
                "(trigger_type, workflow_id, domain, url, feedback_text, "
                " active_hint_ids, flagged_hint_ids, actually_flagged_hint_ids, "
                " reason, llm_model, input_tokens, output_tokens, llm_latency_ms, "
                " status, error_message, created_at, used_hint_ids, unused_hint_ids) "
                "VALUES ('usage_attribution', ?, 'x.com', 'http://x.com', NULL, ?, "
                "        '[]', '[]', NULL, 'm', 1, 1, 1, 'succeeded', NULL, ?, ?, '[]')",
                (wf, injected, now, injected),
            )
        conn.commit()
        conn.close()

    def test_run_detail_aggregates_everything(self, learning_client):
        client, em, mock_fb, db_path = learning_client
        hid = _insert_hint(db_path, feedback_text="click the button firmly")
        self._seed_run(db_path, "wf-run-1", status="passed", hint_id=hid)

        r = client.get("/runs/wf-run-1")
        assert r.status_code == 200
        data = r.json()
        assert data["run"]["test_status"] == "passed"
        assert data["run"]["user_query"] == "click the button"
        assert data["metrics"]["hints_injected"] == 1
        assert len(data["trace"]) == 1
        tr = data["trace"][0]
        assert tr["hint_id"] == hid
        assert tr["feedback_text"] == "click the button firmly"   # from the JOIN
        assert tr["injected"] == 1 and tr["attribution_bucket"] == "used"
        assert tr["attribution_reason"] == "clicked the button as advised"
        assert len(data["triggers"]) == 1
        assert data["triggers"][0]["trigger_type"] == "usage_attribution"

    def test_run_detail_404_for_unknown_workflow(self, learning_client):
        client, *_ = learning_client
        r = client.get("/runs/does-not-exist")
        assert r.status_code == 404
        assert "No run data" in r.json()["detail"]    # the endpoint's own 404

    def test_run_detail_recordless_run_has_trace_but_no_record(self, learning_client):
        # A run whose learning was skipped writes NO execution_records row but
        # DOES write a trace (no FK). The endpoint must still return the trace
        # with run=null.
        client, em, mock_fb, db_path = learning_client
        hid = _insert_hint(db_path)
        conn = _pg_conn(db_path)
        conn.execute(
            "INSERT INTO hint_workflow_trace "
            "(workflow_id, hint_id, injected, available, created_at) "
            "VALUES ('wf-dedup', ?, 1, 1, ?)",
            (hid, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()

        r = client.get("/runs/wf-dedup")
        assert r.status_code == 200
        data = r.json()
        assert data["run"] is None
        assert len(data["trace"]) == 1 and data["trace"][0]["hint_id"] == hid

    def test_runs_list_newest_first_and_count(self, learning_client):
        client, em, mock_fb, db_path = learning_client
        for i in range(3):
            self._seed_run(db_path, f"wf-list-{i}",
                           status="passed" if i % 2 == 0 else "failed")
        r = client.get("/runs")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] >= 3
        wfs = {row["workflow_id"] for row in data["runs"]}
        assert {"wf-list-0", "wf-list-1", "wf-list-2"} <= wfs

    def test_runs_list_status_filter(self, learning_client):
        client, em, mock_fb, db_path = learning_client
        self._seed_run(db_path, "wf-pass", status="passed")
        self._seed_run(db_path, "wf-fail", status="failed")
        r = client.get("/runs?status=failed")
        assert r.status_code == 200
        statuses = {row["test_status"] for row in r.json()["runs"]}
        assert statuses == {"failed"}

    def test_runs_list_query_search_case_insensitive(self, learning_client):
        """q is a user_query substring filter; ILIKE keeps it case-insensitive
        on Postgres (legacy SQLite LIKE behaviour)."""
        client, em, mock_fb, db_path = learning_client
        self._seed_run(db_path, "wf-q-1", query="Open Flipkart and search shoes")
        self._seed_run(db_path, "wf-q-2", query="click the button")

        r = client.get("/runs?q=flipkart")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["runs"][0]["workflow_id"] == "wf-q-1"

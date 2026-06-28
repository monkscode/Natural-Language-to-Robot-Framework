"""Run-history scoping + per-owner report access (role-based functionality).

Covers the three layers added for the user/admin split:
- RunRegistry (Postgres `test_runs` on an isolated schema): write-once
  ownership upsert, status transitions, role-scoped listing, owner lookup.
- GET /api/history: regular users get only their own rows; validated admins
  get everything; a stale admin *claim* (token says admin, DB says no) falls
  back to own-rows.
- authorize_report_access: /reports/{run_id}/* is served only to the run's
  owner or a validated admin; unattributed/unknown runs are admin-only (fail
  closed). The artifact store keeps traversal inside the run directory.

Plus the History-reuse surface (drawer + "Run again"):
- robot_code persistence on test_runs (newest-non-NULL-wins).
- GET /api/history/{run_id} detail: owner-or-admin, 404 for foreign/unknown.
- POST /execute-test {rerun_of}: re-executes stored code as a new run with
  learning skipped (user_query=None) and the source query carried as the
  history description.
"""

from types import SimpleNamespace
from unittest.mock import patch

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.backend.api.history_endpoints import router as history_router
from src.backend.auth import jwt_utils
from src.backend.auth.jwt_utils import authorize_report_access, create_access_token, require_user
from src.backend.core.config import settings
from src.backend.core.run_registry import RunRegistry

_SCHEMA = "history_test"

_USER1 = {"user_id": "u1", "email": "user1@test.local", "role": "user", "name": "U1"}
_USER2 = {"user_id": "u2", "email": "user2@test.local", "role": "user", "name": "U2"}
_ADMIN = {"user_id": "adm", "email": "admin@test.local", "role": "admin", "name": "A"}


# ---------------------------------------------------------------------------
# Fixtures — RunRegistry on an isolated Postgres schema
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def _hist_admin_conn():
    conn = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def _hist_registry(_hist_admin_conn):
    _hist_admin_conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    _hist_admin_conn.execute(f"CREATE SCHEMA {_SCHEMA}")
    dsn = settings.DATABASE_URL + f"?options=-c%20search_path%3D{_SCHEMA},public"
    reg = RunRegistry(dsn=dsn)
    yield reg
    reg.close()
    _hist_admin_conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")


@pytest.fixture
def registry(_hist_registry, _hist_admin_conn):
    """Clean test_runs table per test."""
    _hist_admin_conn.execute(f"TRUNCATE {_SCHEMA}.test_runs")
    return _hist_registry


# ---------------------------------------------------------------------------
# RunRegistry
# ---------------------------------------------------------------------------

class TestRunRegistry:
    def test_record_and_list_roundtrip(self, registry):
        registry.record_start("run-1", _USER1, "open the login page", "generated")
        runs, total = registry.list_runs(user_id="u1")
        assert total == 1 and len(runs) == 1
        row = runs[0]
        assert row["run_id"] == "run-1"
        assert row["user_id"] == "u1"
        assert row["user_email"] == "user1@test.local"
        assert row["user_query"] == "open the login page"
        assert row["status"] == "generated"
        assert row["created_at"] and row["updated_at"]

    def test_upsert_never_steals_ownership(self, registry):
        # Generation attributes the run to user1...
        registry.record_start("run-1", _USER1, "click the buy button", "generated")
        # ...a later execute upsert (no query, different/absent user) only
        # advances the status — owner and query are write-once.
        registry.record_start("run-1", _USER2, None, "running")
        runs, total = registry.list_runs()
        assert total == 1
        assert runs[0]["user_id"] == "u1"
        assert runs[0]["user_query"] == "click the buy button"
        assert runs[0]["status"] == "running"

    def test_set_status_advances(self, registry):
        registry.record_start("run-1", _USER1, "q", "running")
        registry.set_status("run-1", "passed")
        runs, _ = registry.list_runs()
        assert runs[0]["status"] == "passed"

    def test_set_status_on_unknown_run_is_noop(self, registry):
        registry.set_status("never-recorded", "passed")
        assert registry.list_runs() == ([], 0)

    def test_list_scoping_and_order(self, registry):
        registry.record_start("run-a", _USER1, "q1", "passed")
        registry.record_start("run-b", _USER2, "q2", "failed")
        registry.record_start("run-c", _USER1, "q3", "running")

        own, own_total = registry.list_runs(user_id="u1")
        assert own_total == 2
        assert {r["run_id"] for r in own} == {"run-a", "run-c"}

        all_runs, all_total = registry.list_runs()
        assert all_total == 3
        # Newest-first ordering.
        assert [r["run_id"] for r in all_runs] == ["run-c", "run-b", "run-a"]

    def test_pagination(self, registry):
        for i in range(3):
            registry.record_start(f"run-{i}", _USER1, f"q{i}", "passed")
        page1, total = registry.list_runs(user_id="u1", limit=2, offset=0)
        page2, _ = registry.list_runs(user_id="u1", limit=2, offset=2)
        assert total == 3
        assert len(page1) == 2 and len(page2) == 1
        assert {r["run_id"] for r in page1} | {r["run_id"] for r in page2} == {
            "run-0", "run-1", "run-2"
        }

    def test_status_filter_scopes_rows_and_total(self, registry):
        registry.record_start("run-p1", _USER1, "q", "passed")
        registry.record_start("run-p2", _USER1, "q", "passed")
        registry.record_start("run-f1", _USER1, "q", "failed")
        registry.record_start("run-other", _USER2, "q", "passed")

        passed, passed_total = registry.list_runs(user_id="u1", status="passed")
        assert passed_total == 2
        assert {r["run_id"] for r in passed} == {"run-p1", "run-p2"}

        # status combines with the ownership scope (admin: user_id=None).
        all_passed, all_passed_total = registry.list_runs(status="passed")
        assert all_passed_total == 3
        assert {r["run_id"] for r in all_passed} == {"run-p1", "run-p2", "run-other"}

    def test_get_owner(self, registry):
        registry.record_start("owned", _USER1, "q", "passed")
        registry.record_start("anonymous", None, "q", "passed")
        assert registry.get_owner("owned") == "u1"
        assert registry.get_owner("anonymous") is None
        assert registry.get_owner("missing") is None

    def test_q_search_spans_query_email_and_id(self, registry):
        registry.record_start("run-shoes", _USER1, "search for shoes", "passed")
        registry.record_start("run-cart", _USER1, "check the cart", "passed")
        registry.record_start("run-u2", _USER2, "open the dashboard", "passed")

        # Case-insensitive substring over the description...
        rows, total = registry.list_runs(q="SHOES")
        assert total == 1 and rows[0]["run_id"] == "run-shoes"
        # ...over the owner email (admin scope)...
        rows, total = registry.list_runs(q="user2@")
        assert total == 1 and rows[0]["run_id"] == "run-u2"
        # ...and over the run id.
        rows, total = registry.list_runs(q="run-cart")
        assert total == 1 and rows[0]["run_id"] == "run-cart"
        # q combines with the ownership scope (user1 cannot reach run-u2).
        rows, total = registry.list_runs(user_id="u1", q="dashboard")
        assert total == 0 and rows == []

    def test_q_wildcards_are_matched_literally(self, registry):
        # A typed % must not act as a SQL wildcard (it is LIKE-escaped).
        registry.record_start("run-pct", _USER1, "discount 50% off", "passed")
        registry.record_start("run-plain", _USER1, "discount today", "passed")
        rows, total = registry.list_runs(q="50%")
        assert total == 1 and rows[0]["run_id"] == "run-pct"


# ---------------------------------------------------------------------------
# GET /api/history — role scoping
# ---------------------------------------------------------------------------

def _client(registry, user):
    app = FastAPI()
    app.include_router(history_router, prefix="/api")
    app.dependency_overrides[require_user] = lambda: user
    patcher = patch(
        "src.backend.api.history_endpoints.get_run_registry", return_value=registry
    )
    patcher.start()
    client = TestClient(app)
    client._registry_patcher = patcher  # stopped by the caller via _close
    return client


def _close(client):
    client._registry_patcher.stop()
    client.close()


@pytest.fixture
def seeded(registry):
    registry.record_start("run-u1-a", _USER1, "search for shoes", "passed")
    registry.record_start("run-u1-b", _USER1, "check the cart", "generated")
    registry.record_start("run-u2-a", _USER2, "open the dashboard", "failed")
    return registry


class TestHistoryEndpoint:
    def test_user_sees_only_own_runs(self, seeded):
        client = _client(seeded, _USER1)
        try:
            body = client.get("/api/history").json()
        finally:
            _close(client)
        assert body["scope"] == "own"
        assert body["total"] == 2
        ids = {r["run_id"] for r in body["runs"]}
        assert ids == {"run-u1-a", "run-u1-b"}
        # Identity columns are not echoed back to non-admins.
        assert all("user_email" not in r and "user_id" not in r for r in body["runs"])
        by_id = {r["run_id"]: r for r in body["runs"]}
        assert by_id["run-u1-a"]["has_report"] is True      # passed -> artifacts
        assert by_id["run-u1-b"]["has_report"] is False     # generated -> none

    def test_admin_sees_all_runs_with_user_column(self, seeded):
        with patch(
            "src.backend.api.history_endpoints.is_validated_admin", return_value=True
        ):
            client = _client(seeded, _ADMIN)
            try:
                body = client.get("/api/history").json()
            finally:
                _close(client)
        assert body["scope"] == "all"
        assert body["total"] == 3
        emails = {r["user_email"] for r in body["runs"]}
        assert emails == {"user1@test.local", "user2@test.local"}

    def test_stale_admin_claim_falls_back_to_own_scope(self, seeded):
        # Token says admin, but the users table no longer agrees (demoted or
        # deactivated): scope degrades to the user's own rows.
        with patch(
            "src.backend.api.history_endpoints.is_validated_admin", return_value=False
        ):
            client = _client(seeded, {**_ADMIN, "user_id": "u2"})
            try:
                body = client.get("/api/history").json()
            finally:
                _close(client)
        assert body["scope"] == "own"
        assert {r["run_id"] for r in body["runs"]} == {"run-u2-a"}

    def test_permissive_mode_without_token_lists_all(self, seeded):
        # AUTH_ENFORCED=False + no token -> require_user yields None (dev hatch).
        client = _client(seeded, None)
        try:
            body = client.get("/api/history").json()
        finally:
            _close(client)
        assert body["scope"] == "all"
        assert body["total"] == 3

    def test_limit_is_applied(self, seeded):
        client = _client(seeded, _USER1)
        try:
            body = client.get("/api/history?limit=1").json()
        finally:
            _close(client)
        assert body["total"] == 2 and len(body["runs"]) == 1

    def test_status_filter_scopes_rows_total_and_pagination(self, seeded):
        # user1 owns one passed + one generated run; the passed tab must
        # count and return ONLY passed rows (History's per-tab Load more).
        client = _client(seeded, _USER1)
        try:
            body = client.get("/api/history?status=passed").json()
        finally:
            _close(client)
        assert body["total"] == 1
        assert [r["run_id"] for r in body["runs"]] == ["run-u1-a"]

    def test_unknown_status_value_is_rejected(self, seeded):
        client = _client(seeded, _USER1)
        try:
            resp = client.get("/api/history?status=bogus")
        finally:
            _close(client)
        assert resp.status_code == 422  # Literal validation


# ---------------------------------------------------------------------------
# /reports ownership (authorize_report_access)
# ---------------------------------------------------------------------------

def _token(user):
    return create_access_token(
        {"id": user["user_id"], "email": user["email"],
         "role": user["role"], "display_name": user["name"]}
    )


def _request(token=None):
    # The ownership gate reads only the credential now (the run_id arrives as a
    # route parameter), so the request stub no longer carries a URL path.
    return SimpleNamespace(
        headers={"Authorization": f"Bearer {token}"} if token else {},
        cookies={},
    )


@pytest.fixture
def reports_registry(registry):
    """Registry patched into the lazy import inside authorize_report_access."""
    registry.record_start("run-owned", _USER1, "q", "passed")
    registry.record_start("run-anon", None, "q", "passed")
    with patch(
        "src.backend.core.run_registry.get_run_registry", return_value=registry
    ):
        yield registry


class TestReportsOwnership:
    def test_owner_can_open_own_report(self, reports_registry):
        assert authorize_report_access(_request(_token(_USER1)), "run-owned") is None

    def test_other_user_gets_403(self, reports_registry):
        resp = authorize_report_access(_request(_token(_USER2)), "run-owned")
        assert resp is not None and resp.status_code == 403

    def test_admin_can_open_any_report(self, reports_registry):
        # The tests_api conftest patches jwt_utils._admin_repo.get_by_id to an
        # active admin row, so the admin claim validates.
        assert authorize_report_access(_request(_token(_ADMIN)), "run-owned") is None

    def test_unattributed_run_is_admin_only(self, reports_registry):
        user_resp = authorize_report_access(_request(_token(_USER1)), "run-anon")
        assert user_resp is not None and user_resp.status_code == 403
        assert authorize_report_access(_request(_token(_ADMIN)), "run-anon") is None

    def test_unknown_run_is_denied_for_users(self, reports_registry):
        resp = authorize_report_access(_request(_token(_USER1)), "no-such-run")
        assert resp is not None and resp.status_code == 403

    def test_stale_admin_claim_is_denied_like_a_user(self, reports_registry):
        with patch.object(jwt_utils, "is_validated_admin", return_value=False):
            resp = authorize_report_access(_request(_token(_ADMIN)), "run-anon")
        assert resp is not None and resp.status_code == 403

    def test_tokenless_enforced_is_401(self, reports_registry):
        with patch.object(settings, "AUTH_ENFORCED", True):
            resp = authorize_report_access(_request(), "run-owned")
        assert resp is not None and resp.status_code == 401

    def test_tokenless_permissive_passes(self, reports_registry):
        with patch.object(settings, "AUTH_ENFORCED", False):
            assert authorize_report_access(_request(), "run-owned") is None

    def test_invalid_token_is_401(self, reports_registry):
        resp = authorize_report_access(_request("garbage"), "run-owned")
        assert resp is not None and resp.status_code == 401


# ---------------------------------------------------------------------------
# /reports route — owner-gated serving + the closed bypass class (end to end)
# ---------------------------------------------------------------------------

class TestReportRouteServing:
    _VICTIM = "11111111-1111-4111-8111-111111111111"     # u1's run, holds a secret
    _ATTACKER = "22222222-2222-4222-8222-222222222222"   # u2's own run

    @pytest.fixture
    def report_app(self, registry, tmp_path, monkeypatch):
        import src.backend.api.report_endpoints as re_mod
        registry.record_start(self._VICTIM, _USER1, "q", "passed")
        registry.record_start(self._ATTACKER, _USER2, "q", "passed")
        (tmp_path / self._VICTIM).mkdir()
        (tmp_path / self._VICTIM / "log.html").write_text(
            "typed password: hunter2", encoding="utf-8"
        )
        (tmp_path / self._ATTACKER).mkdir()
        (tmp_path / self._ATTACKER / "log.html").write_text("own log", encoding="utf-8")
        shots = tmp_path / self._VICTIM / "browser" / "screenshot"
        shots.mkdir(parents=True)
        (shots / "fail-screenshot-1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        from src.backend.core.artifact_store import LocalArtifactStore
        monkeypatch.setattr(
            re_mod, "get_artifact_store", lambda: LocalArtifactStore(tmp_path))
        reg_patch = patch(
            "src.backend.core.run_registry.get_run_registry", return_value=registry
        )
        reg_patch.start()
        app = FastAPI()
        app.include_router(re_mod.router)
        client = TestClient(app)
        yield client
        reg_patch.stop()
        client.close()

    @staticmethod
    def _auth(user):
        return {"Authorization": f"Bearer {_token(user)}"}

    def test_owner_gets_own_report(self, report_app):
        r = report_app.get(f"/reports/{self._VICTIM}/log.html", headers=self._auth(_USER1))
        assert r.status_code == 200 and "hunter2" in r.text
        assert r.headers["cache-control"] == "private"

    def test_non_owner_gets_403(self, report_app):
        r = report_app.get(f"/reports/{self._VICTIM}/log.html", headers=self._auth(_USER2))
        assert r.status_code == 403

    def test_double_slash_bypass_is_closed(self, report_app):
        # Historical bypass #1: an empty leading segment made the ownership check
        # read an empty run_id (allowed) while StaticFiles served the victim's
        # file. u2 must never receive u1's secret, by any status code.
        r = report_app.get(f"/reports//{self._VICTIM}/log.html", headers=self._auth(_USER2))
        assert r.status_code != 200
        assert "hunter2" not in r.text

    def test_dot_dot_bypass_is_closed(self, report_app):
        # Historical bypass #2: own a run, climb out via '..'. The ownership
        # check passes on u2's own id, but file resolution must stay inside it.
        r = report_app.get(
            f"/reports/{self._ATTACKER}/../{self._VICTIM}/log.html",
            headers=self._auth(_USER2),
        )
        assert r.status_code != 200
        assert "hunter2" not in r.text

    def test_non_owner_is_403_before_file_resolution(self, report_app):
        # Auth runs before store.serve_artifact, so a non-owner is denied (403)
        # whether or not the path would resolve — file existence never leaks.
        r = report_app.get("/reports/not-a-uuid/log.html", headers=self._auth(_USER2))
        assert r.status_code == 403

    def test_non_uuid_run_id_is_404_past_auth(self, report_app):
        # Admin clears the ownership gate, so this exercises the store's UUID
        # guard: a non-UUID run id resolves to no file.
        r = report_app.get("/reports/not-a-uuid/log.html", headers=self._auth(_ADMIN))
        assert r.status_code == 404

    def test_owner_gets_nested_screenshot(self, report_app):
        # The real log.html loads screenshots as a relative sub-resource; the
        # owner must be able to fetch the nested path through the same route.
        r = report_app.get(
            f"/reports/{self._VICTIM}/browser/screenshot/fail-screenshot-1.png",
            headers=self._auth(_USER1),
        )
        assert r.status_code == 200 and r.content.startswith(b"\x89PNG")

    def test_head_request_serves_headers_without_body(self, report_app):
        r = report_app.head(f"/reports/{self._VICTIM}/log.html", headers=self._auth(_USER1))
        assert r.status_code == 200 and r.content == b""

    def test_trailing_slash_directory_is_404_for_owner(self, report_app):
        # /reports/<id>/ -> empty file_path -> the run directory itself: the
        # owner passes auth but resolution refuses a directory (never a 500).
        r = report_app.get(f"/reports/{self._VICTIM}/", headers=self._auth(_USER1))
        assert r.status_code == 404

    def test_encoded_dot_dot_bypass_is_closed(self, report_app):
        # Percent-encoded traversal must not leak the victim's secret either.
        r = report_app.get(
            f"/reports/{self._ATTACKER}/%2e%2e/{self._VICTIM}/log.html",
            headers=self._auth(_USER2),
        )
        assert r.status_code != 200
        assert "hunter2" not in r.text

    def test_report_response_carries_csp_sandbox(self, report_app):
        r = report_app.get(f"/reports/{self._VICTIM}/log.html", headers=self._auth(_USER1))
        assert r.status_code == 200
        csp = r.headers.get("content-security-policy", "")
        assert "sandbox" in csp
        assert "allow-scripts" in csp
        # The absence of allow-same-origin is what produces the opaque origin.
        assert "allow-same-origin" not in csp
        # The existing privacy header must remain.
        assert r.headers.get("cache-control") == "private"


# ---------------------------------------------------------------------------
# /api/feedback — owner-or-admin authorization
# ---------------------------------------------------------------------------

def _feedback_client(registry, user, validated_admin=False):
    from unittest.mock import MagicMock
    from src.backend.api.endpoints import router as api_router

    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[require_user] = lambda: user

    mock_fb = MagicMock()
    mock_fb.process_user_feedback.return_value = {"category": "locator"}

    patchers = [
        patch("src.backend.api.endpoints.get_run_registry", return_value=registry),
        patch("src.backend.api.endpoints.is_validated_admin", return_value=validated_admin),
        patch("src.backend.api.endpoints.get_feedback_loop", return_value=mock_fb),
    ]
    for p in patchers:
        p.start()
    client = TestClient(app)
    client._patchers = patchers
    client._mock_fb = mock_fb  # for asserting which run the feedback landed on
    return client


def _close_feedback(client):
    for p in client._patchers:
        p.stop()
    client.close()


_FEEDBACK_BODY = {
    "workflow_id": "run-owned",
    "feedback_text": "use a stable locator",
    "feedback_type": "close_enough",
}


class TestFeedbackOwnership:
    @pytest.fixture
    def fb_registry(self, registry):
        registry.record_start("run-owned", _USER1, "q", "passed")
        registry.record_start("run-anon", None, "q", "passed")
        return registry

    def test_owner_can_submit_feedback(self, fb_registry):
        client = _feedback_client(fb_registry, _USER1)
        try:
            resp = client.post("/api/feedback", json=_FEEDBACK_BODY)
        finally:
            _close_feedback(client)
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    def test_feedback_forwards_submitter_email_as_actor(self, fb_registry):
        """The handler forwards the authenticated user's email as the actor
        kwarg, so the NL engine records who implicitly unflagged a hint. Passed
        as a kwarg so existing positional call_args[0][0] checks stay valid."""
        client = _feedback_client(fb_registry, _USER1)
        try:
            resp = client.post("/api/feedback", json=_FEEDBACK_BODY)
        finally:
            _close_feedback(client)
        assert resp.status_code == 200
        call = client._mock_fb.process_user_feedback.call_args
        assert call.kwargs["actor"] == _USER1["email"]
        assert call[0][0] == "run-owned"

    def test_other_user_gets_403(self, fb_registry):
        client = _feedback_client(fb_registry, _USER2)
        try:
            resp = client.post("/api/feedback", json=_FEEDBACK_BODY)
        finally:
            _close_feedback(client)
        assert resp.status_code == 403

    def test_validated_admin_can_submit_for_any_run(self, fb_registry):
        client = _feedback_client(fb_registry, _ADMIN, validated_admin=True)
        try:
            resp = client.post("/api/feedback", json=_FEEDBACK_BODY)
        finally:
            _close_feedback(client)
        assert resp.status_code == 200

    def test_unattributed_run_is_admin_only(self, fb_registry):
        client = _feedback_client(fb_registry, _USER1)
        try:
            resp = client.post(
                "/api/feedback", json={**_FEEDBACK_BODY, "workflow_id": "run-anon"}
            )
        finally:
            _close_feedback(client)
        assert resp.status_code == 403

    def test_permissive_mode_without_token_skips_ownership(self, fb_registry):
        client = _feedback_client(fb_registry, None)
        try:
            resp = client.post("/api/feedback", json=_FEEDBACK_BODY)
        finally:
            _close_feedback(client)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# execute-test — a foreign workflow_id forks to a fresh run id
# ---------------------------------------------------------------------------

class TestExecuteRunIdFork:
    @staticmethod
    def _run_execute(registry, workflow_id, user):
        import asyncio
        from src.backend.services import workflow_service as ws

        seen = {}

        async def fake_docker(run_id, robot_code, user_query, release_slot):
            seen["run_id"] = run_id
            release_slot()
            yield "data: ok\n\n"

        with patch.object(ws, "_stream_docker_execution", fake_docker), \
             patch.object(ws, "get_run_registry", return_value=registry):
            async def consume():
                async for _ in ws.stream_execute_only(
                    "*** Test Cases ***\nT\n    Log    x", None, workflow_id, user=user
                ):
                    pass
            asyncio.run(consume())
        return seen.get("run_id")

    def test_owner_reuses_their_run_id(self, registry):
        rid = "11111111-1111-4111-8111-111111111111"
        registry.record_start(rid, _USER1, "q", "generated")
        assert self._run_execute(registry, rid, _USER1) == rid
        assert registry.get_owner(rid) == "u1"

    def test_foreign_run_id_forks_to_fresh_id(self, registry):
        rid = "22222222-2222-4222-8222-222222222222"
        registry.record_start(rid, _USER1, "the victim's run", "passed")
        forked = self._run_execute(registry, rid, _USER2)
        assert forked and forked != rid
        # The victim's row is untouched; the fork belongs to the requester.
        runs, _ = registry.list_runs(user_id="u1")
        assert runs[0]["run_id"] == rid and runs[0]["status"] == "passed"
        assert registry.get_owner(forked) == "u2"

    def test_unattributed_run_id_can_be_claimed(self, registry):
        rid = "33333333-3333-4333-8333-333333333333"
        registry.record_start(rid, None, "q", "generated")
        assert self._run_execute(registry, rid, _USER2) == rid
        assert registry.get_owner(rid) == "u2"


# ---------------------------------------------------------------------------
# workflow_service history hooks never break the pipeline
# ---------------------------------------------------------------------------

class TestHistoryHooksFailOpen:
    def test_record_run_swallows_registry_bootstrap_failure(self):
        from src.backend.services import workflow_service
        with patch.object(
            workflow_service, "get_run_registry", side_effect=RuntimeError("pg down")
        ):
            workflow_service._record_run("rid", _USER1, "q", "running")  # must not raise

    def test_set_run_status_swallows_registry_bootstrap_failure(self):
        from src.backend.services import workflow_service
        with patch.object(
            workflow_service, "get_run_registry", side_effect=RuntimeError("pg down")
        ):
            workflow_service._set_run_status("rid", "passed")  # must not raise


# ---------------------------------------------------------------------------
# RunRegistry — robot_code persistence (the History "Run again" source)
# ---------------------------------------------------------------------------

class TestRobotCodePersistence:
    def test_get_run_returns_stored_code(self, registry):
        registry.record_start(
            "run-1", _USER1, "q", "generated", robot_code="*** Test Cases ***"
        )
        run = registry.get_run("run-1")
        assert run["robot_code"] == "*** Test Cases ***"
        assert run["user_id"] == "u1"
        assert run["status"] == "generated"
        assert registry.get_run("missing") is None

    def test_executed_code_replaces_generated_code(self, registry):
        # Generate (v1) -> user edits in the UI -> execute (v2): the row must
        # hold what actually RAN, so "Run again" re-executes it verbatim.
        registry.record_start("run-1", _USER1, "q", "generated", robot_code="v1")
        registry.record_start("run-1", _USER1, None, "running", robot_code="v2-edited")
        assert registry.get_run("run-1")["robot_code"] == "v2-edited"

    def test_codeless_status_update_keeps_existing_code(self, registry):
        registry.record_start("run-1", _USER1, "q", "running", robot_code="the code")
        registry.record_start("run-1", _USER1, None, "running")  # no code passed
        registry.set_status("run-1", "passed")
        run = registry.get_run("run-1")
        assert run["robot_code"] == "the code"
        assert run["status"] == "passed"

    def test_rerun_lineage_is_write_once(self, registry):
        registry.record_start(
            "rerun-1", _USER1, "q", "running", robot_code="c", rerun_of="root-1"
        )
        registry.record_start("rerun-1", _USER1, None, "running")  # status advance
        assert registry.get_run("rerun-1")["rerun_of"] == "root-1"
        # Normal runs carry no lineage.
        registry.record_start("plain", _USER1, "q", "generated")
        assert registry.get_run("plain")["rerun_of"] is None
        # The list view carries the lineage too (History's Re-run badge).
        by_id = {r["run_id"]: r for r in registry.list_runs(user_id="u1")[0]}
        assert by_id["rerun-1"]["rerun_of"] == "root-1"
        assert by_id["plain"]["rerun_of"] is None


# ---------------------------------------------------------------------------
# GET /api/history/{run_id} — detail drawer (owner-or-admin)
# ---------------------------------------------------------------------------

_RID_OWNED = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"   # user1's, code in column
_RID_FOREIGN = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"  # user2's
_RID_ANON = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"     # unattributed legacy
_RID_NOCODE = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"   # user1's, no stored code


@pytest.fixture
def detail_seeded(registry):
    registry.record_start(
        _RID_OWNED, _USER1, "search for shoes", "passed", robot_code="*** Code 1 ***"
    )
    registry.record_start(
        _RID_FOREIGN, _USER2, "open the dashboard", "passed", robot_code="*** Code 2 ***"
    )
    registry.record_start(_RID_ANON, None, "legacy run", "passed", robot_code="*** Anon ***")
    registry.record_start(_RID_NOCODE, _USER1, "generate only", "generated")
    return registry


class TestRunDetailEndpoint:
    def test_owner_gets_row_with_code(self, detail_seeded):
        client = _client(detail_seeded, _USER1)
        try:
            resp = client.get(f"/api/history/{_RID_OWNED}")
        finally:
            _close(client)
        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == _RID_OWNED
        assert body["robot_code"] == "*** Code 1 ***"
        assert body["has_report"] is True
        # Identity columns are not echoed back to non-admins.
        assert "user_id" not in body and "user_email" not in body

    def test_foreign_run_is_404_for_users(self, detail_seeded):
        client = _client(detail_seeded, _USER1)
        try:
            resp = client.get(f"/api/history/{_RID_FOREIGN}")
        finally:
            _close(client)
        assert resp.status_code == 404

    def test_admin_sees_any_run_with_identity(self, detail_seeded):
        with patch(
            "src.backend.api.history_endpoints.is_validated_admin", return_value=True
        ):
            client = _client(detail_seeded, _ADMIN)
            try:
                resp = client.get(f"/api/history/{_RID_FOREIGN}")
            finally:
                _close(client)
        assert resp.status_code == 200
        assert resp.json()["user_email"] == "user2@test.local"

    def test_unattributed_run_is_admin_only(self, detail_seeded):
        client = _client(detail_seeded, _USER1)
        try:
            user_resp = client.get(f"/api/history/{_RID_ANON}")
        finally:
            _close(client)
        assert user_resp.status_code == 404

        with patch(
            "src.backend.api.history_endpoints.is_validated_admin", return_value=True
        ):
            client = _client(detail_seeded, _ADMIN)
            try:
                admin_resp = client.get(f"/api/history/{_RID_ANON}")
            finally:
                _close(client)
        assert admin_resp.status_code == 200

    def test_invalid_id_is_400(self, detail_seeded):
        client = _client(detail_seeded, _USER1)
        try:
            resp = client.get("/api/history/not-a-uuid")
        finally:
            _close(client)
        assert resp.status_code == 400

    def test_unknown_uuid_is_404(self, detail_seeded):
        client = _client(detail_seeded, _USER1)
        try:
            resp = client.get("/api/history/eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
        finally:
            _close(client)
        assert resp.status_code == 404

    def test_disk_fallback_for_pre_column_runs(self, detail_seeded, tmp_path):
        run_dir = tmp_path / _RID_NOCODE
        run_dir.mkdir()
        (run_dir / "test.robot").write_text("*** From Disk ***", encoding="utf-8")
        from src.backend.core.artifact_store import LocalArtifactStore
        import src.backend.api.history_endpoints as he_mod
        with patch.object(he_mod, "get_artifact_store",
                          return_value=LocalArtifactStore(tmp_path)):
            client = _client(detail_seeded, _USER1)
            try:
                body = client.get(f"/api/history/{_RID_NOCODE}").json()
            finally:
                _close(client)
        assert body["robot_code"] == "*** From Disk ***"

    def test_no_code_anywhere_is_null(self, detail_seeded, tmp_path):
        from src.backend.core.artifact_store import LocalArtifactStore
        import src.backend.api.history_endpoints as he_mod
        with patch.object(he_mod, "get_artifact_store",
                          return_value=LocalArtifactStore(tmp_path)):
            client = _client(detail_seeded, _USER1)
            try:
                body = client.get(f"/api/history/{_RID_NOCODE}").json()
            finally:
                _close(client)
        assert body["robot_code"] is None
        assert body["has_report"] is False


# ---------------------------------------------------------------------------
# POST /execute-test {rerun_of} — re-execute stored code, learning skipped
# ---------------------------------------------------------------------------

def _rerun_client(registry, user, validated_admin=False):
    """TestClient over the api router with stream_execute_only faked; returns
    (client, captured) where captured holds the kwargs the rerun passed in."""
    from src.backend.api.endpoints import router as api_router

    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[require_user] = lambda: user

    captured = {}

    async def fake_stream(robot_code, user_query=None, workflow_id=None,
                          user=None, history_query=None, rerun_of=None):
        captured.update(
            robot_code=robot_code, user_query=user_query,
            workflow_id=workflow_id, user=user, history_query=history_query,
            rerun_of=rerun_of,
        )
        yield "data: {\"stage\": \"execution\", \"status\": \"complete\"}\n\n"

    patchers = [
        patch("src.backend.api.endpoints.get_run_registry", return_value=registry),
        patch("src.backend.api.endpoints.is_validated_admin", return_value=validated_admin),
        patch("src.backend.api.endpoints.stream_execute_only", fake_stream),
    ]
    for p in patchers:
        p.start()
    client = TestClient(app)
    client._patchers = patchers
    return client, captured


class TestRerunEndpoint:
    def test_owner_rerun_uses_stored_code_without_learning(self, detail_seeded):
        client, captured = _rerun_client(detail_seeded, _USER1)
        try:
            resp = client.post("/execute-test", json={"rerun_of": _RID_OWNED})
        finally:
            _close_feedback(client)
        assert resp.status_code == 200
        assert captured["robot_code"] == "*** Code 1 ***"
        # Learning input stays empty; the query rides only as the history label.
        assert captured["user_query"] is None
        assert captured["history_query"] == "search for shoes"
        # Fresh run id (no workflow_id reuse) attributed to the requester.
        assert captured["workflow_id"] is None
        assert captured["user"] == _USER1
        # Lineage anchor: the new row links back to the source run.
        assert captured["rerun_of"] == _RID_OWNED

    def test_chain_rerun_flattens_to_the_original(self, detail_seeded):
        # _RID_CHAIN is itself a re-run of _RID_OWNED. Re-running it must
        # anchor to the ORIGINAL run (where the learning record lives), not
        # to the intermediate re-run row.
        chain_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"
        detail_seeded.record_start(
            chain_id, _USER1, "search for shoes", "passed",
            robot_code="*** Chain ***", rerun_of=_RID_OWNED,
        )
        client, captured = _rerun_client(detail_seeded, _USER1)
        try:
            resp = client.post("/execute-test", json={"rerun_of": chain_id})
        finally:
            _close_feedback(client)
        assert resp.status_code == 200
        assert captured["robot_code"] == "*** Chain ***"
        assert captured["rerun_of"] == _RID_OWNED

    def test_foreign_run_is_404_for_users(self, detail_seeded):
        client, captured = _rerun_client(detail_seeded, _USER2)
        try:
            resp = client.post("/execute-test", json={"rerun_of": _RID_OWNED})
        finally:
            _close_feedback(client)
        assert resp.status_code == 404
        assert captured == {}  # nothing executed

    def test_validated_admin_can_rerun_any_run(self, detail_seeded):
        client, captured = _rerun_client(detail_seeded, _ADMIN, validated_admin=True)
        try:
            resp = client.post("/execute-test", json={"rerun_of": _RID_FOREIGN})
        finally:
            _close_feedback(client)
        assert resp.status_code == 200
        assert captured["robot_code"] == "*** Code 2 ***"

    def test_run_without_stored_code_is_409(self, detail_seeded, tmp_path):
        from src.backend.core.artifact_store import LocalArtifactStore
        import src.backend.api.history_endpoints as he_mod
        with patch.object(he_mod, "get_artifact_store",
                          return_value=LocalArtifactStore(tmp_path)):
            client, captured = _rerun_client(detail_seeded, _USER1)
            try:
                resp = client.post("/execute-test", json={"rerun_of": _RID_NOCODE})
            finally:
                _close_feedback(client)
        assert resp.status_code == 409
        assert captured == {}

    def test_invalid_rerun_id_is_400(self, detail_seeded):
        client, _ = _rerun_client(detail_seeded, _USER1)
        try:
            resp = client.post("/execute-test", json={"rerun_of": "not-a-uuid"})
        finally:
            _close_feedback(client)
        assert resp.status_code == 400

    def test_unknown_rerun_id_is_404(self, detail_seeded):
        client, _ = _rerun_client(detail_seeded, _USER1)
        try:
            resp = client.post(
                "/execute-test",
                json={"rerun_of": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"},
            )
        finally:
            _close_feedback(client)
        assert resp.status_code == 404

    def test_plain_execute_without_code_still_400(self, detail_seeded):
        # robot_code became optional in the request model (rerun_of is the
        # alternative) — omitting BOTH keeps the original 400 contract.
        client, _ = _rerun_client(detail_seeded, _USER1)
        try:
            resp = client.post("/execute-test", json={})
        finally:
            _close_feedback(client)
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# stream_execute_only rerun plumbing — history label without learning input
# ---------------------------------------------------------------------------

class TestRerunServicePlumbing:
    def test_history_query_lands_on_row_while_learning_gets_none(self, registry):
        import asyncio
        from src.backend.services import workflow_service as ws

        seen = {}

        async def fake_docker(run_id, robot_code, user_query, release_slot):
            seen["run_id"] = run_id
            seen["learning_query"] = user_query
            release_slot()
            yield "data: ok\n\n"

        with patch.object(ws, "_stream_docker_execution", fake_docker), \
             patch.object(ws, "get_run_registry", return_value=registry):
            async def consume():
                async for _ in ws.stream_execute_only(
                    "*** Rerun Code ***", None, None,
                    user=_USER2, history_query="the original query",
                    rerun_of="root-run-id",
                ):
                    pass
            asyncio.run(consume())

        row = registry.get_run(seen["run_id"])
        assert row["user_query"] == "the original query"   # history description
        assert row["robot_code"] == "*** Rerun Code ***"   # rerunnable again
        assert row["user_id"] == "u2"                      # attributed to clicker
        assert row["rerun_of"] == "root-run-id"            # lineage for feedback
        assert seen["learning_query"] is None              # learning skipped


# ---------------------------------------------------------------------------
# /api/feedback on a re-run row — redirected to the original run
# ---------------------------------------------------------------------------

class TestFeedbackRerunRedirect:
    @pytest.fixture
    def lineage_registry(self, registry):
        registry.record_start("run-original", _USER1, "q", "passed")
        registry.record_start(
            "run-rerun", _USER1, "q", "passed", rerun_of="run-original"
        )
        return registry

    def test_feedback_on_rerun_applies_to_the_original_run(self, lineage_registry):
        client = _feedback_client(lineage_registry, _USER1)
        try:
            resp = client.post(
                "/api/feedback", json={**_FEEDBACK_BODY, "workflow_id": "run-rerun"}
            )
        finally:
            _close_feedback(client)
        assert resp.status_code == 200
        assert resp.json()["applied_to"] == "run-original"
        # The learning store was addressed with the ORIGINAL run id — the one
        # that owns the execution record (re-runs skip learning entirely).
        client._mock_fb.process_user_feedback.assert_called_once()
        assert client._mock_fb.process_user_feedback.call_args[0][0] == "run-original"

    def test_feedback_on_a_normal_run_is_not_redirected(self, lineage_registry):
        client = _feedback_client(lineage_registry, _USER1)
        try:
            resp = client.post(
                "/api/feedback", json={**_FEEDBACK_BODY, "workflow_id": "run-original"}
            )
        finally:
            _close_feedback(client)
        assert resp.status_code == 200
        assert resp.json()["applied_to"] == "run-original"
        assert client._mock_fb.process_user_feedback.call_args[0][0] == "run-original"

    def test_rerun_feedback_still_requires_ownership_of_the_rerun(self, lineage_registry):
        client = _feedback_client(lineage_registry, _USER2)
        try:
            resp = client.post(
                "/api/feedback", json={**_FEEDBACK_BODY, "workflow_id": "run-rerun"}
            )
        finally:
            _close_feedback(client)
        assert resp.status_code == 403
        client._mock_fb.process_user_feedback.assert_not_called()

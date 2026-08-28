"""T8 Step 6 — GET /api/feedback/{run_id}: what this run already contributed.

T5 made a second submission of the same correction from the same run a no-op.
The decision was NOT to warn about the ignored duplicate — nothing is lost, and
the endpoint could not know the outcome anyway (the gate runs on the writer
thread inside a queued job, long after the response is sent). The trust signal
is visible memory instead: the user sees their own words on file.

The gate is the load-bearing part. Correction text is user-authored content
about a customer's site, so a run-id-only read leaks it across orgs. This route
applies exactly the gate POST /api/feedback applies — the same unscoped lookup,
the same caller_can_access refusal (is_grouped deliberately NOT passed: filing
a run into a folder publishes the test, never the corrections filed against
it), and the same rerun_of redirect through _gated_feedback_target.

Deliberately NOT reused: GET /api/learning/runs/{workflow_id}. It holds the
right data but is gated on is_dashboard_viewer (learning_endpoints.py), so it
answers 403 to the ordinary user this route exists for.

Referenced by: src/backend/api/endpoints.py (get_run_corrections).
Depends on: src/backend/crew_ai/optimization/nl_feedback_engine.py
            (get_corrections_for_run, whose SQL is pinned in
            tests/test_optimization/test_corrections_for_a_run.py).
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.backend.auth.jwt_utils import require_user


_OWNER = {"user_id": "u-owner", "email": "owner@e.com", "org_id": "org-a",
          "role": "user"}
_STRANGER = {"user_id": "u-other", "email": "other@e.com", "org_id": "org-b",
             "role": "user"}

_ON_FILE = [
    {"hint_id": 7, "feedback_text": "wait for the spinner",
     "recorded_at": "2026-08-28T10:00:00+00:00"},
    {"hint_id": 9, "feedback_text": "the search box locator was off",
     "recorded_at": "2026-08-28T10:05:00+00:00"},
]


def _run(run_id="run-1", *, owner=_OWNER, rerun_of=None):
    return {"run_id": run_id, "user_id": owner["user_id"],
            "org_id": owner["org_id"], "rerun_of": rerun_of}


def _client(rows, *, caller=_OWNER, runs=None, loop_present=True,
            engine_present=True):
    """The two feedback routes with the registry and learning store stubbed.

    `runs` maps run_id -> the history row the registry answers with; anything
    absent resolves to None, which is how an unknown run reaches the gate.
    """
    registry = MagicMock()
    registry.get_run.side_effect = lambda rid, *a, **k: (runs or {}).get(rid)

    engine = MagicMock()
    engine.get_corrections_for_run.return_value = rows

    loop = MagicMock()
    loop.nl_engine = engine if engine_present else None
    # POST-side answer, so the gate-parity test can exercise both routes.
    loop.process_user_feedback.return_value = {
        "category": "locator", "outcome": "processed"}

    from src.backend.api.endpoints import router
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: caller

    patchers = [
        patch("src.backend.api.endpoints.get_run_registry", return_value=registry),
        patch("src.backend.api.endpoints.is_validated_admin", return_value=False),
        patch("src.backend.api.endpoints.get_feedback_loop",
              return_value=loop if loop_present else None),
    ]
    for p in patchers:
        p.start()
    client = TestClient(app)
    client._patchers, client._engine, client._loop = patchers, engine, loop
    return client


@pytest.fixture
def owned():
    """The ordinary case: the owner asking about their own finished run."""
    c = _client(_ON_FILE, runs={"run-1": _run()})
    yield c
    for p in c._patchers:
        p.stop()
    c.close()


class TestWhatTheOwnerSees:
    def test_the_corrections_on_file_come_back(self, owned):
        body = owned.get("/api/feedback/run-1").json()

        assert body["status"] == "success"
        assert body["applied_to"] == "run-1"
        assert [c["feedback_text"] for c in body["corrections"]] == [
            "wait for the spinner", "the search box locator was off"]
        owned._engine.get_corrections_for_run.assert_called_once_with("run-1")

    def test_a_run_that_contributed_nothing_is_an_empty_list(self):
        """The store lands after the run finishes, so the panel's first fetch
        is normally empty. That is not an error and must never render as one."""
        c = _client([], runs={"run-1": _run()})
        try:
            resp = c.get("/api/feedback/run-1")
        finally:
            for p in c._patchers:
                p.stop()
            c.close()

        assert resp.status_code == 200
        assert resp.json()["corrections"] == []
        assert resp.json()["status"] == "success"


class TestTheGate:
    def test_another_org_is_refused_and_the_store_is_never_read(self):
        c = _client(_ON_FILE, caller=_STRANGER, runs={"run-1": _run()})
        try:
            resp = c.get("/api/feedback/run-1")
        finally:
            for p in c._patchers:
                p.stop()
            c.close()

        assert resp.status_code == 403
        assert not c._engine.get_corrections_for_run.called, (
            "another org's correction text was read before the refusal")

    def test_an_unknown_run_is_refused_for_an_ordinary_caller(self):
        """Fails closed exactly as the POST does: an unresolvable run arrives
        at the gate as owner_id=None/org_id=None and is refused there."""
        c = _client(_ON_FILE, runs={})
        try:
            resp = c.get("/api/feedback/run-ghost")
        finally:
            for p in c._patchers:
                p.stop()
            c.close()

        assert resp.status_code == 403
        assert not c._engine.get_corrections_for_run.called

    def test_a_rerun_shows_the_corrections_of_its_original(self):
        """A re-run owns no learning record — its execution skipped learning —
        so the corrections it can show are the ORIGINAL's, which is the same
        run POST /api/feedback mutates."""
        c = _client(_ON_FILE, runs={
            "copy-1": _run("copy-1", rerun_of="run-1"),
            "run-1": _run(),
        })
        try:
            body = c.get("/api/feedback/copy-1").json()
        finally:
            for p in c._patchers:
                p.stop()
            c.close()

        assert body["applied_to"] == "run-1"
        c._engine.get_corrections_for_run.assert_called_once_with("run-1")

    def test_the_read_gate_agrees_with_the_write_gate(self):
        """Anti-drift. The two routes carry separate copies of the gate (the
        read is not worth a refactor of the shipped POST), so a caller either
        clears both or neither. If this ever splits, one of them is wrong."""
        c = _client(_ON_FILE, caller=_STRANGER, runs={"run-1": _run()})
        try:
            read = c.get("/api/feedback/run-1")
            write = c.post("/api/feedback", json={
                "workflow_id": "run-1", "feedback_text": "x",
                "feedback_type": "completely_wrong"})
        finally:
            for p in c._patchers:
                p.stop()
            c.close()

        assert read.status_code == write.status_code == 403


class TestItDegradesQuietly:
    """The panel fetches this on mount for every finished run. Anything other
    than a body it can read turns a successful run into a broken page."""

    def test_learning_switched_off_answers_the_same_way_the_post_does(self):
        c = _client(_ON_FILE, runs={"run-1": _run()}, loop_present=False)
        try:
            body = c.get("/api/feedback/run-1").json()
        finally:
            for p in c._patchers:
                p.stop()
            c.close()

        assert body["status"] == "disabled"
        assert body["message"] == "Learning system is not enabled"
        assert body["corrections"] == []

    def test_a_missing_nl_engine_is_an_empty_list_not_a_500(self):
        """nl_engine is None when its construction failed (non-blocking, by
        design). Silence is honest here — the panel adds a note when there is
        something on file and renders today's plain form when there is not."""
        c = _client(_ON_FILE, runs={"run-1": _run()}, engine_present=False)
        try:
            resp = c.get("/api/feedback/run-1")
        finally:
            for p in c._patchers:
                p.stop()
            c.close()

        assert resp.status_code == 200
        assert resp.json()["corrections"] == []

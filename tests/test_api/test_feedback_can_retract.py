"""T7 — GET /api/feedback/{run_id}: can_retract per correction.

Reuses hint_mutation_verdict (auth/ownership.py), the same three-tier rule
that already gates the five hint-mutation routes in learning_endpoints.py:
platform admin anywhere, org admin in their own org, the hint's own author.
This endpoint is the ONE place a plain org member ever finds out whether they
may act on a hint they wrote — list_hints/get_hint stay gated on
is_dashboard_viewer (a deliberate non-change; widening those would open a
cross-user visibility surface the permission design relies on not existing).

The three columns the predicate needs (org_id, created_by_user_id, is_active)
travel from the engine to this handler only. They must never reach the
response body, so one test pins the exact key set of a correction item — an
explicit projection built field-by-field, not the row dict with keys
deleted, so a column added to that SELECT later cannot leak silently.

can_retract is an OFFER of an action, not just a permission check.
hint_mutation_verdict answers who may act on a hint; it says nothing about
whether the hint is still active, and get_corrections_for_run is
deliberately unfiltered on is_active (the user's own words stay on file
after a retract). Without ANDing in is_active, a reload after a successful
retract would offer a control that can only ever no-op
(`{"changed": false, "note": "hint was already retracted"}`).
TestCanRetractReflectsHintState proves this through the real sequence —
GET, retract, GET again — because a seeded is_active=0 row proves only the
projection's boolean logic, not that the real SELECT carries the column or
that retract_hint's UPDATE is what the next read actually sees.

Kept as its own file rather than folding into
test_feedback_recorded_corrections.py, which already covers this endpoint's
read GATE (caller_can_access, the rerun redirect, the degrade-to-empty-list
behaviour) and needed no changes for this task. This file is only about the
can_retract PROJECTION, so every caller here also owns the run being read.

Referenced by: src/backend/api/endpoints.py (get_run_corrections).
Depends on: src/backend/auth/ownership.py (hint_mutation_verdict).
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.backend.auth.jwt_utils import require_user


_ORG = "org-a"

# Plain member, author of the seeded correction below.
_AUTHOR = {"user_id": "u-author", "email": "author@e.com", "org_id": _ORG,
           "org_role": None, "role": "user"}
# Plain member, same org, did NOT write the seeded correction.
_TEAMMATE = {"user_id": "u-teammate", "email": "teammate@e.com", "org_id": _ORG,
             "org_role": None, "role": "user"}
# Org admin, same org, did NOT write the seeded correction either.
_ORG_ADMIN = {"user_id": "u-admin", "email": "admin@e.com", "org_id": _ORG,
              "org_role": "org_admin", "role": "user"}
# Platform admin — the token CLAIMS admin; is_validated_admin's DB
# re-validation is satisfied by the autouse _auth_not_enforced fixture's
# blanket "any uid looks like an active admin" stub (conftest.py), which
# only matters once the token claim itself says "admin" (is_validated_admin
# checks the claim FIRST and short-circuits False before ever touching the
# DB stub for a non-admin-claiming caller, which is why _AUTHOR/_TEAMMATE/
# _ORG_ADMIN above need no such patch despite sharing that fixture).
_PLATFORM_ADMIN = {"user_id": "u-platform", "email": "platform@e.com",
                    "org_id": _ORG, "org_role": None, "role": "admin"}


def _row(author_id="u-author", is_active=1):
    return {"hint_id": 7, "feedback_text": "wait for the spinner",
            "recorded_at": "2026-08-28T10:00:00+00:00",
            "org_id": _ORG, "created_by_user_id": author_id,
            "is_active": is_active}


def _run(run_id, owner):
    """The caller always owns the run under test — this file exercises the
    can_retract projection, not the read gate (already covered by
    test_feedback_recorded_corrections.py)."""
    return {"run_id": run_id, "user_id": owner["user_id"],
            "org_id": owner["org_id"], "rerun_of": None}


def _client(rows, *, caller, run_id="run-1"):
    registry = MagicMock()
    registry.get_run.side_effect = (
        lambda rid, *a, **k: _run(run_id, caller) if rid == run_id else None)

    engine = MagicMock()
    engine.get_corrections_for_run.return_value = rows

    loop = MagicMock()
    loop.nl_engine = engine

    from src.backend.api.endpoints import router
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: caller

    patchers = [
        patch("src.backend.api.endpoints.get_run_registry", return_value=registry),
        patch("src.backend.api.endpoints.is_validated_admin", return_value=False),
        patch("src.backend.api.endpoints.get_feedback_loop", return_value=loop),
    ]
    for p in patchers:
        p.start()
    client = TestClient(app)
    client._patchers = patchers
    return client


def _get(rows, caller, run_id="run-1"):
    c = _client(rows, caller=caller, run_id=run_id)
    try:
        resp = c.get(f"/api/feedback/{run_id}")
    finally:
        for p in c._patchers:
            p.stop()
        c.close()
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestCanRetract:
    def test_the_author_can_retract_their_own_correction(self):
        body = _get([_row(author_id=_AUTHOR["user_id"])], caller=_AUTHOR)

        assert body["corrections"][0]["can_retract"] is True

    def test_a_plain_member_cannot_retract_someone_elses_correction(self):
        """Same org, but _TEAMMATE did not write it."""
        body = _get([_row(author_id=_AUTHOR["user_id"])], caller=_TEAMMATE)

        assert body["corrections"][0]["can_retract"] is False

    def test_an_org_admin_can_retract_a_correction_someone_else_wrote(self):
        body = _get([_row(author_id=_AUTHOR["user_id"])], caller=_ORG_ADMIN)

        assert body["corrections"][0]["can_retract"] is True

    def test_the_raw_permission_columns_never_reach_the_client(self):
        """org_id, created_by_user_id and is_active feed the can_retract
        computation but are not part of the client's contract."""
        body = _get([_row(author_id=_AUTHOR["user_id"])], caller=_AUTHOR)

        assert set(body["corrections"][0].keys()) == {
            "hint_id", "feedback_text", "recorded_at", "can_retract"}

    def test_a_token_less_caller_is_offered_nothing(self):
        """Minor 9. AUTH_ENFORCED off resolves an anonymous request to None,
        and hint_mutation_verdict(None, ...) answers "allow" — the permissive
        dev escape hatch every predicate in that module has. So the panel
        drew a Retract control that POST /hints/{id}/retract then 401s,
        because _require_caller refuses a token-less caller outright.

        The comment justifying the is_active term says an offered action
        "must not be one the server already knows will no-op". This one is
        worse than a no-op: it is one the server already knows it will
        refuse."""
        body = _get([_row(author_id=_AUTHOR["user_id"])], caller=None)

        assert body["corrections"][0]["can_retract"] is False

    def test_an_already_retracted_hint_is_not_offered_to_its_own_author(self):
        """can_retract must mean "retracting this would do something", not
        just "you have the tier for it" — a caller who passes
        hint_mutation_verdict on an inactive hint would otherwise be offered
        a control that can only ever answer "already retracted"."""
        body = _get(
            [_row(author_id=_AUTHOR["user_id"], is_active=0)], caller=_AUTHOR)

        assert body["corrections"][0]["can_retract"] is False


class TestCanRetractReflectsHintState:
    """The reload case: GET, retract through the real endpoint, GET again.

    Real Postgres via api_pg_em (isolated schema, truncated per test) rather
    than a mocked engine — the property under test is that retract_hint's
    UPDATE actually reaches the row get_corrections_for_run's SELECT reads
    back, which a mocked engine cannot prove (it would only prove the
    projection's boolean logic, already covered above).

    Mounts BOTH routers this feature spans, on one app, against the same
    schema: src.backend.api.endpoints (GET /api/feedback/{run_id}) and
    src.backend.api.learning_endpoints (POST /hints/{id}/retract). Neither
    route needs require_admin (only require_user + is_validated_admin), and
    is_validated_admin short-circuits False on a non-admin-claiming token
    before ever touching the DB — see the comment on _PLATFORM_ADMIN above —
    so no real JWT / create_access_token plumbing is needed here, only a
    swapped require_user override per call.
    """

    _WORKFLOW_ID = "wf-t7-reload-1"

    @pytest.fixture
    def seeded(self, api_pg_em):
        """One hint, written through the real learn_from_feedback path (so
        its hint_evidence claim row is exactly what production writes, not
        hand-crafted SQL) — mirrors tests/test_optimization/
        test_corrections_for_a_run.py's own fixture-free pattern. Yields
        (client, app, hint_id)."""
        import threading
        from datetime import datetime, timezone

        from src.backend.api.endpoints import router as feedback_router
        from src.backend.api.learning_endpoints import router as learning_router
        from src.backend.crew_ai.optimization import pg_compat
        from src.backend.crew_ai.optimization.execution_memory import ExecutionRecord
        from src.backend.crew_ai.optimization.learning_config import WRITER_THREAD_NAME
        from src.backend.crew_ai.optimization.nl_feedback_engine import NLFeedbackEngine

        em, dsn = api_pg_em
        engine = NLFeedbackEngine(em)
        record = ExecutionRecord(
            workflow_id=self._WORKFLOW_ID, timestamp=datetime.now(timezone.utc),
            user_query="verify the dashboard loads", url="https://shop.test/dash",
            domain="shop.test", test_status="failed", org_id=_ORG,
        )
        # learn_from_feedback asserts it runs on the LearningWriteQueue
        # worker thread (defense-in-depth against a caller that bypasses the
        # queue). tests/test_optimization/conftest.py renames the pytest
        # thread for its whole directory via an autouse fixture; this
        # package has no such fixture, so the rename is local to this one
        # call — same technique, scoped to where it's actually needed.
        old_thread_name = threading.current_thread().name
        threading.current_thread().name = WRITER_THREAD_NAME
        try:
            engine.learn_from_feedback(record, {
                "category": "keyword", "feedback_text": "wait for the spinner",
                "actor": _AUTHOR["email"], "actor_user_id": _AUTHOR["user_id"],
            })
        finally:
            threading.current_thread().name = old_thread_name
        hint_id = engine.get_corrections_for_run(self._WORKFLOW_ID)[0]["hint_id"]

        registry = MagicMock()
        registry.get_run.return_value = {
            "run_id": self._WORKFLOW_ID, "user_id": _AUTHOR["user_id"],
            "org_id": _ORG, "rerun_of": None,
        }
        # fb only needs to be truthy — both routes' own dependency (
        # _require_feedback_loop / the bare loop-presence check in
        # get_run_corrections) is a 503 gate on absence, nothing else. The
        # real DB work runs through `engine` (GET) and _admin_conn (POST),
        # not through this object.
        loop = MagicMock()
        loop.nl_engine = engine

        app = FastAPI()
        app.include_router(feedback_router)
        app.include_router(learning_router, prefix="/api/learning")

        patchers = [
            patch("src.backend.api.endpoints.get_run_registry", return_value=registry),
            patch("src.backend.api.endpoints.get_feedback_loop", return_value=loop),
            patch("src.backend.api.learning_endpoints.get_feedback_loop", return_value=loop),
            patch("src.backend.api.learning_endpoints._admin_conn",
                  side_effect=lambda: pg_compat.connect(dsn)),
        ]
        for p in patchers:
            p.start()
        client = TestClient(app)
        try:
            yield client, app, hint_id
        finally:
            for p in patchers:
                p.stop()
            client.close()

    @staticmethod
    def _get_as(client, app, caller):
        app.dependency_overrides[require_user] = lambda: caller
        resp = client.get(f"/api/feedback/{TestCanRetractReflectsHintState._WORKFLOW_ID}")
        assert resp.status_code == 200, resp.text
        return resp.json()

    def test_reload_after_retract_stops_offering_the_control(self, seeded):
        client, app, hint_id = seeded

        before = self._get_as(client, app, _AUTHOR)
        assert before["corrections"][0]["can_retract"] is True, (
            "sanity: the seeded hint starts active and the author may act on it")

        app.dependency_overrides[require_user] = lambda: _AUTHOR
        retract_resp = client.post(
            f"/api/learning/hints/{hint_id}/retract",
            json={"actor": "test-author", "reason": "no longer needed"},
        )
        assert retract_resp.status_code == 200, retract_resp.text
        assert retract_resp.json()["changed"] is True, (
            "the retract must have actually flipped the row, not no-op'd")

        # The reload case: a FRESH GET, not the same response re-read.
        after_author = self._get_as(client, app, _AUTHOR)
        item = after_author["corrections"][0]
        assert item["can_retract"] is False, (
            "a retracted hint must not still offer Retract to its own "
            "author on the next page load — this is the defect being fixed")
        assert item["feedback_text"] == "wait for the spinner", (
            "the row must still be RETURNED — get_corrections_for_run stays "
            "unfiltered on is_active, so a retracted hint is still the "
            "user's own recorded words, not hidden")
        assert set(item.keys()) == {
            "hint_id", "feedback_text", "recorded_at", "can_retract"}, (
            "is_active must not have joined the response body")

        # Property of the HINT's state, not the caller's tier: an org admin
        # and a platform admin, who would otherwise pass
        # hint_mutation_verdict on this hint, get the same False.
        after_org_admin = self._get_as(client, app, _ORG_ADMIN)
        assert after_org_admin["corrections"][0]["can_retract"] is False, (
            "false because the hint is inactive, not because of the org "
            "admin's own tier (which alone would pass)")

        after_platform_admin = self._get_as(client, app, _PLATFORM_ADMIN)
        assert after_platform_admin["corrections"][0]["can_retract"] is False, (
            "false because the hint is inactive, not because of the "
            "platform admin's own tier (which alone would pass)")

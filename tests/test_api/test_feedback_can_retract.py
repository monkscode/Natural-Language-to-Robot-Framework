"""T7 — GET /api/feedback/{run_id}: can_retract per correction.

Reuses hint_mutation_verdict (auth/ownership.py), the same three-tier rule
that already gates the five hint-mutation routes in learning_endpoints.py:
platform admin anywhere, org admin in their own org, the hint's own author.
This endpoint is the ONE place a plain org member ever finds out whether they
may act on a hint they wrote — list_hints/get_hint stay gated on
is_dashboard_viewer (a deliberate non-change; widening those would open a
cross-user visibility surface the permission design relies on not existing).

The two columns the predicate needs (org_id, created_by_user_id) travel from
the engine to this handler only. They must never reach the response body, so
one test pins the exact key set of a correction item — an explicit
projection built field-by-field, not the row dict with keys deleted, so a
column added to that SELECT later cannot leak silently.

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


def _row(author_id="u-author"):
    return {"hint_id": 7, "feedback_text": "wait for the spinner",
            "recorded_at": "2026-08-28T10:00:00+00:00",
            "org_id": _ORG, "created_by_user_id": author_id}


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
        """org_id and created_by_user_id feed hint_mutation_verdict but are
        not part of the client's contract."""
        body = _get([_row(author_id=_AUTHOR["user_id"])], caller=_AUTHOR)

        assert set(body["corrections"][0].keys()) == {
            "hint_id", "feedback_text", "recorded_at", "can_retract"}

"""The drawer must not offer a feedback read the server will refuse (R5).

`GET /api/history/{run_id}` passes `is_grouped` to caller_can_access;
`GET /api/feedback/{run_id}` deliberately does not, because publishing a test
into a folder publishes the test and never the corrections filed against it.
So a peer opening a colleague's published run gets 200 from the first and 403
from the second — by design on both sides, and P1 made it reachable on every
peer row rather than almost never.

The SPA cannot decide this for itself: a platform admin and the token-less dev
caller may both read a peer's feedback, and the client knows which it is for
neither. So the detail route answers it, with the SAME gate the feedback route
applies — caller_can_access WITHOUT is_grouped on the submitted row, then
_gated_feedback_target's second pass on the ORIGINAL when the row is a re-run.

The agreement is what makes the flag worth anything, so it is asserted by
driving the SAME caller through BOTH routes rather than by re-reading the
gate. The feedback route's own authorization is untouched and stays the
backstop: this flag suppresses an offered request, it does not replace a
refusal.

Referenced by: src/backend/api/history_endpoints.py (run_detail),
               src/backend/api/endpoints.py (get_run_corrections).
Depends on: tests/test_auth/conftest.py (auth_isolated_schema),
            tests/test_api/test_groups.py (_auth, _register, _seed_run_for,
            _team_of_two).
"""

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


from tests.test_api.test_groups import (  # noqa: E402
    _auth, _register, _seed_run_for, _team_of_two,
)


def _seed_rerun_for(tok, original, query="a re-run") -> str:
    """A run owned by the token's user and linked to `original`, written the
    way _rerun_from_history writes one."""
    from src.backend.auth.jwt_utils import decode_token
    from src.backend.core.run_registry import get_run_registry

    claims = decode_token(tok)
    rid = str(uuid.uuid4())
    get_run_registry().record_start(
        rid,
        {"user_id": claims["user_id"], "email": claims["email"],
         "org_id": claims["org_id"]},
        query, "passed", rerun_of=original,
    )
    return rid


def _flag(client, tok, run_id):
    r = client.get(f"/api/history/{run_id}", headers=_auth(tok))
    assert r.status_code == 200, r.text
    return r.json()["can_read_feedback"]


def _feedback_status(client, tok, run_id):
    """The status GET /api/feedback/{id} really answers for this caller.

    The learning loop is stubbed away so the route stops at its gate: with no
    loop it returns 200 'disabled', which is every bit as much a NOT-403 as a
    real read would be, and it keeps this test off the learning store."""
    with patch("src.backend.api.endpoints.get_feedback_loop",
               return_value=None):
        return client.get(f"/api/feedback/{run_id}",
                          headers=_auth(tok)).status_code


def _published_peer_run(client):
    """A's run, filed into a team folder, so B can open it but not its
    corrections. Returns (tok_b, run_id)."""
    tok_a, tok_b, _org = _team_of_two(client)
    rid = _seed_run_for(client, tok_a, "a colleague's test")
    gid = client.post("/api/groups", json={"name": f"F{uuid.uuid4().hex[:6]}"},
                      headers=_auth(tok_a)).json()["group_id"]
    moved = client.put("/api/groups/assignments",
                       json={"run_ids": [rid], "group_id": gid},
                       headers=_auth(tok_a))
    assert moved.status_code == 200, moved.text
    return tok_a, tok_b, rid


class TestTheFlagMatchesTheFeedbackRoute:
    def test_the_owner_of_a_run_may_read_its_feedback(self, client):
        tok = _register(client, f"cf-{uuid.uuid4().hex[:8]}@e.com")
        rid = _seed_run_for(client, tok, "my own test")

        assert _flag(client, tok, rid) is True
        assert _feedback_status(client, tok, rid) != 403

    def test_a_peer_opens_the_published_run_and_is_told_not_to_ask(
            self, client):
        # The case P1 made ordinary: the drawer 200s, the corrections read
        # 403s, and the flag is what stops the SPA firing it at all.
        _tok_a, tok_b, rid = _published_peer_run(client)

        assert _flag(client, tok_b, rid) is False
        assert _feedback_status(client, tok_b, rid) == 403

    def test_a_platform_admin_may_read_a_peers_feedback(self, client):
        _tok_a, tok_b, rid = _published_peer_run(client)
        # Same caller, same run — only the platform-admin re-validation
        # differs, and it flips BOTH answers together.
        with patch("src.backend.api.history_scope.is_validated_admin",
                   return_value=True), \
             patch("src.backend.api.endpoints.is_validated_admin",
                   return_value=True):
            assert _flag(client, tok_b, rid) is True
            assert _feedback_status(client, tok_b, rid) != 403

    def test_a_re_run_whose_original_is_out_of_reach_is_false(self, client):
        # The submitted row is the caller's OWN, so the first gate passes;
        # _gated_feedback_target then refuses on the original, which is what
        # the second half of this flag exists to predict.
        tok_a, tok_b, original = _published_peer_run(client)
        rerun = _seed_rerun_for(tok_b, original)

        assert _flag(client, tok_b, rerun) is False
        assert _feedback_status(client, tok_b, rerun) == 403
        # ...and the row itself is still openable, so this is not the detail
        # route's own gate answering.
        assert client.get(f"/api/history/{rerun}",
                          headers=_auth(tok_b)).status_code == 200

    def test_a_re_run_of_ones_own_run_stays_true(self, client):
        tok = _register(client, f"cf-{uuid.uuid4().hex[:8]}@e.com")
        original = _seed_run_for(client, tok, "my own test")
        rerun = _seed_rerun_for(tok, original)

        assert _flag(client, tok, rerun) is True
        assert _feedback_status(client, tok, rerun) != 403

    def test_a_re_run_pointing_at_a_run_that_does_not_resolve_is_false(
            self, client):
        # Nothing in src/backend ever deletes a run, so this is an anomaly
        # rather than ordinary state — and both sides fail closed on it:
        # _gated_feedback_target substitutes an empty dict, and an id absent
        # from the owners mapping arrives here as all-None.
        tok = _register(client, f"cf-{uuid.uuid4().hex[:8]}@e.com")
        rerun = _seed_rerun_for(tok, str(uuid.uuid4()))

        assert _flag(client, tok, rerun) is False
        assert _feedback_status(client, tok, rerun) == 403

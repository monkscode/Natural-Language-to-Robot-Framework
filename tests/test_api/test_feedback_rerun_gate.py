"""Feedback routed through a re-run must clear the gate on the ORIGINAL run.

A re-run row owns no learning record — its execution deliberately skipped
learning — so /api/feedback redirects the mutation to `rerun_of`. Until now
only the SUBMITTED row was gated, on the premise that whoever fired the re-run
had already been authorized against the original. Opening the re-run of a
published run to the whole org (D5) broke that premise twice over:

  * within-org: a plain member re-runs a colleague's grouped run and then
    feeds back on their OWN copy, rewriting the colleague's learning record.
  * cross-org: a platform admin seated in org P re-runs org A's published
    run; org P's own org_admin then feeds back on the copy and reaches org
    A's hint store, because conflict detection fires with the TARGET
    record's org (feedback_loop.detect_conflict), never the caller's.

Both are gated here on the original run, with `is_grouped` deliberately NOT
passed (D7): publication opens reading and re-running a test, never the
rewriting of the hints every future generation in the org receives.

The lookup of the original is UNSCOPED on purpose — a platform admin re-runs
across orgs legitimately, and passing the caller's org scope would refuse the
one caller who is allowed.

Referenced by: src/backend/api/endpoints.py (submit_feedback).
Depends on: tests/test_auth/conftest.py (auth_isolated_schema),
            tests/test_api/test_groups.py (_auth, _login, _register,
            _seed_run_for, _team_of_two).
"""

import uuid
from unittest.mock import MagicMock, patch

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from src.backend.main import app
    return TestClient(app)


from tests.test_api.test_groups import (  # noqa: E402
    _auth, _login, _register, _seed_run_for, _team_of_two,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_rerun(owner_claims: dict, original_id: str,
                group_id: str | None = None) -> str:
    """Write the history row a re-run produces, exactly as the server does.

    _rerun_from_history hands stream_execute_only rerun_of=<root-flattened
    anchor> and group_id=<source's folder>; stream_execute_only passes both
    straight to record_start with the RE-RUNNER as owner. Reproducing that
    write keeps the exploit faithful without paying for a Docker execution.
    """
    from src.backend.core.run_registry import get_run_registry

    rid = str(uuid.uuid4())
    get_run_registry().record_start(
        rid,
        {"user_id": owner_claims["user_id"], "email": owner_claims["email"],
         "org_id": owner_claims["org_id"]},
        "cloned run",
        "passed",
        robot_code="*** Test Cases ***\nX\n    Log    hi\n",
        rerun_of=original_id,
        group_id=group_id,
    )
    return rid


def _store_code(owner_claims: dict, run_id: str) -> None:
    """Give a seeded run stored code so _rerun_from_history can clone it.

    _seed_run_for writes no robot_code, and the re-run path answers 409 for a
    run it cannot resolve code for — which would fail the cross-org test
    before its gate ever ran.
    """
    from src.backend.core.run_registry import get_run_registry

    get_run_registry().record_start(
        run_id,
        {"user_id": owner_claims["user_id"], "email": owner_claims["email"],
         "org_id": owner_claims["org_id"]},
        None,
        "passed",
        robot_code="*** Test Cases ***\nX\n    Log    hi\n",
    )


def _feedback(client, tok, run_id):
    """POST /api/feedback with the learning loop replaced by a spy.

    Returns (response, fake_loop, lookups) where lookups is the ordered list of
    (run_id, org_id, identified) tuples the handler asked the registry for. A
    200 proves nothing here — the handler answers 200 with status="disabled"
    when learning is off — so every assertion reads the spy instead.
    """
    from src.backend.core.run_registry import get_run_registry

    fake = MagicMock()
    fake.process_user_feedback.return_value = {
        "action": "recorded", "outcome": "processed",
    }

    reg = get_run_registry()
    real_get_run = reg.get_run
    lookups: list[tuple] = []

    def _spy(run_id_, org_id=None, *, identified=False):
        lookups.append((run_id_, org_id, identified))
        return real_get_run(run_id_, org_id, identified=identified)

    with patch("src.backend.api.endpoints.get_feedback_loop", return_value=fake), \
         patch.object(reg, "get_run", side_effect=_spy):
        resp = client.post(
            "/api/feedback",
            json={"workflow_id": run_id, "feedback_text": "wrong locator",
                  "feedback_type": "completely_wrong"},
            headers=_auth(tok),
        )
    return resp, fake, lookups


def _target(fake):
    """The run id the learning store was actually told to mutate."""
    assert fake.process_user_feedback.called, "the learning store was never called"
    return fake.process_user_feedback.call_args.args[0]


@pytest.fixture
def published(client):
    """Org with A (org_admin) and B (member); A's run is filed into a folder."""
    from src.backend.auth.jwt_utils import decode_token

    tok_a, tok_b, org_id = _team_of_two(client)
    original = _seed_run_for(client, tok_a, "A published checkout test")
    gid = client.post("/api/groups", json={"name": f"Shared {uuid.uuid4().hex[:6]}"},
                      headers=_auth(tok_a)).json()["group_id"]
    r = client.put("/api/groups/assignments",
                   json={"run_ids": [original], "group_id": gid}, headers=_auth(tok_a))
    assert r.status_code == 200, r.text
    return {"tok_a": tok_a, "tok_b": tok_b, "org_id": org_id, "gid": gid,
            "original": original, "claims_a": decode_token(tok_a),
            "claims_b": decode_token(tok_b)}


# ---------------------------------------------------------------------------
# The two bypasses
# ---------------------------------------------------------------------------

def test_a_member_cannot_reach_a_colleagues_learning_record_via_a_rerun(client, published):
    """Within-org. B may re-run A's published run (D5) but the copy must not
    become a side door into A's learning record (D7)."""
    copy = _seed_rerun(published["claims_b"], published["original"], published["gid"])

    resp, fake, _ = _feedback(client, published["tok_b"], copy)

    assert resp.status_code == 403, resp.text
    assert not fake.process_user_feedback.called, (
        "a plain member rewrote a colleague's learning record through a re-run"
    )


def test_an_org_admin_cannot_reach_another_orgs_learning_record_via_an_admin_rerun(client):
    """Cross-org. A platform admin seated in org P re-runs org A's published
    run and files the copy into a P folder. P's own org_admin — no platform
    role — then feeds back on that copy. The gate on the copy passes (their
    org, and they are its admin); the gate on the ORIGINAL must not."""
    from src.backend.auth.jwt_utils import create_access_token, decode_token
    from src.backend.auth.org_repository import OrgRepository
    from src.backend.core.run_registry import get_run_registry

    # Org A: a published run nobody in org P owns.
    tok_a = _register(client, f"oa-{uuid.uuid4().hex[:8]}@e.com")
    claims_a = decode_token(tok_a)
    original = _seed_run_for(client, tok_a, "org A published test")
    _store_code(claims_a, original)
    gid_a = client.post("/api/groups", json={"name": "A folder"},
                        headers=_auth(tok_a)).json()["group_id"]
    assert client.put("/api/groups/assignments",
                      json={"run_ids": [original], "group_id": gid_a},
                      headers=_auth(tok_a)).status_code == 200

    # Org P: a platform admin (role='admin') and a plain org_admin member.
    email_p = f"pa-{uuid.uuid4().hex[:8]}@e.com"
    email_q = f"pq-{uuid.uuid4().hex[:8]}@e.com"
    uid_p = decode_token(_register(client, email_p))["user_id"]
    uid_q = decode_token(_register(client, email_q))["user_id"]
    org_p = OrgRepository().create_team_org(f"Pentagon {uuid.uuid4().hex[:6]}", uid_p)
    OrgRepository().add_member(org_p, uid_q, "org_admin")
    tok_q = _login(client, email_q)
    assert decode_token(tok_q)["role"] != "admin", "org P's admin must not be a platform admin"

    # The platform admin's token: role='admin' is what is_validated_admin reads.
    tok_p = create_access_token({
        "id": uid_p, "email": email_p, "role": "admin", "display_name": "Platform",
        "org_id": org_p, "org_role": "org_admin", "token_version": 0,
    })
    claims_p = decode_token(tok_p)

    # The platform admin really is allowed to re-run org A's run — assert the
    # re-run gate opens for them, then write the row that re-run produces.
    from src.backend.api.endpoints import _rerun_from_history
    with patch("src.backend.api.endpoints.stream_execute_only") as fake_stream:
        _rerun_from_history(original, claims_p)
    assert fake_stream.call_args.kwargs["rerun_of"] == original

    copy = _seed_rerun(claims_p, original, group_id=None)
    # ...and it lands in a P folder, as the scenario describes.
    gid_p = get_run_registry().create_group(org_p, uid_p, "P folder")["group_id"]
    assert client.put("/api/groups/assignments",
                      json={"run_ids": [copy], "group_id": gid_p},
                      headers=_auth(tok_p)).status_code == 200

    resp, fake, _ = _feedback(client, tok_q, copy)

    assert resp.status_code == 403, resp.text
    assert not fake.process_user_feedback.called, (
        f"org P's admin rewrote org A's learning record (target "
        f"{fake.process_user_feedback.call_args})"
    )
    assert claims_a["org_id"] != org_p  # the two orgs really are different


# ---------------------------------------------------------------------------
# What must keep working
# ---------------------------------------------------------------------------

def test_an_owner_still_feeds_back_through_their_own_rerun(client, published):
    copy = _seed_rerun(published["claims_a"], published["original"], published["gid"])

    resp, fake, _ = _feedback(client, published["tok_a"], copy)

    assert resp.status_code == 200, resp.text
    assert _target(fake) == published["original"]


def test_an_org_admin_still_feeds_back_through_a_members_rerun(client, published):
    """B re-runs B's OWN run; A, the org_admin, feeds back on the copy."""
    b_original = _seed_run_for(client, published["tok_b"], "B original")
    copy = _seed_rerun(published["claims_b"], b_original)

    resp, fake, _ = _feedback(client, published["tok_a"], copy)

    assert resp.status_code == 200, resp.text
    assert _target(fake) == b_original


def test_a_platform_admin_still_feeds_back_across_orgs(client, published):
    from src.backend.auth.jwt_utils import create_access_token

    copy = _seed_rerun(published["claims_b"], published["original"], published["gid"])
    tok_admin = create_access_token({
        "id": "00000000-0000-0000-0000-0000000000a1", "email": "root@test.local",
        "role": "admin", "display_name": "Root", "org_id": None,
        "org_role": None, "token_version": 0,
    })

    resp, fake, _ = _feedback(client, tok_admin, copy)

    assert resp.status_code == 200, resp.text
    assert _target(fake) == published["original"]


# ---------------------------------------------------------------------------
# Shape of the new lookup
# ---------------------------------------------------------------------------

def test_the_original_is_read_unscoped_and_only_on_the_rerun_path(client, published):
    """One extra PK read, unscoped, and only when rerun_of is set.

    Unscoped is load-bearing: the platform-admin case above resolves no folder
    in the caller's org, so a scoped read would refuse the one caller who is
    entitled to it.
    """
    plain = _seed_run_for(client, published["tok_a"], "no lineage")
    resp, fake, lookups = _feedback(client, published["tok_a"], plain)
    assert resp.status_code == 200, resp.text
    assert _target(fake) == plain
    assert lookups == [(plain, None, False)], (
        f"a run with no rerun_of paid for a second lookup: {lookups}"
    )

    copy = _seed_rerun(published["claims_a"], published["original"], published["gid"])
    resp, fake, lookups = _feedback(client, published["tok_a"], copy)
    assert resp.status_code == 200, resp.text
    assert lookups == [(copy, None, False), (published["original"], None, False)], (
        f"the original was not read exactly once, unscoped: {lookups}"
    )

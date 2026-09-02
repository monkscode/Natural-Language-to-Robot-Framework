"""Learning /hints + /runs are org-scoped; members 403; telemetry platform-only.

Task 12: org-admin can read their OWN org's hints/runs; telemetry reads
(/stats, /triggers, /health) stay platform-admin-only.

Mutations no longer do. A hint now answers to three tiers — platform admin
anywhere, org admin in their own org, author for their own hints — so the
create-tier tests below expect 201 where they once expected 403, and the
per-hint matrix lives in test_hint_mutation_tiers.py. What stays platform-only
here is the review-hints sweep: an LLM batch pass across every org, with no
single org whose admin could own it.
"""

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("learning_api_isolated")]


def test_member_cannot_list_hints(dash_client):
    r = dash_client.get(
        "/api/learning/hints",
        headers={"Authorization": f"Bearer {dash_client.member_token}"},
    )
    assert r.status_code == 403


def test_org_admin_sees_only_own_org_hints(dash_client):
    dash_client.seed_hint(org_id=dash_client.org_a, text="A-secret")
    dash_client.seed_hint(org_id=dash_client.org_b, text="B-secret")
    r = dash_client.get(
        "/api/learning/hints",
        headers={"Authorization": f"Bearer {dash_client.org_a_admin_token}"},
    )
    assert r.status_code == 200
    texts = [h["feedback_text"] for h in r.json()["hints"]]
    assert "A-secret" in texts and "B-secret" not in texts


def test_org_admin_hint_by_id_wrong_org_returns_404(dash_client):
    """By-id lookup must scope the SELECT — gate alone is not enough (Task-9 lesson).
    An org-A admin fetching org-B's hint by id must get 404, not the hint.
    """
    org_b_hint_id = dash_client.seed_hint(org_id=dash_client.org_b, text="B-by-id-secret")
    r = dash_client.get(
        f"/api/learning/hints/{org_b_hint_id}",
        headers={"Authorization": f"Bearer {dash_client.org_a_admin_token}"},
    )
    assert r.status_code == 404


def test_stats_remains_platform_admin_only(dash_client):
    r = dash_client.get(
        "/api/learning/stats",
        headers={"Authorization": f"Bearer {dash_client.org_a_admin_token}"},
    )
    assert r.status_code == 403


def _create_body(org_id, text="x"):
    return {"feedback_text": text, "anchor_query": "login as admin",
            "scope": "global", "actor": "a@e.com", "org_id": org_id}


def test_org_admin_may_create_a_hint_in_their_own_org(dash_client):
    """Hint creation is no longer platform-only. An org admin curates their
    own org's hints — that is the point of the org-admin tier."""
    r = dash_client.post(
        "/api/learning/hints", json=_create_body(dash_client.org_a),
        headers={"Authorization": f"Bearer {dash_client.org_a_admin_token}"},
    )
    assert r.status_code == 201, r.text


def test_org_admin_cannot_create_a_hint_in_another_org(dash_client):
    """403, not 404: the caller named the org themselves, so refusing tells
    them nothing they did not supply, and no hint exists whose existence
    could leak."""
    r = dash_client.post(
        "/api/learning/hints", json=_create_body(dash_client.org_b),
        headers={"Authorization": f"Bearer {dash_client.org_a_admin_token}"},
    )
    assert r.status_code == 403


def test_a_plain_member_cannot_create_a_hint_at_all(dash_client):
    """A plain org_member contributes through feedback, never through this
    route — they are not a curator of the org's hints."""
    r = dash_client.post(
        "/api/learning/hints", json=_create_body(dash_client.org_a),
        headers={"Authorization": f"Bearer {dash_client.member_token}"},
    )
    assert r.status_code == 403


def test_platform_admin_may_create_a_hint_in_any_org(dash_client):
    r = dash_client.post(
        "/api/learning/hints", json=_create_body(dash_client.org_b, "cross-org create"),
        headers={"Authorization": f"Bearer {dash_client.platform_admin_token}"},
    )
    assert r.status_code == 201, r.text


def test_org_admin_sees_only_own_org_runs(dash_client):
    """List /runs must be org-scoped: org-A admin sees their run, not org-B's."""
    wf_a = dash_client.seed_run(org_id=dash_client.org_a)
    wf_b = dash_client.seed_run(org_id=dash_client.org_b)
    r = dash_client.get(
        "/api/learning/runs",
        headers={"Authorization": f"Bearer {dash_client.org_a_admin_token}"},
    )
    assert r.status_code == 200
    workflow_ids = [run["workflow_id"] for run in r.json()["runs"]]
    assert wf_a in workflow_ids
    assert wf_b not in workflow_ids


def test_org_admin_run_by_id_wrong_org_returns_404(dash_client):
    """By-id lookup must org-scope the early 404 gate — trace/metrics must not leak.

    An org-A admin fetching an org-B run's workflow_id must get 404, not 200 with
    trace data.  with_trace=True seeds a hint_workflow_trace row for the org-B
    workflow: without the early-404 fix, ``run is None and not trace`` evaluates to
    False (trace is non-empty) and get_run returns 200 leaking org-B's trace rows.
    """
    org_b_wf = dash_client.seed_run(org_id=dash_client.org_b, with_trace=True)
    r = dash_client.get(
        f"/api/learning/runs/{org_b_wf}",
        headers={"Authorization": f"Bearer {dash_client.org_a_admin_token}"},
    )
    assert r.status_code == 404


# The per-hint mutations moved to the three-tier rule and are covered by the
# matrix below.  These three stay platform-only: they are LLM batch sweeps
# across every org, not per-hint actions, so there is no org whose admin they
# could belong to.  They share the identical Depends(require_admin) guard, and
# path/body ids need not exist — require_admin 403s before any lookup.
@pytest.mark.parametrize(
    "method, path, body",
    [
        # /promote was removed outright (cross-org sharing disabled) — its
        # 404-for-everyone behaviour is pinned by test_learning_promote.py.
        ("post", "/api/learning/review-hints/start", None),
        ("patch", "/api/learning/review-hints/sessions/1/recommendations/1",
         {"admin_decision": "approved"}),
        ("post", "/api/learning/review-hints/sessions/1/apply", None),
    ],
)
def test_remaining_mutations_platform_admin_only(dash_client, method, path, body):
    """Every cross-org curation route 403s a non-platform-admin (org-admin token)."""
    kwargs = {"headers": {"Authorization": f"Bearer {dash_client.org_a_admin_token}"}}
    if body is not None:
        kwargs["json"] = body
    r = getattr(dash_client, method)(path, **kwargs)
    assert r.status_code == 403, f"{method.upper()} {path} returned {r.status_code}, expected 403"

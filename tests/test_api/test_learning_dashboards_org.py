"""Learning /hints + /runs are org-scoped; members 403; telemetry + mutations platform-only.

Task 12: org-admin can read their OWN org's hints/runs; telemetry reads (/stats,
/triggers, /health) and all mutations stay platform-admin-only.
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


def test_mutation_still_platform_admin_only(dash_client):
    r = dash_client.post(
        "/api/learning/hints",
        json={
            "feedback_text": "x",
            "anchor_query": "login as admin",
            "scope": "global",
            "actor": "a@e.com",
        },
        headers={"Authorization": f"Bearer {dash_client.org_a_admin_token}"},
    )
    assert r.status_code == 403


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

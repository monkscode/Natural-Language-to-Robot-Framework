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

"""Who may change a hint — the same matrix applied to every hint-id route.

Before this, every hint mutation was gated on require_admin, the PLATFORM
role, and was entirely org-blind: a platform admin could retract any org's
hint and nobody else could touch their own. Three tiers replace that —
platform admin anywhere, org admin within their own org, author for their own
hints — and the refusal differs by case:

  another org's hint      -> 404, because 403 confirms it exists and get_hint
                             already 404s a cross-org read
  own org, not the author -> 403, because they may have read its text in
                             their own feedback panel

Parametrised over the routes on purpose: a sixth hint-id mutation added later
without the gate fails here rather than shipping open. _ROUTES is asserted
against the router itself, so adding one and forgetting this list also fails.

Depends on: src/backend/api/learning_endpoints.py,
            src/backend/auth/ownership.py (hint_mutation_verdict)
"""

import pytest


# (method, path suffix, body) for every route that mutates ONE hint by id.
_ROUTES = [
    ("patch", "", {"actor": "a@e.com", "scope": "global"}),
    ("post", "/unflag", {"actor": "a@e.com"}),
    ("post", "/retract", {"actor": "a@e.com"}),
    ("post", "/reactivate", {"actor": "a@e.com"}),
]


def test_the_matrix_covers_every_hint_id_mutation_route():
    """The guard on the guard. If someone adds a fifth per-hint mutation,
    this fails before the matrix below can silently not cover it."""
    from src.backend.api.learning_endpoints import router

    found = set()
    for route in router.routes:
        methods = getattr(route, "methods", set()) & {"POST", "PATCH", "PUT", "DELETE"}
        path = getattr(route, "path", "")
        if methods and path.startswith("/hints/{hint_id}"):
            for m in methods:
                found.add((m.lower(), path[len("/hints/{hint_id}"):]))
    expected = {(m, suffix) for m, suffix, _ in _ROUTES}
    assert found == expected, (
        f"hint-id mutation routes changed: {found ^ expected}. Add the new "
        f"route to _ROUTES and confirm it calls _gate_hint_mutation."
    )


def _call(dash_client, method, suffix, body, hint_id, token):
    return getattr(dash_client, method)(
        f"/api/learning/hints/{hint_id}{suffix}",
        json=body, headers={"Authorization": f"Bearer {token}"},
    )


@pytest.fixture
def authored_hint(dash_client):
    """A hint in org A, written by org A's plain member."""
    return dash_client.seed_hint(
        dash_client.org_a, "wait for the grid",
        author_user_id=dash_client.member_uid,
        author_email="member@test.local",
    )


@pytest.mark.parametrize("method, suffix, body", _ROUTES)
class TestEveryHintIdMutation:

    def test_org_admin_in_the_same_org_is_allowed(
            self, dash_client, authored_hint, method, suffix, body):
        r = _call(dash_client, method, suffix, body, authored_hint,
                  dash_client.org_a_admin_token)
        assert r.status_code == 200, r.text

    def test_the_author_is_allowed(
            self, dash_client, authored_hint, method, suffix, body):
        """A plain org_member who wrote the hint may change it — the whole
        point of the Author tier, and the only reason the SPA can offer them
        a Retract control."""
        r = _call(dash_client, method, suffix, body, authored_hint,
                  dash_client.member_token)
        assert r.status_code == 200, r.text

    def test_platform_admin_is_allowed_anywhere(
            self, dash_client, method, suffix, body):
        other = dash_client.seed_hint(dash_client.org_b, "another org's hint")
        r = _call(dash_client, method, suffix, body, other,
                  dash_client.platform_admin_token)
        assert r.status_code == 200, r.text

    def test_an_org_admin_of_another_org_gets_404_not_403(
            self, dash_client, authored_hint, method, suffix, body):
        """404 is the point: a 403 would confirm the hint exists to someone
        who cannot see it, which the read path already refuses to do."""
        r = _call(dash_client, method, suffix, body, authored_hint,
                  dash_client.org_b_admin_token)
        assert r.status_code == 404, (
            f"{method.upper()} {suffix or '(patch)'} answered {r.status_code} "
            f"to a cross-org caller — 404 or the hint's existence leaks"
        )

    def test_a_non_author_member_in_the_same_org_gets_403(
            self, dash_client, method, suffix, body):
        """403, not 404: the hint is in their org and they may have read its
        text in their own feedback panel."""
        someone_elses = dash_client.seed_hint(
            dash_client.org_a, "written by the org admin",
            author_user_id=dash_client.org_a_admin_uid,
            author_email="org-a-admin@test.local",
        )
        r = _call(dash_client, method, suffix, body, someone_elses,
                  dash_client.member_token)
        assert r.status_code == 403

    def test_an_unattributed_hint_is_not_everyones(
            self, dash_client, method, suffix, body):
        """Legacy hints carry no author. A member must not inherit them."""
        orphan = dash_client.seed_hint(dash_client.org_a, "legacy, no author")
        r = _call(dash_client, method, suffix, body, orphan,
                  dash_client.member_token)
        assert r.status_code == 403

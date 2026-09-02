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

The Author tier is NARROWER than the org tier, and only on this last axis:
an author gets `retract` and nothing else. patch can rewrite `scope` to
'global', which applies the hint to every query in the org; reactivate resets
unused_count, defeating _auto_disable_hint's never-used retirement, and can be
looped forever on one's own hint. Neither escalation was ever argued for, and
the only author surface the product draws is the feedback panel's Retract
control. The org check is unchanged on all five routes — that part the owner
ruled on; this is the separate tier axis, scoped to the surface built.

Parametrised over the routes on purpose: a fifth hint-id mutation added later
without the gate fails here rather than shipping open. _ROUTES is asserted
against the router itself, so adding one and forgetting this list also fails.

Depends on: src/backend/api/learning_endpoints.py,
            src/backend/auth/ownership.py (hint_mutation_verdict)
"""

import pytest


# (method, path suffix, body, the author may do it) for every route that
# mutates ONE hint by id. The last field is hint_mutation_verdict's opt-in
# author_tier_applies, asserted end-to-end: it is True on exactly one route.
_ROUTES = [
    ("patch", "", {"actor": "a@e.com", "scope": "global"}, False),
    ("post", "/unflag", {"actor": "a@e.com"}, False),
    ("post", "/retract", {"actor": "a@e.com"}, True),
    ("post", "/reactivate", {"actor": "a@e.com"}, False),
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
    expected = {(m, suffix) for m, suffix, _, _ in _ROUTES}
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


@pytest.mark.parametrize("method, suffix, body, author_allowed", _ROUTES)
class TestEveryHintIdMutation:

    def test_org_admin_in_the_same_org_is_allowed(
            self, dash_client, authored_hint, method, suffix, body, author_allowed):
        r = _call(dash_client, method, suffix, body, authored_hint,
                  dash_client.org_a_admin_token)
        assert r.status_code == 200, r.text

    def test_the_author_gets_retract_and_only_retract(
            self, dash_client, authored_hint, method, suffix, body, author_allowed):
        """A plain org_member who wrote the hint may RETRACT it — the reason
        the Author tier exists and the only reason the SPA can offer them a
        Retract control. Everything else is 403: they are still a plain
        member of the org, and patch/unflag/reactivate are curation."""
        r = _call(dash_client, method, suffix, body, authored_hint,
                  dash_client.member_token)
        assert r.status_code == (200 if author_allowed else 403), r.text

    def test_platform_admin_is_allowed_anywhere(
            self, dash_client, method, suffix, body, author_allowed):
        other = dash_client.seed_hint(dash_client.org_b, "another org's hint")
        r = _call(dash_client, method, suffix, body, other,
                  dash_client.platform_admin_token)
        assert r.status_code == 200, r.text

    def test_an_org_admin_of_another_org_gets_404_not_403(
            self, dash_client, authored_hint, method, suffix, body, author_allowed):
        """404 is the point: a 403 would confirm the hint exists to someone
        who cannot see it, which the read path already refuses to do."""
        r = _call(dash_client, method, suffix, body, authored_hint,
                  dash_client.org_b_admin_token)
        assert r.status_code == 404, (
            f"{method.upper()} {suffix or '(patch)'} answered {r.status_code} "
            f"to a cross-org caller — 404 or the hint's existence leaks"
        )

    def test_a_non_author_member_in_the_same_org_gets_403(
            self, dash_client, method, suffix, body, author_allowed):
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
            self, dash_client, method, suffix, body, author_allowed):
        """Legacy hints carry no author. A member must not inherit them."""
        orphan = dash_client.seed_hint(dash_client.org_a, "legacy, no author")
        r = _call(dash_client, method, suffix, body, orphan,
                  dash_client.member_token)
        assert r.status_code == 403

    def test_a_token_less_caller_is_refused(
            self, dash_client, authored_hint, method, suffix, body, author_allowed):
        """401, with no token at all. AUTH_ENFORCED is off in this fixture,
        so require_user resolves an anonymous request to None instead of
        raising — the same dev escape hatch dashboard_scope.py opts out of.
        A hint mutation has no legitimate anonymous case, so it opts out too:
        before this, `AUTH_ENFORCED=false ./run.sh bench` left port 5000
        able to retract every hint in every org."""
        r = getattr(dash_client, method)(
            f"/api/learning/hints/{authored_hint}{suffix}", json=body,
        )
        assert r.status_code == 401, (
            f"{method.upper()} /hints/{{id}}{suffix} answered {r.status_code} "
            f"to a caller with no Authorization header"
        )

    def test_a_token_less_caller_cannot_tell_a_hint_exists(
            self, dash_client, method, suffix, body, author_allowed):
        """The refusal must not depend on whether the hint is there. If the
        401 sat behind the row lookup, an anonymous caller would read 404 for
        an absent hint and 401 for a present one — an existence oracle handed
        to someone who is not even authenticated, which is the leak the
        404/403 split above exists to prevent."""
        r = getattr(dash_client, method)(
            f"/api/learning/hints/999999{suffix}", json=body,
        )
        assert r.status_code == 401, (
            f"{method.upper()} /hints/{{id}}{suffix} answered {r.status_code} "
            f"for an ABSENT hint but 401 for a present one — existence leak"
        )


_CRITIQUE = "the LLM thinks this contradicts hint 7"


def _flag(dash_client, hint_id: int) -> None:
    """Stamp the three conflict-detection columns on a hint."""
    from src.backend.crew_ai.optimization import pg_compat

    conn = pg_compat.connect(dash_client._dsn)
    try:
        conn.execute(
            "UPDATE nl_feedback_corrections SET conflict_flagged = 1, "
            "conflict_flagged_at = '2026-09-02T00:00:00+00:00', "
            "conflict_flag_reason = ? WHERE id = ?",
            (_CRITIQUE, hint_id),
        )
        conn.commit()
    finally:
        conn.close()


_CONFLICT_KEYS = ("conflict_flagged", "conflict_flagged_at", "conflict_flag_reason")


def test_a_plain_member_is_not_shown_the_llms_critique_of_their_hint(
        dash_client, authored_hint):
    """Retract is the one mutation a plain org member can reach, so it is the
    one response that can hand them the conflict-detection LLM's verdict on
    their own words. get_corrections_for_run states the product decision for
    the sibling read: "What the LLM thought of it is not exposed here; that is
    a product decision, not a UI one." """
    _flag(dash_client, authored_hint)
    r = _call(dash_client, "post", "/retract", {"actor": "a@e.com"},
              authored_hint, dash_client.member_token)
    assert r.status_code == 200, r.text
    hint = r.json()["hint"]
    for key in _CONFLICT_KEYS:
        assert key not in hint, f"{key} reached a plain member"
    assert _CRITIQUE not in r.text
    # The projection is a trim, not a mangle: the member still gets their own
    # hint back, and the field the retract actually changed.
    assert hint["id"] == authored_hint
    assert hint["feedback_text"] == "wait for the grid"
    assert hint["is_active"] == 0


def test_a_curator_still_gets_the_whole_row(dash_client, authored_hint):
    """The other half of the same projection: an org admin curates these
    hints, so the LLM's critique is exactly what they need. Without this the
    test above would also pass if the fields had been dropped for everyone."""
    _flag(dash_client, authored_hint)
    r = _call(dash_client, "post", "/retract", {"actor": "a@e.com"},
              authored_hint, dash_client.org_a_admin_token)
    assert r.status_code == 200, r.text
    hint = r.json()["hint"]
    for key in _CONFLICT_KEYS:
        assert key in hint, f"{key} withheld from a curator"
    assert hint["conflict_flag_reason"] == _CRITIQUE


def test_the_already_retracted_no_op_is_projected_too(dash_client, authored_hint):
    """retract has two return points. The early "already retracted" one hands
    back the row it read, and it is reachable by the same plain member on the
    second click."""
    _flag(dash_client, authored_hint)
    first = _call(dash_client, "post", "/retract", {"actor": "a@e.com"},
                  authored_hint, dash_client.member_token)
    assert first.status_code == 200, first.text
    again = _call(dash_client, "post", "/retract", {"actor": "a@e.com"},
                  authored_hint, dash_client.member_token)
    assert again.status_code == 200, again.text
    assert again.json()["changed"] is False
    for key in _CONFLICT_KEYS:
        assert key not in again.json()["hint"], f"{key} reached a plain member"


def test_a_token_less_caller_cannot_create_a_hint(dash_client):
    """The fifth mutation route. create_hint has no hint row, so it carries
    the sibling check inline rather than _gate_hint_mutation — and that check
    was written `if caller is not None and ...`, so an anonymous caller
    skipped it entirely and could inject a hint into ANY org."""
    r = dash_client.post(
        "/api/learning/hints",
        json={
            "feedback_text": "anonymous injection",
            "anchor_query": "some example user request",
            "scope": "global", "org_id": dash_client.org_b, "actor": "nobody",
        },
    )
    assert r.status_code == 401, r.text

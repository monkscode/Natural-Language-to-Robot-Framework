"""get_hint must not leak a foreign org's hint, nor its timeline.

hint_audit and trigger_events carry no org_id, so the per-hint timeline cannot
be org-scoped in SQL. It used to be guarded by suppression alone: a shared hint
(`is_shared=1`) was fetchable from any org, so the endpoint returned the hint
but blanked its timeline for a caller that did not own it.

Since v20 there is no shared hint. The SELECT itself is org-scoped, so a
foreign hint 404s and the question of its timeline never arises. The
suppression stays as belt-and-braces (it is still the only thing scoping the
timeline if that SELECT is ever widened again). These tests pin the outer
guarantee — a foreign hint 404s — and that an owning admin still sees their
own timeline.
"""

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("learning_api_isolated")]


def _seed_hint_with_audit(dsn, *, org_id, text):
    """Insert a hint plus one hint_audit row; return the hint id."""
    from datetime import datetime, timezone
    from src.backend.crew_ai.optimization import pg_compat

    now = datetime.now(timezone.utc).isoformat()
    conn = pg_compat.connect(dsn)
    try:
        hid = conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(feedback_text, category, scope, evidence_count, anchor_query, "
            " is_active, conflict_flagged, org_id, created_at, last_seen) "
            "VALUES (?, 'test', 'global', 1, 'anchor', 1, 0, ?, ?, ?) RETURNING id",
            (text, org_id, now, now),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO hint_audit (hint_id, action, actor, reason, created_at) "
            "VALUES (?, 'edit', 'origin-admin@e.com', 'origin-org reason', ?)",
            (hid, now),
        )
        conn.commit()
        return hid
    finally:
        conn.close()


def test_a_foreign_orgs_hint_is_not_returned_at_all(dash_client, api_pg_em):
    """The stronger guarantee that replaced timeline suppression: an org-A admin
    cannot reach a hint owned by org B, so neither its text nor the workflow
    ids, trigger reasons and audit actors behind it are ever assembled."""
    _, dsn = api_pg_em
    hid = _seed_hint_with_audit(dsn, org_id=dash_client.org_b,
                               text="was shared from org B")

    r = dash_client.get(
        f"/api/learning/hints/{hid}",
        headers={"Authorization": f"Bearer {dash_client.org_a_admin_token}"},
    )
    assert r.status_code == 404, (
        f"a hint owned by another org must 404, got {r.status_code}: {r.text}"
    )


def test_own_org_hint_timeline_is_visible(dash_client, api_pg_em):
    """The owning org's admin still sees the full hint timeline."""
    _, dsn = api_pg_em
    hid = _seed_hint_with_audit(dsn, org_id=dash_client.org_a,
                               text="org A private")

    r = dash_client.get(
        f"/api/learning/hints/{hid}",
        headers={"Authorization": f"Bearer {dash_client.org_a_admin_token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["timeline"]) >= 1, (
        "the owning org's admin must see the hint's own timeline"
    )


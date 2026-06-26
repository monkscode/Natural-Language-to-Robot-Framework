"""get_hint must not leak a foreign org's timeline through a shared hint.

hint_audit and trigger_events carry no org_id, so the per-hint timeline cannot
be org-scoped in SQL. A shared hint (is_shared=1) is fetchable from any org via
the is_shared branch, so the endpoint suppresses its timeline for an org-scoped
caller that does not own the hint — otherwise an org-A admin viewing a hint that
originated in org B would see org B's workflow ids, trigger reasons and audit
actors. The hint's own org (and platform admins) still see the full timeline.
"""

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("learning_api_isolated")]


def _seed_hint_with_audit(dsn, *, org_id, is_shared, text):
    """Insert a hint plus one hint_audit row; return the hint id."""
    from datetime import datetime, timezone
    from src.backend.crew_ai.optimization import pg_compat

    now = datetime.now(timezone.utc).isoformat()
    conn = pg_compat.connect(dsn)
    try:
        hid = conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(feedback_text, category, scope, evidence_count, anchor_query, "
            " is_active, conflict_flagged, is_shared, org_id, created_at, last_seen) "
            "VALUES (?, 'test', 'global', 1, 'anchor', 1, 0, ?, ?, ?, ?) RETURNING id",
            (text, is_shared, org_id, now, now),
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


def test_foreign_shared_hint_timeline_is_suppressed(dash_client, api_pg_em):
    """An org-A admin viewing a shared hint owned by org B sees the hint but an
    empty timeline (no cross-org audit/trigger leak)."""
    _, dsn = api_pg_em
    hid = _seed_hint_with_audit(
        dsn, org_id=dash_client.org_b, is_shared=1, text="shared from org B"
    )

    r = dash_client.get(
        f"/api/learning/hints/{hid}",
        headers={"Authorization": f"Bearer {dash_client.org_a_admin_token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hint"]["id"] == hid  # visible only because is_shared=1
    assert body["timeline"] == [], (
        "a foreign org's audit/trigger timeline must not leak to another org's admin"
    )


def test_own_org_hint_timeline_is_visible(dash_client, api_pg_em):
    """The owning org's admin still sees the full hint timeline."""
    _, dsn = api_pg_em
    hid = _seed_hint_with_audit(
        dsn, org_id=dash_client.org_a, is_shared=0, text="org A private"
    )

    r = dash_client.get(
        f"/api/learning/hints/{hid}",
        headers={"Authorization": f"Bearer {dash_client.org_a_admin_token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["timeline"]) >= 1, (
        "the owning org's admin must see the hint's own timeline"
    )

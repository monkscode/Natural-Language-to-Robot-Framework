"""Platform-admin promotes a hint cross-org (is_shared=1 + anchor org nulled).

Task 10 — integration tests against the real learning_api_test schema (via the
learning_api_isolated fixture which injects a FeedbackLoop mock + patches
_admin_conn to the isolated DSN).

Anti-false-green contract: test_promote_sets_is_shared asserts the anchor
org_id IS NULL after promote.  Without the anchor-null UPDATE in the endpoint,
the anchor row retains org_id='org-test-1' and this test fails — catching any
future removal of that privacy-load-bearing UPDATE.
"""

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("learning_api_isolated")]


def test_promote_sets_is_shared(promote_client, seeded_hint_id, api_pg_em):
    """Promote → 200, is_shared=1, changed=True, AND anchor org_id IS NULL."""
    r = promote_client.post(
        f"/api/learning/hints/{seeded_hint_id}/promote",
        json={"actor": "owner@e.com", "reason": "useful everywhere"},
        headers={"Authorization": f"Bearer {promote_client.admin_token}"},
    )
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert body["hint"]["is_shared"] == 1
    assert body["changed"] is True

    # Anti-false-green: the anchor org_id MUST be NULL after promote.
    # Task 5's retrieval filter is `(org_id = ? OR org_id IS NULL)` — without
    # nulling the anchor, a promoted hint surfaces as a candidate but is dropped
    # at the anchor stage for every other org.  If this assertion fails it means
    # the endpoint's anchor-null UPDATE is missing or broken.
    from src.backend.crew_ai.optimization import pg_compat
    _, dsn = api_pg_em
    conn = pg_compat.connect(dsn)
    try:
        anchor = conn.execute(
            "SELECT org_id FROM learning_anchors "
            "WHERE kind = 'nl' AND record_id = ?",
            (seeded_hint_id,),
        ).fetchone()
        assert anchor is not None, (
            "No learning_anchors row found for the seeded hint — "
            "seed_hint fixture must insert one"
        )
        assert anchor["org_id"] is None, (
            f"anchor org_id should be NULL after promote but got {anchor['org_id']!r}; "
            "the endpoint's anchor-null UPDATE is missing or did not match"
        )
    finally:
        conn.close()


def test_promote_unknown_hint_404(promote_client):
    """Promoting a non-existent hint_id returns 404."""
    r = promote_client.post(
        "/api/learning/hints/999999/promote",
        json={"actor": "owner@e.com"},
        headers={"Authorization": f"Bearer {promote_client.admin_token}"},
    )
    assert r.status_code == 404


def test_promote_idempotent(promote_client, seeded_hint_id, api_pg_em):
    """Re-promoting an already-shared hint returns changed=False, no second audit row."""
    from src.backend.crew_ai.optimization import pg_compat
    _, dsn = api_pg_em

    # First promote
    r1 = promote_client.post(
        f"/api/learning/hints/{seeded_hint_id}/promote",
        json={"actor": "owner@e.com", "reason": "first promotion"},
        headers={"Authorization": f"Bearer {promote_client.admin_token}"},
    )
    assert r1.status_code == 200
    assert r1.json()["changed"] is True

    conn = pg_compat.connect(dsn)
    try:
        count_after_first = conn.execute(
            "SELECT COUNT(*) FROM hint_audit "
            "WHERE hint_id = ? AND action = 'promote'",
            (seeded_hint_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count_after_first == 1, (
        f"Expected exactly 1 audit row after first promote, got {count_after_first}"
    )

    # Second promote (idempotent — already is_shared=1)
    r2 = promote_client.post(
        f"/api/learning/hints/{seeded_hint_id}/promote",
        json={"actor": "owner@e.com", "reason": "second promotion"},
        headers={"Authorization": f"Bearer {promote_client.admin_token}"},
    )
    assert r2.status_code == 200
    assert r2.json()["changed"] is False, (
        "Re-promoting an already-shared hint must return changed=False"
    )

    conn = pg_compat.connect(dsn)
    try:
        count_after_second = conn.execute(
            "SELECT COUNT(*) FROM hint_audit "
            "WHERE hint_id = ? AND action = 'promote'",
            (seeded_hint_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count_after_second == 1, (
        f"Idempotent re-promote must not add a second audit row, "
        f"got {count_after_second}"
    )


def test_promote_blank_actor_400(promote_client, seeded_hint_id):
    """A blank actor is rejected with 400 (the guard fires before any DB work)."""
    r = promote_client.post(
        f"/api/learning/hints/{seeded_hint_id}/promote",
        json={"actor": "   ", "reason": "no actor"},
        headers={"Authorization": f"Bearer {promote_client.admin_token}"},
    )
    assert r.status_code == 400

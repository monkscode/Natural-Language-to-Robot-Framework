"""Hint promotion is disabled: no hint may ever cross org boundaries.

The former POST /hints/{id}/promote endpoint let a platform admin flip an
org-learned hint to `is_shared=1` (and null its anchor org), injecting it into
every org whose runs match the hint's domain. Product decision (2026-07-02
access-control review): hints learned on one customer's site must NEVER be
shared with another customer — global hints were to be exclusively
admin-CREATED (POST /hints, generic guidance only), never promoted from org
data. The route was removed to make that guarantee structural.

Schema v20 finished the job by removing the DESTINATION as well as the route:
there is no `is_shared` column, so an admin-created hint is org-owned like any
other and generic guidance wanted in several orgs is created once per org.

These tests pin all three halves: the route must not exist, no hint or anchor
state may change from calling the old path, and no column, value or scope
remains that would make one hint visible to a second org.
"""

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("learning_api_isolated")]


def _hint_and_anchor_state(dsn, hint_id):
    from src.backend.crew_ai.optimization import pg_compat
    conn = pg_compat.connect(dsn)
    try:
        hint = conn.execute(
            "SELECT org_id FROM nl_feedback_corrections WHERE id = ?",
            (hint_id,),
        ).fetchone()
        anchor = conn.execute(
            "SELECT org_id FROM learning_anchors WHERE kind = 'nl' AND record_id = ?",
            (hint_id,),
        ).fetchone()
        return hint, anchor
    finally:
        conn.close()


def test_promote_route_is_removed(promote_client, seeded_hint_id, api_pg_em):
    """POST /hints/{id}/promote must 404 (route gone), even for a platform admin."""
    r = promote_client.post(
        f"/api/learning/hints/{seeded_hint_id}/promote",
        json={"actor": "owner@e.com", "reason": "useful everywhere"},
        headers={"Authorization": f"Bearer {promote_client.admin_token}"},
    )
    assert r.status_code == 404, (
        f"promote endpoint must be removed, got {r.status_code}: {r.text}"
    )

    # Nothing may have been mutated: the hint stays org-private and the anchor
    # keeps its org (an org-less anchor would match every org's similarity
    # filter — the exact cross-org leak the removal prevents).
    _, dsn = api_pg_em
    hint, anchor = _hint_and_anchor_state(dsn, seeded_hint_id)
    assert hint is not None and hint["org_id"] == "org-test-1", (
        "hint must keep its owning org after hitting the removed promote path"
    )
    assert anchor is not None and anchor["org_id"] == "org-test-1", (
        "anchor org_id must be untouched after hitting the removed promote path"
    )


def test_promote_handler_is_gone_from_module():
    """The handler function itself must not linger importable (dead code that a
    future router include would silently resurrect)."""
    from src.backend.api import learning_endpoints
    assert not hasattr(learning_endpoints, "promote_hint"), (
        "promote_hint must be deleted, not just unrouted"
    )


def test_no_cross_org_destination_remains(api_pg_em):
    """Removing the route was half the guarantee; v20 removed the destination.

    While `is_shared` existed, anything that could set it — a future endpoint,
    an admin PATCH gaining a field, a migration backfill — reopened the leak,
    because every counter, flag and disable write is keyed by hint id alone and
    carries no org predicate. With no column there is nothing to set.
    """
    from src.backend.crew_ai.optimization import pg_compat
    _, dsn = api_pg_em
    conn = pg_compat.connect(dsn)
    try:
        cols = {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'nl_feedback_corrections' "
            "AND table_schema = current_schema()").fetchall()}
    finally:
        conn.close()
    assert "is_shared" not in cols, (
        "the shared-visibility column is back — promotion has a destination again"
    )
    assert "org_id" in cols, "every hint must still name exactly one owning org"


def test_no_source_file_still_reads_a_shared_flag():
    """A leftover `OR is_shared = 1` in a filter would be a silently dead
    predicate on a dropped column — or a live one again if the column returns.
    Scanned rather than declared: a declared list can drift from what ships."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "backend"
    offenders = []
    for path in root.rglob("*.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "is_shared" not in line:
                continue
            stripped = line.strip()
            # Comments and the historical v17 migration are records of what
            # WAS; only live SQL and live Python matter here.
            if stripped.startswith("#") or "ADD COLUMN IF NOT EXISTS is_shared" in line:
                continue
            if "idx_nlfc_org_shared" in line or "DROP COLUMN IF EXISTS is_shared" in line:
                continue
            # A PG_MIGRATIONS entry header — `(17, "…is_shared…",` — is the
            # historical description of a shipped migration, not live SQL.
            if re.match(r"^\(\d+, \"", stripped):
                continue
            offenders.append(f"{path.relative_to(root)}:{i}: {stripped}")
    assert not offenders, "live is_shared usage remains:\n" + "\n".join(offenders)

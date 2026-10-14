"""
T10 — every hint is owned by exactly one org; `is_shared` is gone.

`is_shared` was read on retrieval only, never on mutation: `get_hints_by_id`,
`apply_hint_attribution`, `_flag_hints_no_commit` and the auto-disable and
auto-retire paths are all bare `WHERE id = ?`.  So a shared hint injected into
any org's run had its counters, its `conflict_flagged` and its `is_active`
mutated with no org scoping — one tenant's outcomes deciding another tenant's
hint lifecycle.  Cross-org promotion was already removed for that reason in
2026-07; the owner's decision here is to remove the remaining admin-created
shared hints too, so the guarantee is structural rather than policed.

`scope='global'` is a DIFFERENT axis and is retained: it says which queries a
hint applies to, WITHIN an org.

`POST /hints` keeps its place as the manual half of copy-on-promote: an admin
wanting generic guidance in several orgs creates it once per org, and each
copy then owns its own counters, flags and disables permanently.
"""

from datetime import datetime, timezone

import pytest

from src.backend.crew_ai.optimization import pg_compat

pytestmark = [pytest.mark.integration,
              pytest.mark.usefixtures("learning_api_isolated")]

_TEXT = "prefer data-testid over nth-child selectors"
_ANCHOR = "click the second row's edit button"


def _columns(dsn, table):
    # current_schema() matters: the DSN puts the isolated test schema first but
    # keeps public on the search_path, so an unscoped catalog query also returns
    # the LIVE public table and reads whichever version that is at.
    conn = pg_compat.connect(dsn)
    try:
        return {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ? AND table_schema = current_schema()",
            (table,)).fetchall()}
    finally:
        conn.close()


def _hint(dsn, hint_id):
    conn = pg_compat.connect(dsn)
    try:
        return conn.execute(
            "SELECT org_id, created_via, evidence_count, scope "
            "FROM nl_feedback_corrections WHERE id = ?", (hint_id,)).fetchone()
    finally:
        conn.close()


def _seed_admin_hint(dsn, *, org_id, text=_TEXT):
    now = datetime.now(timezone.utc).isoformat()
    conn = pg_compat.connect(dsn)
    try:
        row = conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(feedback_text, category, scope, evidence_count, anchor_query, "
            " is_active, conflict_flagged, org_id, created_via, "
            " created_at, last_seen) "
            "VALUES (?, 'locator', 'global', 1, ?, 1, 0, ?, 'admin', ?, ?) "
            "RETURNING id",
            (text, _ANCHOR, org_id, now, now)).fetchone()
        conn.commit()
        return row["id"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The column itself
# ---------------------------------------------------------------------------

class TestTheSharedFlagIsGone:
    def test_the_column_no_longer_exists(self, api_pg_em):
        _, dsn = api_pg_em
        assert "is_shared" not in _columns(dsn, "nl_feedback_corrections")

    def test_org_id_is_still_indexed(self, api_pg_em):
        """`idx_nlfc_org_shared` was `(org_id, is_shared)` — the ONLY index on
        the hint table's org_id, and dropping the column drops it with them.
        Every hint read now filters on `org_id = ?`, so the index is replaced
        rather than silently lost."""
        _, dsn = api_pg_em
        conn = pg_compat.connect(dsn)
        try:
            names = {r["indexname"] for r in conn.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'nl_feedback_corrections' "
                "AND schemaname = current_schema()").fetchall()}
        finally:
            conn.close()
        assert "idx_nlfc_org_shared" not in names
        assert "idx_nlfc_org" in names

    def test_global_scope_survives(self, api_pg_em):
        """scope='global' is a different axis — which queries a hint applies to
        WITHIN an org — and is deliberately retained."""
        _, dsn = api_pg_em
        hid = _seed_admin_hint(dsn, org_id="org-test-1")
        assert _hint(dsn, hid)["scope"] == "global"


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

class TestNoReadCrossesAnOrg:
    def test_the_hint_list_shows_only_the_callers_org(self, dash_client):
        mine = dash_client.seed_hint(dash_client.org_a, "org A private")
        theirs = dash_client.seed_hint(dash_client.org_b, "org B private")
        r = dash_client.get(
            "/api/learning/hints",
            headers={"Authorization": f"Bearer {dash_client.org_a_admin_token}"})
        assert r.status_code == 200
        ids = {h["id"] for h in r.json()["hints"]}
        assert mine in ids
        assert theirs not in ids

    def test_another_orgs_hint_is_not_fetchable_by_id(self, dash_client):
        theirs = dash_client.seed_hint(dash_client.org_b, "org B private")
        r = dash_client.get(
            f"/api/learning/hints/{theirs}",
            headers={"Authorization": f"Bearer {dash_client.org_a_admin_token}"})
        assert r.status_code == 404, (
            "an admin-created hint in another org must be invisible; there is "
            "no shared-visibility branch left to fetch it through")

    def test_an_admin_created_hint_does_not_leak_either(self, dash_client,
                                                        api_pg_em):
        """The removed flag was set exclusively by admin creates, so this is
        the case that used to cross and must not any more."""
        _, dsn = api_pg_em
        theirs = _seed_admin_hint(dsn, org_id=dash_client.org_b)
        r = dash_client.get(
            f"/api/learning/hints/{theirs}",
            headers={"Authorization": f"Bearer {dash_client.org_a_admin_token}"})
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Admin create targets a named org
# ---------------------------------------------------------------------------

class TestAdminCreateTargetsAnOrg:
    def _body(self, **over):
        body = {"feedback_text": _TEXT, "anchor_query": _ANCHOR,
                "scope": "global", "actor": "owner@e.com"}
        body.update(over)
        return body

    def test_a_create_without_an_org_is_refused(self, promote_client):
        r = promote_client.post(
            "/api/learning/hints", json=self._body(),
            headers={"Authorization": f"Bearer {promote_client.admin_token}"})
        assert r.status_code == 422, (
            "org_id is required — a hint with no owner is the shared hint this "
            "task removes, under another name")

    def test_a_blank_org_is_refused(self, promote_client):
        r = promote_client.post(
            "/api/learning/hints", json=self._body(org_id="   "),
            headers={"Authorization": f"Bearer {promote_client.admin_token}"})
        assert r.status_code == 400

    def test_a_create_owns_the_named_org(self, promote_client, api_pg_em):
        _, dsn = api_pg_em
        r = promote_client.post(
            "/api/learning/hints", json=self._body(org_id="org-test-1"),
            headers={"Authorization": f"Bearer {promote_client.admin_token}"})
        assert r.status_code == 201, r.text
        row = _hint(dsn, r.json()["hint"]["id"])
        assert row["org_id"] == "org-test-1"
        assert row["created_via"] == "admin"

    def test_a_repeat_create_dedups_within_the_org(self, promote_client,
                                                   api_pg_em):
        _, dsn = api_pg_em
        hdr = {"Authorization": f"Bearer {promote_client.admin_token}"}
        first = promote_client.post(
            "/api/learning/hints", json=self._body(org_id="org-test-1"),
            headers=hdr)
        second = promote_client.post(
            "/api/learning/hints", json=self._body(org_id="org-test-1"),
            headers=hdr)
        assert first.json()["created"] is True
        assert second.json()["created"] is False
        assert second.json()["hint"]["id"] == first.json()["hint"]["id"]
        assert _hint(dsn, first.json()["hint"]["id"])["evidence_count"] == 2

    def test_the_same_text_in_another_org_is_a_separate_hint(self,
                                                             promote_client,
                                                             api_pg_em):
        """This is copy-on-promote's manual half: identical guidance in two orgs
        is two rows, so their counters, flags and disables stay independent."""
        _, dsn = api_pg_em
        hdr = {"Authorization": f"Bearer {promote_client.admin_token}"}
        a = promote_client.post(
            "/api/learning/hints", json=self._body(org_id="org-test-1"),
            headers=hdr)
        b = promote_client.post(
            "/api/learning/hints", json=self._body(org_id="org-test-2"),
            headers=hdr)
        assert a.status_code == 201 and b.status_code == 201, (a.text, b.text)
        assert a.json()["hint"]["id"] != b.json()["hint"]["id"]
        assert _hint(dsn, a.json()["hint"]["id"])["org_id"] == "org-test-1"
        assert _hint(dsn, b.json()["hint"]["id"])["org_id"] == "org-test-2"

    def test_the_anchor_is_enqueued_with_the_same_org(self, promote_client):
        """An org-less anchor satisfies `filter_by_query_similarity`'s
        `org_id IS NULL` branch for every caller, so the anchor must be owned
        too — the SQL gate is then not the only thing standing between orgs.

        The anchor write goes through the write queue, which this fixture
        replaces with a MagicMock, so the enqueued call is what can be checked.
        """
        from src.backend.crew_ai.optimization.learning_registry import (
            get_feedback_loop,
        )
        r = promote_client.post(
            "/api/learning/hints", json=self._body(org_id="org-test-1"),
            headers={"Authorization": f"Bearer {promote_client.admin_token}"})
        assert r.status_code == 201, r.text
        hint_id = r.json()["hint"]["id"]
        submits = get_feedback_loop().write_queue.submit.call_args_list
        anchors = [c for c in submits
                   if c.args[1:2] == ("nl",) and c.args[2:3] == (hint_id,)]
        assert anchors, f"admin create must enqueue its anchor; got {submits}"
        assert anchors[-1].kwargs.get("org_id") == "org-test-1"

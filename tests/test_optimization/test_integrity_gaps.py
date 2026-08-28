"""T12 — the dedup indexes and the engine's dedup key say the same thing.

Two pre-existing gaps, both older than this plan:

1. **NULL-distinct indexes vs a NULL-equal engine.** v18's unique indexes use
   plain `domain`/`url`, so Postgres treats two NULL domains as different keys,
   while the engine's SELECT uses `IS NOT DISTINCT FROM`, which treats them as
   the same. One writer thread and one uvicorn worker hide it today; a second
   replica does not. Aligned to `COALESCE(domain,'')` / `COALESCE(url,'')`.

2. **Global hints duplicate across domains.** The `keyword` triage category maps
   to `scope='global'` and `_SCOPE_WHERE` ignores `domain` for global hints — but
   `domain` was still in the dedup key, so the same correction typed on two sites
   became two org-wide hints with split evidence. Both would be injected and
   neither would reach a threshold. `domain` is out of the global key now.

Migration 21 is the one statement in this plan that DELETES rows, so its order
is load-bearing and pinned here by execution, not by reading:

* `CREATE UNIQUE INDEX IF NOT EXISTS` matches on relation NAME, not definition,
  so the new indexes take NEW names — reusing the old ones would silently leave
  v18's definitions in place.
* Both `ensure_schema` call sites run autocommit, so DROP-before-CREATE would
  leave the DROPs durable when a CREATE failed and the table would have NO dedup
  uniqueness during the 300s retry loop. Every CREATE therefore precedes every
  DROP, and that ordering is asserted structurally so a later edit cannot undo it.
* A failing migration does not crash the app — `get_feedback_loop` catches it and
  disables the ENTIRE learning system behind one WARNING. So the pre-flight merge
  must leave zero collisions, and it must be safe to run again.

The merge DELETES absorbed rows rather than deactivating them: the unique indexes
carry no `is_active` predicate, so a deactivated duplicate still aborts the
CREATE, and kept rows would re-sum their counters on every retry.

Referenced by: docs/superpowers/plans/2026-08-26-feedback-integrity-and-org-isolation.md (T12)
Depends on: src/backend/crew_ai/optimization/pg_schema.py,
            src/backend/crew_ai/optimization/nl_feedback_engine.py
"""

import json
from datetime import datetime, timezone

import psycopg
import pytest

from src.backend.core.config import settings
from src.backend.crew_ai.optimization import pg_schema
from src.backend.crew_ai.optimization.execution_memory import ExecutionRecord
from src.backend.crew_ai.optimization.nl_feedback_engine import NLFeedbackEngine

pytestmark = pytest.mark.integration

_SCHEMA = "integrity_gaps_test"
_ORG = "org-A"
_NOW = datetime.now(timezone.utc)

# The v18 index definitions, verbatim from migration 18 — the state a real
# deployment is upgrading FROM. Recreating them exactly is what makes the
# rewind faithful rather than approximate.
# Schema-qualified on purpose. The fixture DSN keeps `public` on the
# search_path so the pgvector type resolves, which means an unqualified DDL
# statement can silently land on the LIVE schema the moment the object it names
# is missing here — the same trap as a bare DROP TABLE in this package.
_V20_INDEXES = (
    "CREATE UNIQUE INDEX uq_nlfc_dedup_general "
    "ON {s}.nl_feedback_corrections "
    "(feedback_text, domain, scope, COALESCE(org_id, '')) "
    "WHERE scope <> 'url'",
    "CREATE UNIQUE INDEX uq_nlfc_dedup_url "
    "ON {s}.nl_feedback_corrections "
    "(feedback_text, domain, url, scope, COALESCE(org_id, '')) "
    "WHERE scope = 'url'",
)
_V21_NAMES = (
    "uq_nlfc_dedup_general_v21",
    "uq_nlfc_dedup_url_v21",
    "uq_nlfc_dedup_global_v21",
)


# ---------------------------------------------------------------------------
# schema harness
# ---------------------------------------------------------------------------

@pytest.fixture
def pg():
    """A throwaway schema at the CURRENT version, dropped afterwards."""
    admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    admin.execute(f"CREATE SCHEMA {_SCHEMA}")
    # public stays on the path so the pgvector `vector` type resolves.
    dsn = settings.DATABASE_URL + f"?options=-c%20search_path%3D{_SCHEMA},public"
    # No connection-level row_factory: ensure_schema reads its own rows
    # positionally, so a dict_row connection makes the migration runner itself
    # raise KeyError. The helpers below take a dict cursor instead.
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        pg_schema.ensure_schema(conn)
        yield conn
    finally:
        conn.close()
        admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        admin.close()


def rewind_to_v20(conn):
    """Put the schema back in the state migration 21 upgrades FROM."""
    for name in _V21_NAMES:
        conn.execute(f"DROP INDEX IF EXISTS {_SCHEMA}.{name}")
    for ddl in _V20_INDEXES:
        conn.execute(ddl.format(s=_SCHEMA))
    conn.execute("DELETE FROM schema_version WHERE version >= 21")


def q(conn, sql, params=()):
    """Rows as dicts. The connection stays tuple-rowed for ensure_schema."""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def indexes(conn):
    return {r["indexname"]: r["indexdef"] for r in q(
        conn,
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE schemaname = %s AND tablename = 'nl_feedback_corrections'",
        (_SCHEMA,),
    )}


def hint(conn, **over):
    """Insert one hint, returning its id. Every column the merge resolves is
    settable so a test can build a group with genuinely different states."""
    col = {
        "feedback_text": "wait for the spinner", "category": "structural",
        "scope": "domain", "domain": "shop.test", "url": None,
        "evidence_count": 1, "applied_count": 0, "success_count": 0,
        "failure_count": 0, "unused_count": 0, "is_active": 1,
        "conflict_flagged": 0, "conflict_flagged_at": None,
        "conflict_flag_reason": None, "disabled_at": None,
        "created_at": _NOW.isoformat(), "last_seen": _NOW.isoformat(),
        "org_id": _ORG, "source_workflow_id": None, "anchor_query": None,
    }
    col.update(over)
    names = ", ".join(col)
    marks = ", ".join(["%s"] * len(col))
    return conn.execute(
        f"INSERT INTO nl_feedback_corrections ({names}) VALUES ({marks}) RETURNING id",
        tuple(col.values()),
    ).fetchone()[0]


def rows(conn):
    return q(conn, "SELECT * FROM nl_feedback_corrections ORDER BY id")


def evidence(conn, hint_id=None):
    if hint_id is None:
        return q(conn, "SELECT hint_id, source_kind, source_hash, bucket "
                       "FROM hint_evidence ORDER BY hint_id, id")
    return q(conn, "SELECT hint_id, source_kind, source_hash, bucket "
                   "FROM hint_evidence WHERE hint_id = %s ORDER BY id", (hint_id,))


def add_evidence(conn, hint_id, kind, key, hash_, bucket):
    conn.execute(
        "INSERT INTO hint_evidence "
        "(hint_id, source_kind, source_key, source_hash, bucket, created_at) "
        "VALUES (%s, %s, %s, %s, %s, now()::text)",
        (hint_id, kind, key, hash_, bucket),
    )


def merge_audit(conn):
    return q(conn, "SELECT hint_id, actor, reason, before_value, after_value "
                   "FROM hint_audit WHERE action = 'merge' ORDER BY id")


# ---------------------------------------------------------------------------
# Step 2 / Step 3 — the keys themselves
# ---------------------------------------------------------------------------

class TestTheIndexesMatchTheEnginesDedupKey:

    def test_two_null_domain_hints_can_no_longer_both_exist(self, pg):
        """v18 indexed `domain` plainly, so Postgres called two NULLs distinct
        while the engine's `IS NOT DISTINCT FROM` called them equal. Under one
        writer the engine never inserts the second; a second replica does."""
        hint(pg, domain=None)
        with pytest.raises(psycopg.errors.UniqueViolation):
            hint(pg, domain=None)

    def test_a_null_url_is_not_a_second_url_scoped_hint(self, pg):
        hint(pg, scope="url", url=None)
        with pytest.raises(psycopg.errors.UniqueViolation):
            hint(pg, scope="url", url=None)

    def test_the_same_global_hint_on_two_domains_is_one_hint(self, pg):
        """A global hint applies to every query in the org regardless of the
        page it was typed on, so `domain` has no business in its dedup key."""
        hint(pg, scope="global", domain="shop.test")
        with pytest.raises(psycopg.errors.UniqueViolation):
            hint(pg, scope="global", domain="other.test")

    def test_the_three_keys_do_not_overlap(self, pg):
        """Partial-index predicates must partition the table, or a global hint
        is keyed twice and one of the two keys is wrong. `scope` is NOT NULL,
        so `NOT IN ('url','global')` / `= 'url'` / `= 'global'` is exhaustive."""
        general = hint(pg, scope="domain")
        url = hint(pg, scope="url", url="https://shop.test/cart")
        glob = hint(pg, scope="global")
        assert {r["id"] for r in rows(pg)} == {general, url, glob}
        # Same text under a different scope is a different hint in all three
        # directions — nothing was over-collapsed by widening the global key.
        assert len(rows(pg)) == 3

    def test_a_second_org_still_gets_its_own_copy(self, pg):
        """org_id stays in every key: hints are org-private and one tenant's
        text must never dedup into another's row."""
        hint(pg, scope="global")
        hint(pg, scope="global", org_id="org-B")
        assert len(rows(pg)) == 2

    def test_url_scoped_hints_still_key_on_the_page(self, pg):
        hint(pg, scope="url", url="https://shop.test/cart")
        hint(pg, scope="url", url="https://shop.test/checkout")
        with pytest.raises(psycopg.errors.UniqueViolation):
            hint(pg, scope="url", url="https://shop.test/cart")


class TestTheEngineAgreesWithTheIndex:
    """The engine's SELECT and the index must agree, or the SELECT misses and
    the INSERT dies on the constraint — the hint is lost, not deduplicated."""

    @staticmethod
    def _record(workflow_id, url):
        return ExecutionRecord(
            workflow_id=workflow_id, timestamp=_NOW,
            user_query="search the catalogue", url=url,
            domain=url.split("/")[2], test_status="failed", org_id=_ORG,
        )

    def test_one_global_correction_typed_on_two_sites_is_one_hint(self, in_memory_db):
        """category 'keyword' -> scope 'global'. Before T12 this produced two
        org-wide hints with split evidence, both injected, neither ever
        reaching a lifecycle threshold."""
        engine = NLFeedbackEngine(in_memory_db)
        triage = {"category": "keyword", "feedback_text": "use Get Text, not Get Property",
                  "actor": "alice@example.com"}

        engine.learn_from_feedback(self._record("wf-1", "https://shop.test/a"), triage)
        engine.learn_from_feedback(self._record("wf-2", "https://other.test/b"), triage)

        found = in_memory_db.execute(
            "SELECT id, domain, evidence_count FROM nl_feedback_corrections "
            "ORDER BY id").fetchall()
        assert len(found) == 1, (
            f"the same global correction on two sites made {len(found)} hints"
        )
        assert found[0]["evidence_count"] == 2, (
            "the second site's submission did not reinforce the first hint"
        )

    def test_a_domain_scoped_correction_on_two_sites_is_still_two_hints(
        self, in_memory_db,
    ):
        """Anti-false-green: only the GLOBAL key loses `domain`. A domain-scoped
        hint is about its domain, and collapsing those would be a real defect."""
        engine = NLFeedbackEngine(in_memory_db)
        triage = {"category": "structural", "feedback_text": "the table renders late",
                  "actor": "alice@example.com"}

        engine.learn_from_feedback(self._record("wf-1", "https://shop.test/a"), triage)
        engine.learn_from_feedback(self._record("wf-2", "https://other.test/b"), triage)

        found = rows_from(in_memory_db)
        assert [r["domain"] for r in found] == ["shop.test", "other.test"]

    def test_the_global_reinforcement_keeps_the_first_domain(self, in_memory_db):
        """The survivor's `domain` column is now decorative for global hints —
        it records where the correction was first typed and is not part of the
        key. Pinned so a later reader does not assume it was cleared."""
        engine = NLFeedbackEngine(in_memory_db)
        triage = {"category": "keyword", "feedback_text": "use Get Text",
                  "actor": "a@b.c"}
        engine.learn_from_feedback(self._record("wf-1", "https://shop.test/a"), triage)
        engine.learn_from_feedback(self._record("wf-2", "https://other.test/b"), triage)

        assert rows_from(in_memory_db)[0]["domain"] == "shop.test"


def rows_from(conn):
    return conn.execute(
        "SELECT id, domain, evidence_count FROM nl_feedback_corrections ORDER BY id"
    ).fetchall()


# ---------------------------------------------------------------------------
# Step 4 — the index swap
# ---------------------------------------------------------------------------

class TestTheIndexSwapIsCreateFirst:

    def test_every_create_precedes_every_drop(self):
        """S3's ordering, pinned structurally. Both `ensure_schema` call sites
        run autocommit, so a DROP that lands before a failing CREATE is durable
        and the table spends the 300s retry loop with no dedup uniqueness at
        all. Reading the migration is not enough — this must fail if a later
        edit reorders it."""
        statements = dict(
            (v, s) for v, _, s in pg_schema.PG_MIGRATIONS)[21]
        kinds = [
            "create" if "CREATE UNIQUE INDEX" in s
            else "drop" if s.strip().startswith("DROP INDEX")
            else "other"
            for s in statements
        ]
        assert "create" in kinds and "drop" in kinds
        assert kinds.index("drop") > max(
            i for i, k in enumerate(kinds) if k == "create"
        ), f"migration 21 drops an old index before creating a new one: {kinds}"

    def test_the_new_indexes_take_new_names(self):
        """`CREATE UNIQUE INDEX IF NOT EXISTS` matches on relation NAME, not
        definition (proven by execution, R2). Reusing `uq_nlfc_dedup_general`
        would be a silent no-op on every existing database: the statement
        succeeds, the old NULL-distinct definition stays, and nothing reports
        it. The new names are the whole reason the swap works."""
        statements = dict(
            (v, s) for v, _, s in pg_schema.PG_MIGRATIONS)[21]
        created = [s for s in statements if "CREATE UNIQUE INDEX" in s]
        assert len(created) == 3
        for name in _V21_NAMES:
            assert any(name in s for s in created), f"{name} is not created"
        for stale in ("uq_nlfc_dedup_general ", "uq_nlfc_dedup_url "):
            assert not any(f"EXISTS {stale}" in s for s in created), (
                f"migration 21 reuses the v18 name {stale.strip()} — "
                "IF NOT EXISTS would match it and change nothing"
            )

    def test_upgrading_a_v20_database_replaces_the_index_set(self, pg):
        rewind_to_v20(pg)
        before = indexes(pg)
        assert "uq_nlfc_dedup_general" in before and "uq_nlfc_dedup_url" in before
        assert not any(n in before for n in _V21_NAMES)

        assert pg_schema.ensure_schema(pg) == 21

        after = indexes(pg)
        assert set(_V21_NAMES) <= set(after)
        assert "uq_nlfc_dedup_general" not in after
        assert "uq_nlfc_dedup_url" not in after
        assert 21 in {r["version"] for r in q(
            pg, "SELECT version FROM schema_version")}

    def test_the_new_definitions_actually_took_effect(self, pg):
        """The R2 trap in one assertion: a swap that "succeeded" but left v18's
        definitions behind passes every name check and still permits the
        duplicate. Assert on behaviour, and on COALESCE being in the indexdef."""
        rewind_to_v20(pg)
        first = hint(pg, domain=None)
        second = hint(pg, domain=None)  # v18 permits this — NULLs are distinct
        assert first != second

        pg.execute("DELETE FROM nl_feedback_corrections WHERE id = %s", (second,))
        pg_schema.ensure_schema(pg)

        defs = indexes(pg)
        assert "COALESCE(domain" in defs["uq_nlfc_dedup_general_v21"]
        with pytest.raises(psycopg.errors.UniqueViolation):
            hint(pg, domain=None)

    def test_a_fresh_database_gets_the_v21_indexes_too(self, pg):
        """The dedup indexes live only in migrations, never in the baseline
        DDL, and `ensure_schema` records only the baseline version — so a fresh
        install must still walk 18 -> 21 and end on the new names."""
        after = indexes(pg)
        assert set(_V21_NAMES) <= set(after)
        assert "uq_nlfc_dedup_general" not in after


# ---------------------------------------------------------------------------
# Step 5 — the pre-flight merge
# ---------------------------------------------------------------------------

class TestTheMerge:

    def test_null_domain_duplicates_merge(self, pg):
        """(a) The shape v18's NULL-distinct index permitted."""
        rewind_to_v20(pg)
        keep = hint(pg, domain=None, created_at="2026-01-01T00:00:00+00:00",
                    evidence_count=2)
        gone = hint(pg, domain=None, created_at="2026-02-01T00:00:00+00:00",
                    evidence_count=3)

        pg_schema.ensure_schema(pg)

        remaining = rows(pg)
        assert [r["id"] for r in remaining] == [keep], (
            f"expected only the oldest row {keep} to survive"
        )
        assert remaining[0]["evidence_count"] == 5
        assert gone not in {r["id"] for r in remaining}

    def test_two_global_hints_differing_only_in_domain_merge(self, pg):
        """(b) The shape Step 3's wider key creates. Nothing produced these
        before, which is exactly why the merge has to exist: the index would
        abort on them and silently disable the whole learning system."""
        rewind_to_v20(pg)
        keep = hint(pg, scope="global", domain="shop.test",
                    created_at="2026-01-01T00:00:00+00:00", evidence_count=4,
                    success_count=2)
        hint(pg, scope="global", domain="other.test",
             created_at="2026-03-01T00:00:00+00:00", evidence_count=1,
             success_count=5)

        pg_schema.ensure_schema(pg)

        remaining = rows(pg)
        assert [r["id"] for r in remaining] == [keep]
        assert remaining[0]["evidence_count"] == 5
        assert remaining[0]["success_count"] == 7

    def test_a_group_mixing_states_resolves_deterministically(self, pg):
        """(c) Preserve-when-uncertain, one expression per column: active if
        ANY was active, flagged if ANY was flagged (newest flag metadata wins),
        counters summed, last_seen the latest."""
        rewind_to_v20(pg)
        keep = hint(
            pg, domain=None, created_at="2026-01-01T00:00:00+00:00",
            last_seen="2026-01-01T00:00:00+00:00",
            is_active=0, disabled_at="2026-01-05T00:00:00+00:00",
            evidence_count=1, applied_count=3, success_count=1,
            failure_count=2, unused_count=4,
        )
        active = hint(
            pg, domain=None, created_at="2026-02-01T00:00:00+00:00",
            last_seen="2026-06-01T00:00:00+00:00",
            is_active=1, evidence_count=5, applied_count=1, success_count=2,
            failure_count=0, unused_count=1,
        )
        flagged = hint(
            pg, domain=None, created_at="2026-03-01T00:00:00+00:00",
            last_seen="2026-02-02T00:00:00+00:00",
            is_active=0, conflict_flagged=1,
            conflict_flagged_at="2026-04-01T00:00:00+00:00",
            conflict_flag_reason="Trigger 1 said it caused the failure",
            evidence_count=2, applied_count=2, success_count=0,
            failure_count=1, unused_count=0,
        )
        add_evidence(pg, active, "query", "search shoes", "h-shoes", "used")
        add_evidence(pg, flagged, "query", "book a flight", "h-flight", "failure")
        add_evidence(pg, keep, "query", "search shoes", "h-shoes", "used")

        pg_schema.ensure_schema(pg)

        remaining = rows(pg)
        assert [r["id"] for r in remaining] == [keep]
        s = remaining[0]
        assert s["evidence_count"] == 8
        assert s["applied_count"] == 6
        assert s["success_count"] == 3
        assert s["failure_count"] == 3
        assert s["unused_count"] == 5
        assert s["last_seen"] == "2026-06-01T00:00:00+00:00"
        assert s["is_active"] == 1, "a group holding an active hint stays served"
        assert s["disabled_at"] is None, (
            "the survivor was reactivated by the merge but still claims a "
            "disable timestamp"
        )
        assert s["conflict_flagged"] == 1, "the flag on a merged row was lost"
        assert s["conflict_flagged_at"] == "2026-04-01T00:00:00+00:00"
        assert "Trigger 1" in s["conflict_flag_reason"]

        # hint_evidence follows the survivor, and the duplicate source that
        # both rows carried collapses rather than being counted twice.
        assert [(r["source_hash"], r["bucket"]) for r in evidence(pg)] == [
            ("h-flight", "failure"), ("h-shoes", "used"),
        ] or [(r["source_hash"], r["bucket"]) for r in evidence(pg)] == [
            ("h-shoes", "used"), ("h-flight", "failure"),
        ]
        assert {r["hint_id"] for r in evidence(pg)} == {keep}

        # One audit row per absorbed hint, on the survivor's timeline.
        audit = merge_audit(pg)
        assert [r["hint_id"] for r in audit] == [keep, keep]
        assert {r["actor"] for r in audit} == {"migration_v21"}
        absorbed_ids = {json.loads(r["before_value"])["id"] for r in audit}
        assert absorbed_ids == {active, flagged}
        assert json.loads(audit[0]["after_value"])["evidence_count"] == 8

    def test_running_the_merge_twice_changes_nothing(self, pg):
        """(d) Idempotency is what makes the 300s retry loop safe: a second
        pass must not re-sum the counters it already merged."""
        rewind_to_v20(pg)
        keep = hint(pg, domain=None, created_at="2026-01-01T00:00:00+00:00",
                    evidence_count=2)
        hint(pg, domain=None, created_at="2026-02-01T00:00:00+00:00",
             evidence_count=3)

        pg_schema.ensure_schema(pg)
        first = [dict(r) for r in rows(pg)]
        first_audit = len(merge_audit(pg))

        # Re-run the migration itself, not just ensure_schema (which would skip
        # a recorded version) — this is the retry after a mid-migration failure.
        pg.execute("DELETE FROM schema_version WHERE version = 21")
        pg_schema.ensure_schema(pg)

        assert [dict(r) for r in rows(pg)] == first
        assert rows(pg)[0]["id"] == keep
        assert len(merge_audit(pg)) == first_audit, (
            "the retry wrote a second merge audit row for a merge that "
            "happened once"
        )

    def test_a_claim_row_survives_on_the_survivor(self, pg, in_memory_db):
        """(e) T5's per-run claim is `source_kind='workflow'`. Moving it means a
        run that already submitted this correction cannot claim the survivor a
        second time — dropping it instead would hand that run a free evidence
        bump, unflag and unused reset on the merged hint."""
        rewind_to_v20(pg)
        keep = hint(pg, domain=None, created_at="2026-01-01T00:00:00+00:00")
        gone = hint(pg, domain=None, created_at="2026-02-01T00:00:00+00:00")
        add_evidence(pg, gone, "workflow", "wf-9", "hash-of-wf-9", "evidence")

        pg_schema.ensure_schema(pg)

        claims = evidence(pg, keep)
        assert [(r["source_kind"], r["source_hash"]) for r in claims] == [
            ("workflow", "hash-of-wf-9")], (
            "the absorbed hint's claim row did not follow the survivor"
        )
        assert evidence(pg, gone) == []

    def test_a_run_that_claimed_both_rows_collapses_to_one_claim(self, pg):
        """The `ON CONFLICT DO NOTHING` leg: one run may have claimed the
        survivor AND an absorbed row. Two identical claims cannot coexist under
        uq_hint_evidence, and the insert must not abort the migration."""
        rewind_to_v20(pg)
        keep = hint(pg, domain=None, created_at="2026-01-01T00:00:00+00:00")
        gone = hint(pg, domain=None, created_at="2026-02-01T00:00:00+00:00")
        add_evidence(pg, keep, "workflow", "wf-9", "hash-of-wf-9", "evidence")
        add_evidence(pg, gone, "workflow", "wf-9", "hash-of-wf-9", "evidence")

        pg_schema.ensure_schema(pg)

        assert len(evidence(pg, keep)) == 1

    def test_two_absorbed_rows_sharing_a_source_collapse_to_one(self, pg):
        """`ON CONFLICT DO NOTHING` also has to absorb duplicates arriving
        within the SAME insert — two absorbed rows carrying one source produce
        two identical candidate rows for the survivor. (DO UPDATE would raise
        "cannot affect row a second time" here; DO NOTHING is why this works,
        and it is a Postgres subtlety worth pinning rather than assuming.)"""
        rewind_to_v20(pg)
        keep = hint(pg, domain=None, created_at="2026-01-01T00:00:00+00:00")
        b = hint(pg, domain=None, created_at="2026-02-01T00:00:00+00:00")
        c = hint(pg, domain=None, created_at="2026-03-01T00:00:00+00:00")
        add_evidence(pg, b, "query", "search shoes", "h-shoes", "used")
        add_evidence(pg, c, "query", "search shoes", "h-shoes", "used")

        pg_schema.ensure_schema(pg)

        assert [(r["hint_id"], r["source_hash"]) for r in evidence(pg)] == [
            (keep, "h-shoes")]

    def test_the_absorbed_rows_anchors_go_with_them(self, pg):
        """A dangling anchor is harmless (the hint SELECT runs first, so its id
        can never match), but leaving one behind is dead weight under an HNSW
        index and would keep matching a hint that no longer exists."""
        rewind_to_v20(pg)
        keep = hint(pg, domain=None, created_at="2026-01-01T00:00:00+00:00")
        gone = hint(pg, domain=None, created_at="2026-02-01T00:00:00+00:00")
        for hid in (keep, gone):
            pg.execute(
                "INSERT INTO learning_anchors "
                "(anchor_key, kind, record_id, anchor_query, embedding, org_id) "
                "VALUES (%s, 'nl', %s, 'q', %s, %s)",
                (f"nl:{hid}", hid, "[" + ",".join(["0"] * 384) + "]", _ORG),
            )

        pg_schema.ensure_schema(pg)

        left = {r["record_id"] for r in q(
            pg, "SELECT record_id FROM learning_anchors WHERE kind = 'nl'")}
        assert left == {keep}

    def test_a_non_colliding_hint_is_untouched(self, pg):
        """Anti-false-green: the merge must not rewrite rows that collide with
        nothing. Without this a merge that summed the whole table would pass
        every other test here."""
        rewind_to_v20(pg)
        alone = hint(pg, domain="alone.test", evidence_count=7, success_count=3,
                     last_seen="2026-01-01T00:00:00+00:00")
        keep = hint(pg, domain=None, created_at="2026-01-01T00:00:00+00:00")
        hint(pg, domain=None, created_at="2026-02-01T00:00:00+00:00")

        before = dict(next(r for r in rows(pg) if r["id"] == alone))
        pg_schema.ensure_schema(pg)

        after = dict(next(r for r in rows(pg) if r["id"] == alone))
        assert after == before
        assert {r["id"] for r in rows(pg)} == {alone, keep}

    def test_two_orgs_with_identical_text_do_not_merge(self, pg):
        """org_id is in all three keys, so a second tenant's identical hint is
        a separate group. Merging across orgs would be the cross-org mutation
        this plan spent Stage 4 removing."""
        rewind_to_v20(pg)
        a = hint(pg, domain=None, org_id="org-A", evidence_count=1)
        b = hint(pg, domain=None, org_id="org-B", evidence_count=1)

        pg_schema.ensure_schema(pg)

        assert {r["id"] for r in rows(pg)} == {a, b}
        assert all(r["evidence_count"] == 1 for r in rows(pg))

"""Learning backfill: owner-linked rows via test_runs; ownerless rows → home org."""

import uuid

import pytest

pytestmark = pytest.mark.integration


def test_backfill_owner_linked_and_home_fallback(in_memory_em):
    conn = in_memory_em._writer_conn
    try:
        # A local test_runs in the SAME isolated schema so the unqualified join
        # resolves here (search_path = learning_test,public) without touching public.
        conn.execute("CREATE TABLE IF NOT EXISTS test_runs (run_id TEXT PRIMARY KEY, org_id TEXT)")
        wid = str(uuid.uuid4())
        conn.execute("INSERT INTO test_runs (run_id, org_id) VALUES (?, 'org-RUN')", (wid,))
        conn.execute(
            "INSERT INTO execution_records (workflow_id, timestamp, user_query, test_status) "
            "VALUES (?, datetime('now'), 'q', 'passed')", (wid,))
        # An ownerless anti-pattern (pre-tenancy).
        conn.execute(
            "INSERT INTO anti_patterns (failure_category, query_pattern, score, "
            " evidence_count, last_seen) VALUES ('A1', 'iterate rows', 0.9, 5, datetime('now'))")
        conn.commit()

        counts = in_memory_em.backfill_org_ids(home_org_id="org-HOME")
        with in_memory_em.read_conn() as c:
            er = c.execute(
                "SELECT org_id FROM execution_records WHERE workflow_id = ?", (wid,)
            ).fetchone()
            ap = c.execute(
                "SELECT org_id FROM anti_patterns WHERE query_pattern = 'iterate rows'"
            ).fetchone()
        assert er["org_id"] == "org-RUN", "owner-linked row not attributed via test_runs"
        assert ap["org_id"] == "org-HOME", "ownerless row not attributed to home org"

        # Idempotent: a second pass changes nothing.
        counts2 = in_memory_em.backfill_org_ids(home_org_id="org-HOME")
        assert sum(counts2.values()) == 0, (
            "second backfill pass should update 0 rows (all WHERE org_id IS NULL guards fired)"
        )
    finally:
        # Drop the local test_runs so the shared session schema stays clean.
        try:
            conn.execute("DROP TABLE IF EXISTS test_runs")
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass

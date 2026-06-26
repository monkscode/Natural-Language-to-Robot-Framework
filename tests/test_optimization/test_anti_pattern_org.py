"""anti_patterns retrieval does not cross orgs."""

import uuid

import pytest

pytestmark = pytest.mark.integration


def _seed_anti(em, org_id, query="iterate over all rows in the table"):
    # Seed above the injection gate (score >= 0.4 AND evidence >= 3) directly so
    # get_hints will surface it; mirror the real columns.
    em._writer_conn.execute(
        "INSERT INTO anti_patterns "
        "(failure_category, query_pattern, bad_code_snippet, error_message, domain, "
        " org_id, score, evidence_count, last_seen) "
        "VALUES ('A1', ?, 'Get Text', 'only first row', 'a.test', ?, 0.9, 5, datetime('now'))",
        (query, org_id),
    )
    em._writer_conn.commit()
    # Anchor so the similarity filter can match it (kind='anti').
    cur = em._writer_conn.execute(
        "SELECT id FROM anti_patterns WHERE org_id = ? ORDER BY id DESC LIMIT 1", (org_id,))
    aid = cur.fetchone()["id"]
    em.add_anchor("anti", aid, query, org_id=org_id)


def test_anti_pattern_not_shared_across_orgs(em_vec):
    from src.backend.crew_ai.optimization.anti_pattern_engine import AntiPatternEngine
    _seed_anti(em_vec, "org-A")
    engine = AntiPatternEngine(execution_memory=em_vec)
    q = "iterate over all rows in the table"
    assert not engine.get_hints(q, "https://a.test", "planner", org_id="org-B"), \
        "org B received org A's anti-pattern"
    assert engine.get_hints(q, "https://a.test", "planner", org_id="org-A"), \
        "org A should see its own anti-pattern"


def _seed_anti_null_anchor(em, org_id, query="iterate over all rows in the table"):
    """Seed an anti-pattern owned by org_id but attach a NULL-org anchor.

    Guard (1): the gated SELECT's ``AND org_id = ?`` clause.
    Guard (2): filter_by_query_similarity(org_id=X) matches
               ``(org_id=X OR org_id IS NULL)``.

    Because the anchor's org_id is NULL, guard (2) passes for ANY calling
    org — the anchor is not a blocker.  Only guard (1) (the SQL clause) can
    still prevent a cross-org leak.  This helper exists solely to isolate
    guard (1) in test_anti_pattern_sql_clause_blocks_when_anchor_is_org_less.
    """
    em._writer_conn.execute(
        "INSERT INTO anti_patterns "
        "(failure_category, query_pattern, bad_code_snippet, error_message, domain, "
        " org_id, score, evidence_count, last_seen) "
        "VALUES ('A1', ?, 'Get Text', 'only first row', 'a.test', ?, 0.9, 5, datetime('now'))",
        (query, org_id),
    )
    em._writer_conn.commit()
    cur = em._writer_conn.execute(
        "SELECT id FROM anti_patterns WHERE org_id = ? ORDER BY id DESC LIMIT 1",
        (org_id,),
    )
    aid = cur.fetchone()["id"]
    # org_id=None on the anchor so filter_by_query_similarity passes for every org
    em.add_anchor("anti", aid, query, org_id=None)


def test_anti_pattern_sql_clause_blocks_when_anchor_is_org_less(em_vec):
    """Guard (1) — the gated SELECT's AND org_id = ? — is independently load-bearing.

    Setup
    -----
    Seed an org-A anti-pattern that satisfies the injection gate
    (score=0.9, evidence=5) but whose learning_anchor has org_id=NULL.

    Why the anchor alone cannot block the leak
    ------------------------------------------
    filter_by_query_similarity(org_id="org-B") uses the ChromaDB where-clause
    ``{$or: [{org_id: org-B}, {org_id: null}]}``.  A NULL-org anchor satisfies
    the second branch, so the anchor filter PASSES for an org-B caller.

    What MUST block org-B
    ---------------------
    Only the ``AND org_id = ?`` added to the SQL SELECT in Task 6:
        ``SELECT * FROM anti_patterns WHERE score >= ? AND evidence_count >= ?
          AND org_id = ?``
    Without that clause the NULL anchor would let org-B see org-A's row.
    """
    from src.backend.crew_ai.optimization.anti_pattern_engine import AntiPatternEngine
    _seed_anti_null_anchor(em_vec, "org-A")
    engine = AntiPatternEngine(execution_memory=em_vec)
    q = "iterate over all rows in the table"
    assert not engine.get_hints(q, "https://a.test", "planner", org_id="org-B"), (
        "CROSS-ORG LEAK: org-B received org-A's anti-pattern even though only "
        "the SQL AND org_id = ? clause (guard 1) could block it"
    )
    assert engine.get_hints(q, "https://a.test", "planner", org_id="org-A"), \
        "org-A should see its own anti-pattern"


def test_correct_alternative_does_not_cross_orgs(em_vec):
    """Regression: _check_for_correct_alternative must not write org-A's
    robot_code into org-B's anti_pattern row.

    Reproduces the cross-org leak in _check_for_correct_alternative where
    _find_matching_anti_patterns was called without org_id, allowing a
    passing run from org-A to update org-B's correct_alternative field.
    """
    from src.backend.crew_ai.optimization.anti_pattern_engine import AntiPatternEngine
    from src.backend.crew_ai.optimization.execution_memory import ExecutionRecord

    SHARED_QUERY = "iterate over all rows in the table"

    # Seed org-B's anti-pattern above the injection gate, correct_alternative NULL.
    # bad_code_snippet is 'Get Text' (single keyword, not a full line match)
    # so _bad_snippet_present will NOT block the update when robot_code differs.
    _seed_anti(em_vec, "org-B", query=SHARED_QUERY)

    # Confirm org-B's row starts with correct_alternative NULL.
    row = em_vec._writer_conn.execute(
        "SELECT correct_alternative FROM anti_patterns WHERE org_id = 'org-B'"
    ).fetchone()
    assert row is not None, "org-B anti-pattern not seeded"
    assert row["correct_alternative"] is None, "pre-condition: correct_alternative should be NULL"

    engine = AntiPatternEngine(execution_memory=em_vec)

    # Build a passing ExecutionRecord for org-A with the same query.
    # robot_code deliberately does NOT contain 'Get Text' so the
    # _bad_snippet_present guard would otherwise allow the update.
    record = ExecutionRecord(
        workflow_id=str(uuid.uuid4()),
        timestamp=__import__("datetime").datetime.utcnow(),
        user_query=SHARED_QUERY,
        url="https://a.test",
        domain="a.test",
        robot_code="*** Test Cases ***\nIterate Rows\n    Click    //button",
        test_status="passed",
        org_id="org-A",
    )

    engine.learn(record)

    # org-B's correct_alternative must still be NULL — org-A must not have
    # contaminated it.
    row_after = em_vec._writer_conn.execute(
        "SELECT correct_alternative FROM anti_patterns WHERE org_id = 'org-B'"
    ).fetchone()
    assert row_after["correct_alternative"] is None, (
        "CROSS-ORG LEAK: org-A's robot_code was written into org-B's "
        f"anti_pattern row: {row_after['correct_alternative']!r}"
    )

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

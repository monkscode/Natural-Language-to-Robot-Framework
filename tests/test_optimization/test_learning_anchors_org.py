"""learning_anchors carries org; similarity filter is org-aware (NULL = shared)."""

import pytest

pytestmark = pytest.mark.integration


def test_similarity_filter_scopes_by_org(em_vec):
    em_vec.add_anchor("nl", 101, "login as admin", org_id="org-A")
    em_vec.add_anchor("nl", 202, "login as admin", org_id="org-B")
    em_vec.add_anchor("nl", 303, "login as admin", org_id=None)   # shared/admin
    survivors = em_vec.filter_by_query_similarity(
        "login as admin", [101, 202, 303], kind="nl", org_id="org-A",
    )
    assert 101 in survivors            # own org
    assert 303 in survivors            # org-less (shared) matches any org
    assert 202 not in survivors        # other org excluded

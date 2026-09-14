"""An org that still owns tests cannot be deleted.

The guard is a BLOCK trigger, not a sweep: it raises rather than cascading, so
deleting an org with live data is refused. `tests` carries org_id and therefore
qualifies; test_versions does not carry one and is covered by ON DELETE CASCADE
from tests instead.
"""


def test_tests_is_named_in_the_guard_array():
    from src.backend.auth.org_db import _ORG_DELETE_GUARD_FN_DDL
    assert "'tests'" in _ORG_DELETE_GUARD_FN_DDL


def test_test_versions_is_not_named_because_it_has_no_org_id():
    """Adding it would be silently skipped by the pg_attribute check — a
    no-op that reads like coverage."""
    from src.backend.auth.org_db import _ORG_DELETE_GUARD_FN_DDL
    assert "'test_versions'" not in _ORG_DELETE_GUARD_FN_DDL

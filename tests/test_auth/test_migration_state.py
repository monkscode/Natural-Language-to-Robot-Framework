"""One-shot data-migration markers: a migration runs once, then is skipped.

The pre-tenancy backfills (personal-org provisioning, data org_id attribution)
must not re-run on every boot. These tests cover the marker primitive and prove
init_org_db records the personal-org backfill so a later boot skips it.
"""

import pytest

from src.backend.auth.migration_state import (
    is_migration_done,
    mark_migration_done,
    run_migration_once,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


def test_unseen_migration_is_not_done():
    assert is_migration_done("never_recorded_migration") is False


def test_mark_then_is_done():
    name = "unit_test_migration_alpha"
    assert is_migration_done(name) is False
    mark_migration_done(name)
    assert is_migration_done(name) is True


def test_mark_is_idempotent():
    name = "unit_test_migration_beta"
    mark_migration_done(name)
    mark_migration_done(name)  # ON CONFLICT DO NOTHING — no error, no duplicate
    assert is_migration_done(name) is True


def test_init_org_db_records_personal_org_backfill():
    """The auth_isolated_schema fixture runs init_org_db(), which gates and marks
    the personal-org backfill — so a subsequent boot skips it."""
    assert is_migration_done("personal_org_backfill") is True


def test_run_migration_once_runs_then_skips():
    name = "unit_test_run_once_gamma"
    calls = []
    assert run_migration_once(name, lambda: calls.append(1)) is True
    assert is_migration_done(name) is True
    # Second call sees the marker under the advisory lock and skips the body.
    assert run_migration_once(name, lambda: calls.append(1)) is False
    assert calls == [1]


def test_run_migration_once_marks_only_after_success():
    name = "unit_test_run_once_delta"

    def boom():
        raise RuntimeError("backfill failed")

    with pytest.raises(RuntimeError, match="backfill failed"):
        run_migration_once(name, boom)
    # A failed migration leaves the marker unset so the next boot retries.
    assert is_migration_done(name) is False

"""main.startup_event builds the RunRegistry at boot, and never fails on it.

RunRegistry.__init__ executes _SCHEMA_DDL, which carries the one-shot
test-split collapse. Before this, the only startup path that constructed the
registry was the data_org_id_backfill migration, which run_migration_once
calls only while its marker is unset — so on a database that has already
completed that migration the collapse ran inside the first request to touch
History, groups or a report authorization.

These tests stub every other startup dependency (they are startup wiring, not
storage): what is pinned is that the construction happens, that it happens
after the org backfill, and that a failure is swallowed.

Referenced by: src/backend/main.py.
Depends on: src/backend/core/run_registry.py (get_run_registry).
"""

import asyncio
from unittest.mock import MagicMock, patch

from src.backend import main


def _run_startup(calls, registry_side_effect):
    """Drive startup_event with every dependency but the registry stubbed."""
    with (
        patch("src.backend.auth.security_posture.validate_security_posture"),
        patch("src.backend.core.artifact_store.get_artifact_store"),
        patch.object(main, "_check_learning_health"),
        patch.object(main, "init_auth_db"),
        patch.object(main, "init_org_db"),
        patch.object(main, "init_invitations_db"),
        patch("src.backend.core.audit_log.init_audit_log"),
        patch("src.backend.auth.admin_seed.seed_platform_admins"),
        patch("src.backend.core.org_backfill.backfill_data_org_ids"),
        patch(
            "src.backend.auth.migration_state.run_migration_once",
            side_effect=lambda *a, **k: calls.append("org_backfill") or False,
        ),
        patch(
            "src.backend.core.run_registry.get_run_registry",
            side_effect=registry_side_effect,
        ),
        patch(
            "src.backend.core.temp_metrics_storage.get_temp_metrics_storage",
            side_effect=lambda: calls.append("temp_cleanup") or MagicMock(),
        ),
    ):
        asyncio.run(main.startup_event())


def test_startup_constructs_the_registry_after_the_org_backfill():
    """The registry is built at boot, and after the backfill — not before it.

    The order is load-bearing: backfill_org_ids repairs org_id on runs AND on
    tests, and it constructs the registry itself when its marker is unset.
    """
    calls = []
    _run_startup(calls, lambda: calls.append("run_registry") or MagicMock())
    assert calls == ["org_backfill", "run_registry", "temp_cleanup"]


def test_startup_survives_a_registry_construction_failure():
    """Postgres down must not block boot: the failure is logged and startup
    carries on to the blocks after it."""
    calls = []
    _run_startup(calls, RuntimeError("postgres unreachable"))
    assert calls == ["org_backfill", "temp_cleanup"]

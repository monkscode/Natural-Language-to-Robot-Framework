"""main.startup_event builds the RunRegistry at boot, and never fails on it.

RunRegistry.__init__ executes _SCHEMA_DDL, which carries the one-shot
test-split collapse. Before this, the only startup path that constructed the
registry was the data_org_id_backfill migration, which run_migration_once
calls only while its marker is unset — so on a database that has already
completed that migration the collapse ran inside the first request to touch
History, groups or a report authorization.

These tests stub every other startup dependency (they are startup wiring, not
storage), including the abandoned-run sweeper: it reaches the registry through
workflow_service, not the patched get_run_registry, so unstubbed it would start
for real. What is pinned is that the construction happens, that it happens
after the org backfill, that a failure is swallowed, and that startup launches
the sweeper as a task on app.state.

Referenced by: src/backend/main.py.
Depends on: src/backend/core/run_registry.py (get_run_registry).
"""

import asyncio
import logging
from unittest.mock import MagicMock, patch

from src.backend import main


def _run_startup(calls, registry_side_effect, sweeps=None, migrations=None,
                 outcomes=None):
    """Drive startup_event with every dependency but the registry stubbed.

    outcomes maps a migration name to what run_migration_once returns for it
    (an exception instance is raised instead); a name not in it returns False.

    Returns the task startup left on app.state.abandoned_run_sweeper, captured
    before asyncio.run closes the loop and cancels it."""
    sweeps = [] if sweeps is None else sweeps
    migrations = {} if migrations is None else migrations
    outcomes = {} if outcomes is None else outcomes

    def stub_migration(name, fn, *a, **k):
        calls.append(name)
        migrations[name] = fn
        outcome = outcomes.get(name, False)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def stub_sweeper():
        sweeps.append(1)

    async def go():
        await main.startup_event()
        return getattr(main.app.state, "abandoned_run_sweeper", None)

    with (
        patch("src.backend.auth.security_posture.validate_security_posture"),
        patch("src.backend.core.artifact_store.get_artifact_store"),
        patch("src.backend.crew_ai.model_map_fallback.ensure_model_entry"),
        patch.object(main, "_check_learning_health"),
        patch.object(main, "init_auth_db"),
        patch.object(main, "init_org_db"),
        patch.object(main, "init_invitations_db"),
        patch("src.backend.core.audit_log.init_audit_log"),
        patch("src.backend.core.org_backfill.backfill_data_org_ids"),
        patch(
            "src.backend.auth.migration_state.run_migration_once",
            side_effect=stub_migration,
        ),
        patch(
            "src.backend.core.run_registry.get_run_registry",
            side_effect=registry_side_effect,
        ),
        patch(
            "src.backend.services.workflow_service.sweep_abandoned_runs_forever",
            stub_sweeper,
        ),
        patch(
            "src.backend.core.temp_metrics_storage.get_temp_metrics_storage",
            side_effect=lambda: calls.append("temp_cleanup") or MagicMock(),
        ),
    ):
        return asyncio.run(go())


def test_startup_constructs_the_registry_after_the_org_backfill():
    """The registry is built at boot, and after the backfill — not before it.

    The order is load-bearing: backfill_org_ids repairs org_id on runs AND on
    tests, and it constructs the registry itself when its marker is unset.
    """
    calls = []
    _run_startup(calls, lambda: calls.append("run_registry") or MagicMock())
    assert calls == ["data_org_id_backfill", "run_registry",
                     "result_folder_alignment", "temp_cleanup"]


def test_startup_survives_a_registry_construction_failure():
    """Postgres down must not block boot: the failure is logged and startup
    carries on to the blocks after it."""
    calls = []
    _run_startup(calls, RuntimeError("postgres unreachable"))
    assert calls == ["data_org_id_backfill", "result_folder_alignment",
                     "temp_cleanup"]


def test_startup_launches_the_abandoned_run_sweeper_as_a_task():
    sweeps = []
    task = _run_startup([], lambda: MagicMock(), sweeps)
    assert sweeps == [1]
    assert isinstance(task, asyncio.Task)


def test_startup_aligns_result_folders_once_through_the_migration_marker():
    """The alignment goes through run_migration_once under its own marker
    name, after the registry is built, and its migrate callable is the
    registry method -- so a boot that finds the marker set never runs it."""
    calls, migrations = [], {}
    registry = MagicMock()
    _run_startup(calls, lambda: registry, migrations=migrations)
    assert calls.count("result_folder_alignment") == 1
    registry.align_result_folders.assert_not_called()  # the marker decides
    migrations["result_folder_alignment"]()
    registry.align_result_folders.assert_called_once_with()


def test_startup_logs_the_alignment_as_applied_when_the_marker_was_unset(caplog):
    """run_migration_once returns True only on the boot that ran the migration;
    that boot says so, and does not claim the alignment was already applied."""
    with caplog.at_level(logging.INFO):
        _run_startup([], lambda: MagicMock(),
                     outcomes={"result_folder_alignment": True})
    messages = [r.getMessage() for r in caplog.records]
    assert "[RUN_REGISTRY] result folder alignment applied" in messages
    assert not any("result folder alignment already applied" in m
                   for m in messages)


def test_startup_survives_a_result_folder_alignment_failure(caplog):
    """A failing alignment (marker left unset, the next boot retries) is
    logged as a WARNING and never blocks the steps after it."""
    calls = []
    sweeps = []
    with caplog.at_level(logging.INFO):
        task = _run_startup(
            calls, lambda: MagicMock(), sweeps,
            outcomes={"result_folder_alignment": RuntimeError("postgres unreachable")})
    warnings = [r.getMessage() for r in caplog.records
                if r.levelno == logging.WARNING]
    assert any("result folder alignment skipped: postgres unreachable" in m
               for m in warnings)
    assert calls == ["data_org_id_backfill", "result_folder_alignment",
                     "temp_cleanup"]
    assert sweeps == [1]
    assert isinstance(task, asyncio.Task)


def test_startup_warns_when_admin_emails_is_set(caplog):
    """ADMIN_EMAILS no longer makes anyone an admin; a boot that still finds it
    set says so and names the host command, and a boot without it stays quiet."""
    with caplog.at_level(logging.INFO):
        with patch.object(main.settings, "ADMIN_EMAILS", "x@example.com"):
            _run_startup([], lambda: MagicMock())
    warnings = [r.getMessage() for r in caplog.records
                if r.levelno == logging.WARNING]
    assert any("tools.make_admin" in m for m in warnings)

    caplog.clear()
    with caplog.at_level(logging.INFO):
        with patch.object(main.settings, "ADMIN_EMAILS", ""):
            _run_startup([], lambda: MagicMock())
    assert not any("tools.make_admin" in r.getMessage() for r in caplog.records)

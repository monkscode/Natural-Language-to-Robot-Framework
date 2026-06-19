"""One-shot, idempotent backfill of org_id across the data tables (Phase 1b).

Runs at startup after init_org_db(): maps every pre-tenancy test_runs / llm_traces
/ workflow_metrics row to its owner's personal org. Best-effort — each table's
backfill is independent and swallows its own errors so a partial failure never
blocks boot. A no-op once every row carries an org_id.

Referenced by: main.py (startup_event).
Depends on: core/run_registry, core/trace_store, core/workflow_metrics.
"""
import logging

logger = logging.getLogger(__name__)


def backfill_data_org_ids() -> dict[str, int]:
    """Backfill org_id on pre-tenancy rows in all three data tables.

    Each table's backfill runs independently: one failure does not abort the
    others. Returns a dict of row counts updated per table.
    """
    out: dict[str, int] = {}

    try:
        from src.backend.core.run_registry import get_run_registry
        out["test_runs"] = get_run_registry().backfill_org_ids()
    except Exception as e:
        logger.warning("[ORG_BACKFILL] test_runs backfill failed: %s", e)
        out["test_runs"] = 0

    try:
        from src.backend.core.trace_store import get_trace_store
        store = get_trace_store()
        out["llm_traces"] = store.backfill_org_ids() if store else 0
    except Exception as e:
        logger.warning("[ORG_BACKFILL] llm_traces backfill failed: %s", e)
        out["llm_traces"] = 0

    try:
        from src.backend.core.workflow_metrics import get_workflow_metrics_collector
        out["workflow_metrics"] = get_workflow_metrics_collector().backfill_org_ids()
    except Exception as e:
        logger.warning("[ORG_BACKFILL] workflow_metrics backfill failed: %s", e)
        out["workflow_metrics"] = 0

    logger.info("[ORG_BACKFILL] data org_id backfill: %s", out)
    return out

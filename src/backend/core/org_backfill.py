"""One-shot, idempotent backfill of org_id across the data tables (Phase 1b/1c).

Runs at startup after init_org_db(): maps every pre-tenancy test_runs / llm_traces
/ workflow_metrics row to its owner's personal org (Phase 1b), and attributes
pre-tenancy learning rows (Phase 1c — Task 11). Best-effort — each table's
backfill is independent and swallows its own errors so a partial failure never
blocks boot. A no-op once every row carries an org_id.

Referenced by: main.py (startup_event).
Depends on: core/run_registry, core/trace_store, core/workflow_metrics,
            crew_ai/optimization/learning_registry, crew_ai/optimization/keyword_vector_store.
"""
import logging

logger = logging.getLogger(__name__)


def _home_org_id() -> str | None:
    """The oldest personal org — the single tenant whose learning predates
    tenancy. Ownerless legacy learning rows attribute here so the current org
    keeps the patterns it already had."""
    try:
        from src.backend.auth.db import get_pool
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT id FROM organizations WHERE kind = 'personal' "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
        return str(row["id"]) if row else None
    except Exception as e:
        logger.warning("[ORG_BACKFILL] home-org resolve failed: %s", e)
        return None


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

    home = _home_org_id()
    try:
        from src.backend.crew_ai.optimization.learning_registry import get_feedback_loop
        fb = get_feedback_loop()
        out["learning"] = fb.execution_memory.backfill_org_ids(home) if fb else {}
    except Exception as e:
        logger.warning("[ORG_BACKFILL] learning backfill failed: %s", e)
        out["learning"] = {}

    try:
        from src.backend.crew_ai.optimization.keyword_vector_store import get_keyword_vector_store
        out["kw_query_patterns"] = get_keyword_vector_store().backfill_org_ids(home)
    except Exception as e:
        logger.warning("[ORG_BACKFILL] kw_query_patterns backfill failed: %s", e)
        out["kw_query_patterns"] = 0

    logger.info("[ORG_BACKFILL] data org_id backfill: %s", out)
    return out

"""
Admin API for querying LLM traces stored in the Postgres trace store.

Provides endpoints for debugging generation failures:
- GET /api/admin/traces/              — list traces with optional filtering
- GET /api/admin/traces/{span_id}     — full detail for one span (includes prompt/response)
- GET /api/admin/traces/workflow/{id} — all LLM calls for a single workflow in order
- GET /api/admin/traces/stats/cost    — aggregate cost and token usage by model

Queries use SQLite-dialect SQL (`?` placeholders, sqlite3.Row access) translated
to Postgres by the pg_compat adapter, so the endpoint SQL is unchanged from the
former SQLite store.

Referenced by: main.py (router registration with prefix="/api")
Depends on: core/trace_store.py (Postgres schema), crew_ai/optimization/pg_compat
"""
import logging
from contextlib import closing
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg_pool import ConnectionPool

from src.backend.auth.jwt_utils import is_validated_admin, require_user
from src.backend.auth.ownership import is_dashboard_viewer
from src.backend.core.config import PG_CONNECT_TIMEOUT_S, settings
from src.backend.crew_ai.optimization import pg_compat

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/traces", tags=["traces"])

# Read-only connection pool for the trace dashboards. These are low-QPS admin
# endpoints, but opening a brand-new psycopg connection per request (the former
# behaviour) pays a TCP+auth handshake every time and is unbounded under burst.
# A small pool recycles a handful of autocommit connections instead. Rows use the
# pg_compat factory so the endpoints keep their `?` placeholders + dict-style row
# access unchanged.
_read_pool: ConnectionPool | None = None
_read_pool_lock = Lock()


def _make_read_pool(dsn: str) -> ConnectionPool:
    """Build a compat-row, autocommit read pool for the trace store at `dsn`."""
    return ConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=4,
        kwargs={
            "row_factory": pg_compat.compat_row,
            "autocommit": True,  # read-only SELECTs — no transaction to manage
            "connect_timeout": PG_CONNECT_TIMEOUT_S,
        },
        open=True,
    )


def _get_read_pool() -> ConnectionPool:
    """Lazily create the process-wide read pool (double-checked locking)."""
    global _read_pool
    if _read_pool is None:
        with _read_pool_lock:
            if _read_pool is None:
                _read_pool = _make_read_pool(settings.DATABASE_URL)
    return _read_pool


def close_read_pool() -> None:
    """Close the read pool on shutdown (no-op if never created)."""
    global _read_pool
    if _read_pool is not None:
        _read_pool.close()
        _read_pool = None


class _PooledCompatConnection(pg_compat.CompatConnection):
    """A pg_compat connection checked out from a pool. close() RETURNS it to the
    pool instead of closing it, so the endpoints' `with closing(_get_db())`
    recycles the connection rather than dropping it."""

    def __init__(self, pool: ConnectionPool):
        self._pool = pool
        super().__init__(pool.getconn())

    def close(self) -> None:
        self._pool.putconn(self._real)


def _get_db():
    """Check out a pooled trace-store connection (SQLite-dialect via pg_compat).

    Maps "the llm_traces table does not exist yet" to FileNotFoundError so the
    endpoints' existing graceful-degradation branches fire unchanged (no traces
    have been recorded yet). The endpoint SQL keeps its `?` placeholders and
    sqlite3.Row-style access through the compat adapter.
    """
    conn = _PooledCompatConnection(_get_read_pool())
    # to_regclass respects the connection's search_path — an llm_traces table
    # in some other schema (e.g. an isolated test schema) neither hides nor
    # fakes the one this connection would actually query.
    try:
        exists = conn.execute("SELECT to_regclass('llm_traces')").fetchone()[0]
    except Exception:
        conn.close()  # return the connection to the pool before propagating
        raise
    if not exists:
        conn.close()
        raise FileNotFoundError("Trace table not found. No traces have been recorded yet.")
    return conn


@router.get("/")
async def list_traces(
    workflow_id: Optional[str] = Query(None, description="Filter by workflow ID"),
    model: Optional[str] = Query(None, description="Filter by model name (partial match)"),
    status: Optional[str] = Query(None, description="Filter by status: OK or ERROR"),
    llm_only: bool = Query(True, description="When true (default), only return LLM call spans (model IS NOT NULL). Set false to include all span types (HTTP, ChromaDB, etc.)"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict | None = Depends(require_user),
):
    """List LLM call traces with optional filtering. Returns metadata only (no prompt/response text)."""
    admin = is_validated_admin(user)
    if not is_dashboard_viewer(user, is_platform_admin=admin):
        raise HTTPException(status_code=403, detail="Org-admin access required")
    scope_org = None if (admin or user is None) else user.get("org_id")
    try:
        with closing(_get_db()) as conn:
            query = (
                "SELECT id, trace_id, name, duration_ms, status, model, "
                "prompt_tokens, completion_tokens, cost_usd, workflow_id, created_at "
                "FROM llm_traces WHERE 1=1"
            )
            params: list = []

            if llm_only:
                # Only *.litellm spans have reliable token/cost data.
                # OTel agent spans (e.g. "Test Automation Planner.agent") have model set
                # but tokens=0/cost=0 due to the Vertex AI instrumentation bug.
                query += " AND name LIKE '%.litellm'"
            if scope_org is not None:
                query += " AND org_id = ?"
                params.append(scope_org)
            if workflow_id:
                query += " AND workflow_id = ?"
                params.append(workflow_id)
            if model:
                query += " AND model LIKE ?"
                params.append(f"%{model}%")
            if status:
                query += " AND status = ?"
                params.append(status.upper())

            query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            rows = conn.execute(query, params).fetchall()
        return {"traces": [dict(r) for r in rows], "limit": limit, "offset": offset}
    except FileNotFoundError as e:
        logger.info("Trace database unavailable in list_traces: %s", e)
        return {
            "traces": [],
            "limit": limit,
            "offset": offset,
            "note": "Trace database is not available yet. No traces have been recorded.",
        }
    except Exception as e:
        logger.exception("Unexpected error in list_traces")
        raise HTTPException(status_code=500, detail="An internal error occurred.") from e


@router.get("/stats/cost")
async def get_cost_stats(
    last_days: int = Query(7, ge=1, le=90, description="Cost stats for last N days"),
    user: dict | None = Depends(require_user),
):
    """Aggregate cost and token usage per model for the last N days."""
    admin = is_validated_admin(user)
    if not is_dashboard_viewer(user, is_platform_admin=admin):
        raise HTTPException(status_code=403, detail="Org-admin access required")
    scope_org = None if (admin or user is None) else user.get("org_id")
    try:
        with closing(_get_db()) as conn:
            # tz-aware datetime → psycopg adapts to timestamptz (correct regardless
            # of the server's session TZ); created_at is now timestamptz.
            cutoff = datetime.now(tz=timezone.utc) - timedelta(days=last_days)

            org_clause = " AND org_id = ?" if scope_org is not None else ""
            org_params = [scope_org] if scope_org is not None else []

            row = conn.execute(
                f"""SELECT
                     COUNT(*) as total_llm_calls,
                     SUM(cost_usd) as total_cost,
                     SUM(prompt_tokens) as total_prompt_tokens,
                     SUM(completion_tokens) as total_completion_tokens,
                     AVG(duration_ms) as avg_latency_ms,
                     COUNT(DISTINCT workflow_id) as total_workflows
                   FROM llm_traces
                   WHERE created_at >= ? AND model IS NOT NULL{org_clause}""",
                [cutoff] + org_params,
            ).fetchone()

            model_rows = conn.execute(
                f"""SELECT model,
                     COUNT(*) as calls,
                     SUM(cost_usd) as cost,
                     SUM(total_tokens) as tokens,
                     AVG(duration_ms) as avg_latency_ms
                   FROM llm_traces
                   WHERE created_at >= ? AND model IS NOT NULL{org_clause}
                   GROUP BY model
                   ORDER BY cost DESC""",
                [cutoff] + org_params,
            ).fetchall()

        return {
            "period_days": last_days,
            "total_llm_calls": row["total_llm_calls"],
            "total_cost_usd": round(row["total_cost"] or 0, 6),
            "total_prompt_tokens": row["total_prompt_tokens"] or 0,
            "total_completion_tokens": row["total_completion_tokens"] or 0,
            "avg_latency_ms": round(row["avg_latency_ms"] or 0, 2),
            "total_workflows": row["total_workflows"],
            "per_model": [dict(r) for r in model_rows],
        }
    except FileNotFoundError as e:
        logger.info("Trace database unavailable in get_cost_stats: %s", e)
        return {
            "period_days": last_days,
            "note": "Trace database not found. No traces have been recorded yet.",
            "total_llm_calls": 0,
        }
    except Exception as e:
        logger.exception("Unexpected error in get_cost_stats")
        raise HTTPException(status_code=500, detail="An internal error occurred.") from e


@router.get("/workflow/{workflow_id}")
async def get_workflow_traces(
    workflow_id: str,
    user: dict | None = Depends(require_user),
):
    """
    Get all LLM calls for a single workflow, ordered by start time.

    Primary debugging view: shows the full chain of LLM calls for one test
    generation, including agent name, prompt, response, tokens, cost, latency.

    Query strategy: finds the trace_id for this workflow_id from the root span,
    then fetches ALL spans with that trace_id. This captures child spans even if
    OTel Baggage propagation missed setting workflow_id on them.
    """
    admin = is_validated_admin(user)
    if not is_dashboard_viewer(user, is_platform_admin=admin):
        raise HTTPException(status_code=403, detail="Org-admin access required")
    scope_org = None if (admin or user is None) else user.get("org_id")
    try:
        with closing(_get_db()) as conn:
            if scope_org is not None:
                trace_row = conn.execute(
                    "SELECT trace_id FROM llm_traces WHERE workflow_id = ? AND org_id = ? LIMIT 1",
                    (workflow_id, scope_org),
                ).fetchone()
            else:
                trace_row = conn.execute(
                    "SELECT trace_id FROM llm_traces WHERE workflow_id = ? LIMIT 1",
                    (workflow_id,),
                ).fetchone()

            if not trace_row:
                return {"workflow_id": workflow_id, "llm_calls": 0, "traces": []}

            trace_id = trace_row["trace_id"]
            rows = conn.execute(
                """SELECT id, trace_id, parent_span_id, name, duration_ms, status,
                          model, prompt_tokens, completion_tokens, total_tokens,
                          cost_usd, prompt_text, response_text, created_at
                   FROM llm_traces
                   WHERE trace_id = ?
                   ORDER BY start_time_ns ASC""",
                (trace_id,),
            ).fetchall()

        traces = [dict(r) for r in rows]
        total_cost = sum(t.get("cost_usd") or 0 for t in traces)
        total_tokens = sum(t.get("total_tokens") or 0 for t in traces)
        total_duration = sum(t.get("duration_ms") or 0 for t in traces)

        return {
            "workflow_id": workflow_id,
            "llm_calls": len(traces),
            "total_cost_usd": round(total_cost, 6),
            "total_tokens": total_tokens,
            "total_duration_ms": round(total_duration, 2),
            "traces": traces,
        }
    except FileNotFoundError as e:
        logger.info("Trace database unavailable in get_workflow_traces: %s", e)
        return {
            "workflow_id": workflow_id,
            "llm_calls": 0,
            "traces": [],
            "note": "Trace data is currently unavailable.",
        }
    except Exception as e:
        logger.exception("Unexpected error in get_workflow_traces")
        raise HTTPException(status_code=500, detail="An internal error occurred.") from e


@router.get("/{span_id}")
async def get_trace_detail(
    span_id: str,
    user: dict | None = Depends(require_user),
):
    """Get full trace detail for one span, including prompt and response text."""
    admin = is_validated_admin(user)
    if not is_dashboard_viewer(user, is_platform_admin=admin):
        raise HTTPException(status_code=403, detail="Org-admin access required")
    scope_org = None if (admin or user is None) else user.get("org_id")
    try:
        with closing(_get_db()) as conn:
            if scope_org is not None:
                row = conn.execute(
                    "SELECT * FROM llm_traces WHERE id = ? AND org_id = ?",
                    (span_id, scope_org),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM llm_traces WHERE id = ?", (span_id,)
                ).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Trace not found")
        return dict(row)
    except HTTPException:
        raise
    except FileNotFoundError as e:
        logger.info("Trace database unavailable in get_trace_detail: %s", e)
        raise HTTPException(status_code=404, detail="Trace store unavailable.") from e
    except Exception as e:
        logger.exception("Unexpected error in get_trace_detail")
        raise HTTPException(status_code=500, detail="An internal error occurred.") from e

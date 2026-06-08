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
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from src.backend.core.config import settings
from src.backend.crew_ai.optimization import pg_compat

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/traces", tags=["traces"])


def _get_db():
    """Open a connection to the Postgres trace store (SQLite-dialect via pg_compat).

    Maps "the llm_traces table does not exist yet" to FileNotFoundError so the
    endpoints' existing graceful-degradation branches fire unchanged (no traces
    have been recorded yet). The endpoint SQL keeps its `?` placeholders and
    sqlite3.Row-style access through the compat adapter.
    """
    conn = pg_compat.connect(settings.DATABASE_URL, autocommit=True)
    exists = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'llm_traces'"
    ).fetchone()
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
):
    """List LLM call traces with optional filtering. Returns metadata only (no prompt/response text)."""
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
):
    """Aggregate cost and token usage per model for the last N days."""
    try:
        with closing(_get_db()) as conn:
            # tz-aware datetime → psycopg adapts to timestamptz (correct regardless
            # of the server's session TZ); created_at is now timestamptz.
            cutoff = datetime.now(tz=timezone.utc) - timedelta(days=last_days)

            row = conn.execute(
                """SELECT
                     COUNT(*) as total_llm_calls,
                     SUM(cost_usd) as total_cost,
                     SUM(prompt_tokens) as total_prompt_tokens,
                     SUM(completion_tokens) as total_completion_tokens,
                     AVG(duration_ms) as avg_latency_ms,
                     COUNT(DISTINCT workflow_id) as total_workflows
                   FROM llm_traces
                   WHERE created_at >= ? AND model IS NOT NULL""",
                (cutoff,),
            ).fetchone()

            model_rows = conn.execute(
                """SELECT model,
                     COUNT(*) as calls,
                     SUM(cost_usd) as cost,
                     SUM(total_tokens) as tokens,
                     AVG(duration_ms) as avg_latency_ms
                   FROM llm_traces
                   WHERE created_at >= ? AND model IS NOT NULL
                   GROUP BY model
                   ORDER BY cost DESC""",
                (cutoff,),
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
async def get_workflow_traces(workflow_id: str):
    """
    Get all LLM calls for a single workflow, ordered by start time.

    Primary debugging view: shows the full chain of LLM calls for one test
    generation, including agent name, prompt, response, tokens, cost, latency.

    Query strategy: finds the trace_id for this workflow_id from the root span,
    then fetches ALL spans with that trace_id. This captures child spans even if
    OTel Baggage propagation missed setting workflow_id on them.
    """
    try:
        with closing(_get_db()) as conn:
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
async def get_trace_detail(span_id: str):
    """Get full trace detail for one span, including prompt and response text."""
    try:
        with closing(_get_db()) as conn:
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

"""
Admin API for querying LLM traces stored in the SQLite trace store.

Provides endpoints for debugging generation failures:
- GET /admin/traces/              — list traces with optional filtering
- GET /admin/traces/{span_id}     — full detail for one span (includes prompt/response)
- GET /admin/traces/workflow/{id} — all LLM calls for a single workflow in order
- GET /admin/traces/stats/cost    — aggregate cost and token usage by model

Referenced by: main.py (router registration)
Depends on: core/trace_store.py (SQLite schema), core/observability.py (db_path constant)
"""
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from src.backend.core.trace_store import TRACE_DB_PATH

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/traces", tags=["traces"])

_DB_PATH = TRACE_DB_PATH


def _get_db() -> sqlite3.Connection:
    """Open a read-only connection to the trace store.

    Raises FileNotFoundError if no traces have been written yet (DB does not exist).
    """
    if not os.path.exists(_DB_PATH):
        raise FileNotFoundError(f"Trace database not found at {_DB_PATH}. No traces have been recorded yet.")
    # Use absolute path — SQLite URI mode does not resolve relative paths on Windows.
    abs_path = os.path.abspath(_DB_PATH).replace("\\", "/")
    conn = sqlite3.connect(f"file:{abs_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
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
        conn = _get_db()
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
        conn.close()
        return {"traces": [dict(r) for r in rows], "limit": limit, "offset": offset}
    except FileNotFoundError as e:
        return {"traces": [], "limit": limit, "offset": offset, "note": str(e)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stats/cost")
async def get_cost_stats(
    last_days: int = Query(7, ge=1, le=90, description="Cost stats for last N days"),
):
    """Aggregate cost and token usage per model for the last N days."""
    try:
        conn = _get_db()
        cutoff = (datetime.now() - timedelta(days=last_days)).isoformat()

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
        conn.close()

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
        return {"period_days": last_days, "note": str(e), "total_llm_calls": 0}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
        conn = _get_db()

        trace_row = conn.execute(
            "SELECT trace_id FROM llm_traces WHERE workflow_id = ? LIMIT 1",
            (workflow_id,),
        ).fetchone()

        if not trace_row:
            conn.close()
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
        conn.close()

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
        return {"workflow_id": workflow_id, "llm_calls": 0, "traces": [], "note": str(e)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{span_id}")
async def get_trace_detail(span_id: str):
    """Get full trace detail for one span, including prompt and response text."""
    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT * FROM llm_traces WHERE id = ?", (span_id,)
        ).fetchone()
        conn.close()

        if not row:
            raise HTTPException(status_code=404, detail="Trace not found")
        return dict(row)
    except HTTPException:
        raise
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

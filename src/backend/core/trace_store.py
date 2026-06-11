"""Postgres span exporter — queryable trace storage for the learning/observability stack.

Stores OTel spans (including LLM prompt/response from OpenLLMetry) in PostgreSQL
(Phase 4 consolidation; replaces the former local SQLite trace store). Schema
stores the OTel span model flattened to columns for the fields we care about:
workflow_id, agent name, model, prompt, response, tokens, cost, latency.
Non-LLM spans are stored with NULL prompt/response.

Two write paths exist, with strict separation of concerns:
1. LiteLLM callback path — insert_litellm_call() is the AUTHORITATIVE writer
   for LLM calls (prompt, response, tokens, cost). Called from the LiteLLM
   success callback in cleaned_llm_wrapper.py.
2. OTel export path — SpanExporter.export() stores orchestration-only spans
   (crewai.agent, http client, chromadb). LLM spans are skipped via
   _is_llm_span() to avoid duplicate rows with the LiteLLM path.

Both paths share a single PostgresSpanExporter singleton (a small psycopg pool)
returned by get_trace_store(). observability._get_exporter() returns this same
singleton to the OTel BatchSpanProcessor. Postgres MVCC handles concurrent
readers/writers — no application-level lock needed.

Postgres-native (Phase 4): nanosecond timestamps are BIGINT (int4 would
overflow), created_at is TIMESTAMPTZ (native interval-based retention + range
index), and attributes_json is JSONB (queryable). Schema is created idempotently
on init.

Referenced by: core/observability.py (_get_exporter), api/trace_endpoints.py,
               crew_ai/cleaned_llm_wrapper.py (get_trace_store)
Depends on: opentelemetry-sdk, psycopg
"""
import json
import logging
import threading
from datetime import datetime, timezone

import psycopg
from psycopg_pool import ConnectionPool
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import StatusCode

from src.backend.core.config import settings

logger = logging.getLogger(__name__)

# Kept for import-compatibility with code/tests that referenced the old SQLite
# path constant. The trace store now lives in Postgres (settings.DATABASE_URL).
TRACE_DB_PATH = "postgres://llm_traces"

_DB_SCHEMA_PG = (
    """
    CREATE TABLE IF NOT EXISTS llm_traces (
        id                TEXT PRIMARY KEY,
        trace_id          TEXT NOT NULL,
        parent_span_id    TEXT,
        name              TEXT NOT NULL,
        start_time_ns     BIGINT NOT NULL,
        end_time_ns       BIGINT NOT NULL,
        duration_ms       DOUBLE PRECISION NOT NULL,
        status            TEXT DEFAULT 'OK',
        model             TEXT,
        prompt_text       TEXT,
        response_text     TEXT,
        prompt_tokens     INTEGER DEFAULT 0,
        completion_tokens INTEGER DEFAULT 0,
        total_tokens      INTEGER DEFAULT 0,
        cost_usd          DOUBLE PRECISION DEFAULT 0.0,
        workflow_id       TEXT,
        attributes_json   JSONB,
        created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_traces_trace_id   ON llm_traces(trace_id)",
    "CREATE INDEX IF NOT EXISTS idx_traces_workflow_id ON llm_traces(workflow_id)",
    "CREATE INDEX IF NOT EXISTS idx_traces_created     ON llm_traces(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_traces_model       ON llm_traces(model)",
)

_SCHEMA_VERSION = 2  # v2 = Postgres-native (BIGINT ns, timestamptz, jsonb)


def _ensure_schema(conn) -> None:
    """Create the trace schema if absent (idempotent). Takes a raw psycopg conn."""
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS trace_schema_version (v INTEGER PRIMARY KEY)")
        for ddl in _DB_SCHEMA_PG:
            cur.execute(ddl)
        cur.execute(
            "INSERT INTO trace_schema_version (v) VALUES (%s) ON CONFLICT DO NOTHING",
            (_SCHEMA_VERSION,))
    conn.commit()


# Identify LLM call spans by span name suffix, NOT by attribute presence.
# traceloop's CrewAI instrumentation also sets gen_ai.request.model on
# "<Agent>.agent" orchestration spans, so attr-based detection would
# incorrectly drop CrewAI agent hierarchy. Name suffix is the reliable signal:
#   LLM calls        → "openai.chat", "vertex_ai.chat", "bedrock.chat", "*.completion", "*.embeddings"
#   Orchestration    → "<Agent>.agent", "crewai.workflow", "crewai.task", HTTP, ChromaDB (all kept)
# ".llm" covers older Vertex AI instrumentation naming (e.g. "vertex_ai/gemini-2.5.llm").
# ".litellm" rows are written by the LiteLLM callback directly, not via the OTel path,
# so they're not in this list.
_LLM_SPAN_NAME_SUFFIXES = (".chat", ".completion", ".embeddings", ".llm")


def _is_llm_span(span_name: str) -> bool:
    """Return True if this span is an LLM call already covered by the LiteLLM callback."""
    name = (span_name or "").lower()
    return any(name.endswith(s) for s in _LLM_SPAN_NAME_SUFFIXES)


_INSERT_SQL = """
    INSERT INTO llm_traces
    (id, trace_id, parent_span_id, name, start_time_ns, end_time_ns,
     duration_ms, status, model, prompt_text, response_text,
     prompt_tokens, completion_tokens, total_tokens, cost_usd,
     workflow_id, attributes_json)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (id) DO UPDATE SET
        trace_id = EXCLUDED.trace_id, parent_span_id = EXCLUDED.parent_span_id,
        name = EXCLUDED.name, start_time_ns = EXCLUDED.start_time_ns,
        end_time_ns = EXCLUDED.end_time_ns, duration_ms = EXCLUDED.duration_ms,
        status = EXCLUDED.status, model = EXCLUDED.model,
        prompt_text = EXCLUDED.prompt_text, response_text = EXCLUDED.response_text,
        prompt_tokens = EXCLUDED.prompt_tokens, completion_tokens = EXCLUDED.completion_tokens,
        total_tokens = EXCLUDED.total_tokens, cost_usd = EXCLUDED.cost_usd,
        workflow_id = EXCLUDED.workflow_id, attributes_json = EXCLUDED.attributes_json
"""


class PostgresSpanExporter(SpanExporter):
    """Export OTel spans to PostgreSQL (pooled connections; MVCC handles concurrency)."""

    def __init__(self, dsn: str = None):
        self.dsn = dsn or settings.DATABASE_URL
        # Bootstrap the schema on a throwaway raw connection.
        setup = psycopg.connect(self.dsn, autocommit=False)
        try:
            _ensure_schema(setup)
        finally:
            setup.close()
        self._pool = ConnectionPool(conninfo=self.dsn, min_size=1, max_size=4, open=True)
        logger.info("[TRACE_STORE] Postgres trace store ready (schema v%d)", _SCHEMA_VERSION)

    # ------------------------------------------------------------------
    # OTel export path
    # ------------------------------------------------------------------

    def export(self, spans) -> SpanExportResult:
        try:
            rows = [r for span in spans if (r := self._span_row(span)) is not None]
            if rows:
                with self._pool.connection() as conn:
                    with conn.cursor() as cur:
                        cur.executemany(_INSERT_SQL, rows)
                    conn.commit()
            return SpanExportResult.SUCCESS
        except Exception:
            logger.exception("[TRACE_STORE] Failed to export spans")
            return SpanExportResult.FAILURE

    def _span_row(self, span):
        """Build an insert tuple for one OTel orchestration span, or None to skip.

        LLM call spans are skipped — the LiteLLM success-callback path is the
        authoritative writer for those (insert_litellm_call). One row per LLM
        call, avoiding the empty-field duplicates the buggy Vertex AI OTel
        instrumentation produces.
        """
        if _is_llm_span(span.name):
            return None
        attrs = dict(span.attributes or {})
        ctx = span.context
        start_ns = span.start_time or 0
        end_ns = span.end_time or 0
        duration_ms = (end_ns - start_ns) / 1_000_000 if end_ns > start_ns else 0.0
        status = (
            "ERROR"
            if span.status and span.status.status_code == StatusCode.ERROR
            else "OK"
        )
        return (
            format(ctx.span_id, "016x"),
            format(ctx.trace_id, "032x"),
            format(span.parent.span_id, "016x") if span.parent else None,
            span.name, start_ns, end_ns, duration_ms, status,
            None, None, None, 0, 0, 0, 0.0,
            attrs.get("workflow.id"),
            json.dumps({k: str(v) for k, v in attrs.items()}),
        )

    # ------------------------------------------------------------------
    # LiteLLM callback path
    # ------------------------------------------------------------------

    def insert_litellm_call(
        self, *, span_id: str, trace_id: str, parent_span_id: str | None,
        name: str, model: str, prompt_tokens: int, completion_tokens: int,
        total_tokens: int, cost_usd: float, duration_ms: float,
        workflow_id: str | None, prompt_text: str | None = None,
        response_text: str | None = None, extra_attrs: dict | None = None,
    ) -> None:
        """Write one per-call LiteLLM record directly to the trace store.

        Called from the LiteLLM success callback in cleaned_llm_wrapper.py.
        span_id / trace_id / parent_span_id must be pre-formatted hex strings.
        prompt_text is the JSON-serialized messages list; response_text is the
        first choice content string. Both are None if unavailable.
        """
        now_ns = int(datetime.now(tz=timezone.utc).timestamp() * 1_000_000_000)
        start_ns = now_ns - int(duration_ms * 1_000_000)
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(_INSERT_SQL, (
                        span_id, trace_id, parent_span_id, name, start_ns, now_ns,
                        duration_ms, "OK", model, prompt_text, response_text,
                        prompt_tokens, completion_tokens, total_tokens, cost_usd,
                        workflow_id, json.dumps(extra_attrs or {}),
                    ))
                conn.commit()
        except Exception:
            logger.exception("[TRACE_STORE] insert_litellm_call failed")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        try:
            self._pool.close()
        except Exception:
            # Shutdown stays best-effort, but a failed close is worth a trace
            # in the log — silent suppression hides resource-leak regressions.
            logger.warning("[TRACE_STORE] pool close failed during shutdown", exc_info=True)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True  # writes commit synchronously per call/batch

    def cleanup_old_traces(self, max_age_days: int = 30) -> int:
        """Delete traces older than max_age_days (native interval math)."""
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM llm_traces "
                        "WHERE created_at < now() - make_interval(days => %s)",
                        (max_age_days,))
                    deleted = cur.rowcount
                conn.commit()
            if deleted > 0:
                logger.info(
                    "[TRACE_STORE] Cleaned up %d traces older than %d days",
                    deleted, max_age_days)
            return deleted
        except Exception:
            logger.exception("[TRACE_STORE] Failed to cleanup old traces")
            return 0


# ---------------------------------------------------------------------------
# Module-level singleton — used by BOTH the LiteLLM callback path and the
# OTel BatchSpanProcessor (observability._get_exporter reuses this instance).
# Initialized lazily on first call so import order does not matter.
# ---------------------------------------------------------------------------
_singleton: "PostgresSpanExporter | None" = None
_singleton_lock = threading.Lock()


def get_trace_store() -> "PostgresSpanExporter | None":
    """Return the shared PostgresSpanExporter singleton for direct writes.

    Returns None (never raises) if the store cannot be initialized (e.g. Postgres
    unreachable), so callers can guard with `if store := get_trace_store()`.
    """
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        if _singleton is not None:
            return _singleton
        try:
            _singleton = PostgresSpanExporter()
        except Exception:
            logger.exception("[TRACE_STORE] Singleton init failed")
            return None
    return _singleton

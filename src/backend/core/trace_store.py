"""
SQLite Span Exporter — lightweight trace storage for pilot deployments.

Stores OTel spans (including LLM prompt/response from OpenLLMetry) in a local
SQLite database. Replaces the write-only logs/workflow_metrics.jsonl with a
queryable trace store for per-call debugging.

Schema stores the OTel span model flattened to SQL columns for the fields
we care about: workflow_id, agent name, model, prompt, response, tokens,
cost, latency. Non-LLM spans are stored with NULL prompt/response.

Two write paths exist, with strict separation of concerns:
1. LiteLLM callback path — insert_litellm_call() is the AUTHORITATIVE writer
   for LLM calls (prompt, response, tokens, cost). Called from the LiteLLM
   success callback in cleaned_llm_wrapper.py. Bypasses the broken
   opentelemetry-instrumentation-vertexai which creates empty-attribute spans.
2. OTel export path — SpanExporter.export() stores orchestration-only spans
   (crewai.agent, http client, chromadb). LLM spans are skipped via
   _is_llm_span() to avoid duplicate rows with the LiteLLM path.

Both paths share a single SQLiteSpanExporter singleton (one connection, one
threading.Lock) returned by get_trace_store(). observability._get_exporter()
returns this same singleton to the OTel BatchSpanProcessor. WAL mode allows
concurrent readers.

Schema migrations: _ensure_schema() is called on every __init__ and is
idempotent. Bump _SCHEMA_VERSION and add a new branch there when changing
_DB_SCHEMA so existing DBs catch up on the next startup instead of silently
running against a stale schema.

Referenced by: core/observability.py (_get_exporter), api/trace_endpoints.py,
               crew_ai/cleaned_llm_wrapper.py (get_trace_store)
Depends on: opentelemetry-sdk
"""
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Sequence

from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import StatusCode

logger = logging.getLogger(__name__)

# Single source of truth for the trace DB path — imported by trace_endpoints.py
# so both files always reference the same location.
TRACE_DB_PATH = "./data/llm_traces.db"

_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_traces (
    id                  TEXT PRIMARY KEY,
    trace_id            TEXT NOT NULL,
    parent_span_id      TEXT,
    name                TEXT NOT NULL,
    start_time_ns       INTEGER NOT NULL,
    end_time_ns         INTEGER NOT NULL,
    duration_ms         REAL NOT NULL,
    status              TEXT DEFAULT 'OK',
    model               TEXT,
    prompt_text         TEXT,
    response_text       TEXT,
    prompt_tokens       INTEGER DEFAULT 0,
    completion_tokens   INTEGER DEFAULT 0,
    total_tokens        INTEGER DEFAULT 0,
    cost_usd            REAL DEFAULT 0.0,
    workflow_id         TEXT,
    attributes_json     TEXT,
    created_at          TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_traces_trace_id   ON llm_traces(trace_id);
CREATE INDEX IF NOT EXISTS idx_traces_workflow_id ON llm_traces(workflow_id);
CREATE INDEX IF NOT EXISTS idx_traces_created     ON llm_traces(created_at);
CREATE INDEX IF NOT EXISTS idx_traces_model       ON llm_traces(model);
"""

# Current schema version. Bump + add a _migrate_vN branch in _ensure_schema()
# whenever you change _DB_SCHEMA. Existing DBs catch up on next startup.
_SCHEMA_VERSION = 1


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Apply schema migrations idempotently. Safe to call on every startup.

    Uses INSERT OR IGNORE on the version row so concurrent starters (e.g. a
    FastAPI worker + the trace-store singleton initializing in parallel) do
    not collide on the PRIMARY KEY. CREATE TABLE IF NOT EXISTS + ALTER TABLE
    statements are naturally idempotent when guarded by the version check.
    """
    conn.execute("CREATE TABLE IF NOT EXISTS trace_schema_version (v INTEGER PRIMARY KEY)")
    row = conn.execute("SELECT MAX(v) FROM trace_schema_version").fetchone()
    current = (row[0] if row and row[0] is not None else 0)

    if current < 1:
        conn.executescript(_DB_SCHEMA)
        conn.execute("INSERT OR IGNORE INTO trace_schema_version (v) VALUES (1)")

    # Future migrations go here. Example:
    # if current < 2:
    #     conn.execute("ALTER TABLE llm_traces ADD COLUMN agent_name TEXT")
    #     conn.execute("INSERT OR IGNORE INTO trace_schema_version (v) VALUES (2)")

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


class SQLiteSpanExporter(SpanExporter):
    """Export OTel spans to a local SQLite database."""

    def __init__(self, db_path: str = TRACE_DB_PATH):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        _ensure_schema(self._conn)
        logger.info("[TRACE_STORE] SQLite trace store initialized at %s (schema v%d)", db_path, _SCHEMA_VERSION)

    # ------------------------------------------------------------------
    # OTel export path
    # ------------------------------------------------------------------

    def export(self, spans: Sequence) -> SpanExportResult:
        try:
            with self._lock:
                for span in spans:
                    self._insert_span(span)
                self._conn.commit()
            return SpanExportResult.SUCCESS
        except Exception:
            logger.exception("[TRACE_STORE] Failed to export spans")
            return SpanExportResult.FAILURE

    def _insert_span(self, span) -> None:
        """Insert one OTel orchestration span (crewai.agent / http / chromadb / etc).

        LLM call spans are skipped — the LiteLLM success-callback path is the
        authoritative writer for those (see insert_litellm_call). This keeps
        exactly one row per LLM call and avoids the empty-field duplicates
        that the buggy Vertex AI OTel instrumentation produces.
        """
        attrs = dict(span.attributes or {})
        ctx = span.context

        if _is_llm_span(span.name):
            return

        start_ns = span.start_time or 0
        end_ns = span.end_time or 0
        duration_ms = (end_ns - start_ns) / 1_000_000 if end_ns > start_ns else 0.0

        status = (
            "ERROR"
            if span.status and span.status.status_code == StatusCode.ERROR
            else "OK"
        )

        self._conn.execute(
            """
            INSERT OR REPLACE INTO llm_traces
            (id, trace_id, parent_span_id, name, start_time_ns, end_time_ns,
             duration_ms, status, model, prompt_text, response_text,
             prompt_tokens, completion_tokens, total_tokens, cost_usd,
             workflow_id, attributes_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 0, 0, 0, 0.0, ?, ?)
            """,
            (
                format(ctx.span_id, "016x"),
                format(ctx.trace_id, "032x"),
                format(span.parent.span_id, "016x") if span.parent else None,
                span.name,
                start_ns,
                end_ns,
                duration_ms,
                status,
                attrs.get("workflow.id"),
                json.dumps({k: str(v) for k, v in attrs.items()}),
            ),
        )

    # ------------------------------------------------------------------
    # LiteLLM callback path
    # ------------------------------------------------------------------

    def insert_litellm_call(
        self,
        *,
        span_id: str,
        trace_id: str,
        parent_span_id: str | None,
        name: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        cost_usd: float,
        duration_ms: float,
        workflow_id: str | None,
        prompt_text: str | None = None,
        response_text: str | None = None,
        extra_attrs: dict | None = None,
    ) -> None:
        """
        Write one per-call LiteLLM record directly to the trace store.

        Called from the LiteLLM success callback in cleaned_llm_wrapper.py.
        Uses the same lock as the OTel export path so concurrent workflows
        serialize writes safely. WAL mode handles concurrent readers.

        span_id / trace_id / parent_span_id must be pre-formatted hex strings.
        prompt_text is the JSON-serialized messages list; response_text is the
        first choice content string. Both are None if unavailable.
        Pass extra_attrs for any additional k/v pairs to store in attributes_json.
        """
        now_ns = int(datetime.now(tz=timezone.utc).timestamp() * 1_000_000_000)
        start_ns = now_ns - int(duration_ms * 1_000_000)

        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO llm_traces
                    (id, trace_id, parent_span_id, name, start_time_ns, end_time_ns,
                     duration_ms, status, model, prompt_text, response_text,
                     prompt_tokens, completion_tokens, total_tokens, cost_usd,
                     workflow_id, attributes_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        span_id,
                        trace_id,
                        parent_span_id,
                        name,
                        start_ns,
                        now_ns,
                        duration_ms,
                        "OK",
                        model,
                        prompt_text,
                        response_text,
                        prompt_tokens,
                        completion_tokens,
                        total_tokens,
                        cost_usd,
                        workflow_id,
                        json.dumps(extra_attrs or {}),
                    ),
                )
                self._conn.commit()
        except Exception:
            logger.exception("[TRACE_STORE] insert_litellm_call failed")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """No-op close: the connection is owned by the module-level singleton.

        BatchSpanProcessor calls shutdown() on the exporter at app stop, but
        the SAME exporter is also used by the LiteLLM success-callback path.
        Closing the connection here would cause late callbacks to fail
        silently against a closed DB. The process exit will reclaim the
        connection; SQLite is crash-safe in WAL mode.
        """
        with self._lock:
            try:
                self._conn.commit()
            except Exception:
                pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        with self._lock:
            self._conn.commit()
        return True

    def cleanup_old_traces(self, max_age_days: int = 30) -> int:
        """Delete traces older than max_age_days to prevent unbounded growth."""
        try:
            with self._lock:
                cursor = self._conn.execute(
                    "DELETE FROM llm_traces WHERE created_at < datetime('now', ?)",
                    (f"-{max_age_days} days",),
                )
                self._conn.commit()
                deleted = cursor.rowcount
                if deleted > 0:
                    logger.info(
                        "[TRACE_STORE] Cleaned up %d traces older than %d days",
                        deleted, max_age_days,
                    )
                return deleted
        except Exception:
            logger.exception("[TRACE_STORE] Failed to cleanup old traces")
            return 0


# ---------------------------------------------------------------------------
# Module-level singleton — used by BOTH the LiteLLM callback path and the
# OTel BatchSpanProcessor (observability._get_exporter reuses this instance).
# One connection, one threading.Lock — no object-level races. Initialized
# lazily on first call so import order does not matter.
# ---------------------------------------------------------------------------
_singleton: SQLiteSpanExporter | None = None
_singleton_lock = threading.Lock()


def get_trace_store() -> SQLiteSpanExporter | None:
    """
    Return the shared SQLiteSpanExporter singleton for direct writes.

    Returns None (never raises) if the DB cannot be initialized, so callers
    can guard with `if store := get_trace_store()` without error handling.
    """
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        if _singleton is not None:
            return _singleton
        try:
            _singleton = SQLiteSpanExporter(db_path=TRACE_DB_PATH)
        except Exception:
            logger.exception("[TRACE_STORE] Singleton init failed")
            return None
    return _singleton

"""
Unit tests for src.backend.core.trace_store — PostgresSpanExporter and helpers.

Covers:
- _ensure_schema: idempotent creation, version tracking
- _is_llm_span: suffix detection for all known patterns
- PostgresSpanExporter: init, export (success/failure), orchestration-span store /
  LLM skip, insert_litellm_call (success/exception), shutdown, force_flush,
  cleanup_old_traces
- get_trace_store: singleton creation and None-on-failure path

Runs against an isolated `trace_test` Postgres schema (see conftest.py).
"""
import pytest
from unittest.mock import MagicMock, patch

import psycopg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_span_id_counter = 0


def _make_otel_span(name="crewai.agent", workflow_id=None, is_error=False, has_parent=False):
    """Build a minimal mock that satisfies the exporter's attribute access."""
    global _span_id_counter
    _span_id_counter += 1
    from opentelemetry.trace import StatusCode
    span = MagicMock()
    span.name = name
    span.attributes = {"workflow.id": workflow_id} if workflow_id else {}
    span.context = MagicMock(span_id=_span_id_counter, trace_id=0x1234ABCD)
    span.parent = MagicMock(span_id=0x9999) if has_parent else None
    span.start_time = 1_000_000_000
    span.end_time = 2_000_000_000
    span.status = MagicMock(
        status_code=StatusCode.ERROR if is_error else StatusCode.OK
    )
    return span


def _broken_store(trace_dsn):
    """A throwaway PostgresSpanExporter whose pool is closed → writes fail."""
    from src.backend.core.trace_store import PostgresSpanExporter
    store = PostgresSpanExporter(dsn=trace_dsn)
    store._pool.close()
    return store


# ---------------------------------------------------------------------------
# _ensure_schema
# ---------------------------------------------------------------------------

class TestEnsureSchema:
    def test_creates_llm_traces_table(self, trace_store, trace_query):
        rows = trace_query(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = 'llm_traces'")
        assert rows

    def test_creates_schema_version_table(self, trace_store, trace_query):
        rows = trace_query(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = 'trace_schema_version'")
        assert rows

    def test_records_current_version(self, trace_store, trace_query):
        from src.backend.core.trace_store import _SCHEMA_VERSION
        row = trace_query("SELECT MAX(v) FROM trace_schema_version")[0]
        assert row[0] == _SCHEMA_VERSION

    def test_idempotent_on_second_call(self, trace_store, trace_dsn, trace_query):
        from src.backend.core.trace_store import _ensure_schema, _SCHEMA_VERSION
        conn = psycopg.connect(trace_dsn)
        try:
            _ensure_schema(conn)  # must not raise; version row stays single
        finally:
            conn.close()
        row = trace_query("SELECT COUNT(*), MAX(v) FROM trace_schema_version")[0]
        assert row[0] == 1 and row[1] == _SCHEMA_VERSION


# ---------------------------------------------------------------------------
# _is_llm_span
# ---------------------------------------------------------------------------

class TestIsLLMSpan:
    def test_chat_suffix(self):
        from src.backend.core.trace_store import _is_llm_span
        assert _is_llm_span("openai.chat") is True
        assert _is_llm_span("vertex_ai.chat") is True

    def test_completion_suffix(self):
        from src.backend.core.trace_store import _is_llm_span
        assert _is_llm_span("vertex_ai.completion") is True

    def test_embeddings_suffix(self):
        from src.backend.core.trace_store import _is_llm_span
        assert _is_llm_span("text-embedding.embeddings") is True

    def test_llm_suffix(self):
        from src.backend.core.trace_store import _is_llm_span
        assert _is_llm_span("vertex_ai/gemini-2.5.llm") is True

    def test_case_insensitive(self):
        from src.backend.core.trace_store import _is_llm_span
        assert _is_llm_span("OpenAI.CHAT") is True

    def test_orchestration_agent_span_false(self):
        from src.backend.core.trace_store import _is_llm_span
        assert _is_llm_span("Test Planner.agent") is False

    def test_crewai_workflow_false(self):
        from src.backend.core.trace_store import _is_llm_span
        assert _is_llm_span("crewai.workflow") is False

    def test_http_span_false(self):
        from src.backend.core.trace_store import _is_llm_span
        assert _is_llm_span("http.client") is False

    def test_empty_string_false(self):
        from src.backend.core.trace_store import _is_llm_span
        assert _is_llm_span("") is False

    def test_none_safe(self):
        from src.backend.core.trace_store import _is_llm_span
        assert _is_llm_span(None) is False


# ---------------------------------------------------------------------------
# PostgresSpanExporter — init
# ---------------------------------------------------------------------------

class TestPostgresSpanExporterInit:
    def test_dsn_stored(self, trace_store, trace_dsn):
        assert trace_store.dsn == trace_dsn

    def test_pool_open(self, trace_store):
        assert trace_store._pool is not None
        # a borrowed connection is usable
        with trace_store._pool.connection() as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# PostgresSpanExporter — export
# ---------------------------------------------------------------------------

class TestExport:
    def test_export_orchestration_span_returns_success(self, trace_store):
        from opentelemetry.sdk.trace.export import SpanExportResult
        result = trace_store.export([_make_otel_span("crewai.task", workflow_id="wf-1")])
        assert result == SpanExportResult.SUCCESS

    def test_export_commits_row(self, trace_store, trace_query):
        trace_store.export([_make_otel_span("crewai.agent")])
        assert trace_query("SELECT COUNT(*) FROM llm_traces")[0][0] == 1

    def test_export_skips_llm_spans(self, trace_store, trace_query):
        trace_store.export([_make_otel_span("openai.chat")])
        assert trace_query("SELECT COUNT(*) FROM llm_traces")[0][0] == 0

    def test_export_multiple_spans(self, trace_store, trace_query):
        trace_store.export([
            _make_otel_span("crewai.agent"),
            _make_otel_span("openai.chat"),   # LLM — skipped
            _make_otel_span("http.request"),
        ])
        assert trace_query("SELECT COUNT(*) FROM llm_traces")[0][0] == 2

    def test_export_returns_failure_on_error(self, trace_dsn):
        from opentelemetry.sdk.trace.export import SpanExportResult
        store = _broken_store(trace_dsn)
        assert store.export([_make_otel_span()]) == SpanExportResult.FAILURE


# ---------------------------------------------------------------------------
# PostgresSpanExporter — orchestration span storage (via export)
# ---------------------------------------------------------------------------

class TestOrchestrationStore:
    def test_stores_workflow_id(self, trace_store, trace_query):
        trace_store.export([_make_otel_span("crewai.agent", workflow_id="wf-test")])
        assert trace_query("SELECT 1 FROM llm_traces WHERE workflow_id='wf-test'")

    def test_error_status_stored(self, trace_store, trace_query):
        trace_store.export([_make_otel_span("crewai.agent", is_error=True)])
        assert trace_query("SELECT status FROM llm_traces")[0][0] == "ERROR"

    def test_ok_status_stored(self, trace_store, trace_query):
        trace_store.export([_make_otel_span("crewai.agent")])
        assert trace_query("SELECT status FROM llm_traces")[0][0] == "OK"

    def test_parent_span_id_stored(self, trace_store, trace_query):
        trace_store.export([_make_otel_span("crewai.agent", has_parent=True)])
        assert trace_query("SELECT parent_span_id FROM llm_traces")[0][0] is not None

    def test_no_parent_span_id_null(self, trace_store, trace_query):
        trace_store.export([_make_otel_span("crewai.agent", has_parent=False)])
        assert trace_query("SELECT parent_span_id FROM llm_traces")[0][0] is None

    def test_attributes_stored_as_jsonb(self, trace_store, trace_query):
        trace_store.export([_make_otel_span("crewai.agent", workflow_id="wf-x")])
        val = trace_query("SELECT attributes_json FROM llm_traces")[0][0]
        assert isinstance(val, dict)  # jsonb → parsed dict

    def test_duration_computed(self, trace_store, trace_query):
        trace_store.export([_make_otel_span("crewai.agent")])
        assert trace_query("SELECT duration_ms FROM llm_traces")[0][0] == pytest.approx(1000.0)

    def test_zero_times_produces_zero_duration(self, trace_store, trace_query):
        span = _make_otel_span("crewai.agent")
        span.start_time = 0
        span.end_time = 0
        trace_store.export([span])
        assert trace_query("SELECT duration_ms FROM llm_traces")[0][0] == 0.0


# ---------------------------------------------------------------------------
# PostgresSpanExporter — insert_litellm_call
# ---------------------------------------------------------------------------

class TestInsertLitellmCall:
    def _insert(self, store, **overrides):
        defaults = dict(
            span_id="span-abc", trace_id="trace-xyz", parent_span_id=None,
            name="gemini/gemini-2.5-flash.litellm", model="gemini/gemini-2.5-flash",
            prompt_tokens=10, completion_tokens=5, total_tokens=15, cost_usd=0.001,
            duration_ms=200.0, workflow_id="wf-1",
            prompt_text='[{"role":"user","content":"hi"}]', response_text="hello",
        )
        defaults.update(overrides)
        store.insert_litellm_call(**defaults)

    def test_row_is_inserted(self, trace_store, trace_query):
        self._insert(trace_store)
        assert trace_query("SELECT 1 FROM llm_traces WHERE id='span-abc'")

    def test_model_stored(self, trace_store, trace_query):
        self._insert(trace_store)
        assert trace_query("SELECT model FROM llm_traces WHERE id='span-abc'")[0][0] == "gemini/gemini-2.5-flash"

    def test_tokens_stored(self, trace_store, trace_query):
        self._insert(trace_store)
        row = trace_query(
            "SELECT prompt_tokens, completion_tokens, total_tokens "
            "FROM llm_traces WHERE id='span-abc'")[0]
        assert (row[0], row[1], row[2]) == (10, 5, 15)

    def test_cost_stored(self, trace_store, trace_query):
        self._insert(trace_store)
        assert trace_query("SELECT cost_usd FROM llm_traces WHERE id='span-abc'")[0][0] == pytest.approx(0.001)

    def test_extra_attrs_stored(self, trace_store, trace_query):
        self._insert(trace_store, span_id="span-extra", extra_attrs={"key": "val"})
        val = trace_query("SELECT attributes_json FROM llm_traces WHERE id='span-extra'")[0][0]
        assert val["key"] == "val"

    def test_none_extra_attrs_stored_as_empty(self, trace_store, trace_query):
        self._insert(trace_store, span_id="span-none-attrs", extra_attrs=None)
        assert trace_query("SELECT attributes_json FROM llm_traces WHERE id='span-none-attrs'")[0][0] == {}

    def test_bigint_timestamps_stored(self, trace_store, trace_query):
        # nanosecond epochs (~1.7e18) require BIGINT — would overflow int4.
        self._insert(trace_store, span_id="span-ns")
        ns = trace_query("SELECT start_time_ns, end_time_ns FROM llm_traces WHERE id='span-ns'")[0]
        assert ns[0] > 1_000_000_000_000_000 and ns[1] > ns[0]

    def test_exception_does_not_raise(self, trace_dsn):
        store = _broken_store(trace_dsn)
        store.insert_litellm_call(  # must not raise
            span_id="span-fail", trace_id="t", parent_span_id=None, name="x.litellm",
            model="m", prompt_tokens=1, completion_tokens=1, total_tokens=2,
            cost_usd=0.0, duration_ms=1.0, workflow_id=None)

    def test_duplicate_span_id_replaces(self, trace_store, trace_query):
        self._insert(trace_store, span_id="dup-span", cost_usd=0.001)
        self._insert(trace_store, span_id="dup-span", cost_usd=0.999)
        assert trace_query("SELECT cost_usd FROM llm_traces WHERE id='dup-span'")[0][0] == pytest.approx(0.999)


# ---------------------------------------------------------------------------
# PostgresSpanExporter — lifecycle methods
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_shutdown_does_not_raise(self, trace_dsn):
        _broken_store(trace_dsn)  # already-closed pool; shutdown again below
        from src.backend.core.trace_store import PostgresSpanExporter
        store = PostgresSpanExporter(dsn=trace_dsn)
        store.shutdown()

    def test_shutdown_twice_does_not_raise(self, trace_dsn):
        from src.backend.core.trace_store import PostgresSpanExporter
        store = PostgresSpanExporter(dsn=trace_dsn)
        store.shutdown()
        store.shutdown()

    def test_force_flush_returns_true(self, trace_store):
        assert trace_store.force_flush() is True

    def test_cleanup_old_traces_deletes_old(self, trace_store, trace_query):
        with trace_store._pool.connection() as conn:
            conn.execute(
                "INSERT INTO llm_traces "
                "(id, trace_id, name, start_time_ns, end_time_ns, duration_ms, created_at) "
                "VALUES ('old-1','tr-old','agent',0,1,1.0, now() - interval '60 days')")
            conn.commit()
        assert trace_store.cleanup_old_traces(max_age_days=30) >= 1

    def test_cleanup_old_traces_keeps_recent(self, trace_store):
        with trace_store._pool.connection() as conn:
            conn.execute(
                "INSERT INTO llm_traces "
                "(id, trace_id, name, start_time_ns, end_time_ns, duration_ms, created_at) "
                "VALUES ('new-1','tr-new','agent',0,1,1.0, now())")
            conn.commit()
        assert trace_store.cleanup_old_traces(max_age_days=30) == 0

    def test_cleanup_handles_exception(self, trace_dsn):
        store = _broken_store(trace_dsn)
        assert store.cleanup_old_traces() == 0


# ---------------------------------------------------------------------------
# get_trace_store — singleton
# ---------------------------------------------------------------------------

class TestGetTraceStore:
    def test_returns_singleton(self):
        import src.backend.core.trace_store as ts
        saved = ts._singleton
        ts._singleton = None
        try:
            with patch.object(ts, "PostgresSpanExporter", return_value=MagicMock()):
                a = ts.get_trace_store()
                b = ts.get_trace_store()
            assert a is b
        finally:
            ts._singleton = saved

    def test_returns_none_on_init_failure(self):
        import src.backend.core.trace_store as ts
        saved = ts._singleton
        ts._singleton = None
        try:
            with patch.object(ts, "PostgresSpanExporter", side_effect=Exception("boom")):
                assert ts.get_trace_store() is None
        finally:
            ts._singleton = saved

    def test_returns_existing_singleton_without_lock(self):
        import src.backend.core.trace_store as ts
        saved = ts._singleton
        fake = MagicMock()
        ts._singleton = fake
        try:
            assert ts.get_trace_store() is fake
        finally:
            ts._singleton = saved

"""
Unit tests for src.backend.core.trace_store — SQLiteSpanExporter and helpers.

Covers:
- _ensure_schema: idempotent creation, version tracking
- _is_llm_span: suffix detection for all known patterns
- SQLiteSpanExporter: init, export (success/failure), _insert_span (LLM skip / orchestration store),
  insert_litellm_call (success/exception), shutdown, force_flush, cleanup_old_traces
- get_trace_store: singleton creation and None-on-failure path
"""
import json
import threading
import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(tmp_path):
    from src.backend.core.trace_store import SQLiteSpanExporter
    return SQLiteSpanExporter(db_path=str(tmp_path / "traces.db"))


_span_id_counter = 0


def _make_otel_span(name="crewai.agent", workflow_id=None, is_error=False, has_parent=False):
    """Build a minimal mock that satisfies _insert_span's attribute access."""
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


# ---------------------------------------------------------------------------
# _ensure_schema
# ---------------------------------------------------------------------------

class TestEnsureSchema:
    def test_creates_llm_traces_table(self, tmp_path):
        store = _make_store(tmp_path)
        cur = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_traces'"
        )
        assert cur.fetchone() is not None

    def test_creates_schema_version_table(self, tmp_path):
        store = _make_store(tmp_path)
        cur = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trace_schema_version'"
        )
        assert cur.fetchone() is not None

    def test_records_version_1(self, tmp_path):
        store = _make_store(tmp_path)
        row = store._conn.execute("SELECT MAX(v) FROM trace_schema_version").fetchone()
        assert row[0] == 1

    def test_idempotent_on_second_call(self, tmp_path):
        from src.backend.core.trace_store import _ensure_schema
        store = _make_store(tmp_path)
        # Should not raise
        _ensure_schema(store._conn)
        row = store._conn.execute("SELECT MAX(v) FROM trace_schema_version").fetchone()
        assert row[0] == 1


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
# SQLiteSpanExporter — init
# ---------------------------------------------------------------------------

class TestSQLiteSpanExporterInit:
    def test_wal_mode_enabled(self, tmp_path):
        store = _make_store(tmp_path)
        row = store._conn.execute("PRAGMA journal_mode").fetchone()
        assert row[0] == "wal"

    def test_db_path_stored(self, tmp_path):
        db = str(tmp_path / "traces.db")
        from src.backend.core.trace_store import SQLiteSpanExporter
        store = SQLiteSpanExporter(db_path=db)
        assert store.db_path == db

    def test_lock_is_threading_lock(self, tmp_path):
        store = _make_store(tmp_path)
        assert isinstance(store._lock, type(threading.Lock()))


# ---------------------------------------------------------------------------
# SQLiteSpanExporter — export
# ---------------------------------------------------------------------------

class TestExport:
    def test_export_orchestration_span_returns_success(self, tmp_path):
        from opentelemetry.sdk.trace.export import SpanExportResult
        store = _make_store(tmp_path)
        span = _make_otel_span("crewai.task", workflow_id="wf-1")
        result = store.export([span])
        assert result == SpanExportResult.SUCCESS

    def test_export_commits_row(self, tmp_path):
        store = _make_store(tmp_path)
        span = _make_otel_span("crewai.agent")
        store.export([span])
        count = store._conn.execute("SELECT COUNT(*) FROM llm_traces").fetchone()[0]
        assert count == 1

    def test_export_skips_llm_spans(self, tmp_path):
        store = _make_store(tmp_path)
        span = _make_otel_span("openai.chat")
        store.export([span])
        count = store._conn.execute("SELECT COUNT(*) FROM llm_traces").fetchone()[0]
        assert count == 0

    def test_export_multiple_spans(self, tmp_path):
        store = _make_store(tmp_path)
        spans = [
            _make_otel_span("crewai.agent"),
            _make_otel_span("openai.chat"),   # LLM — skipped
            _make_otel_span("http.request"),
        ]
        store.export(spans)
        count = store._conn.execute("SELECT COUNT(*) FROM llm_traces").fetchone()[0]
        assert count == 2

    def test_export_returns_failure_on_error(self, tmp_path):
        from opentelemetry.sdk.trace.export import SpanExportResult
        store = _make_store(tmp_path)
        store._conn.close()
        result = store.export([_make_otel_span()])
        assert result == SpanExportResult.FAILURE


# ---------------------------------------------------------------------------
# SQLiteSpanExporter — _insert_span
# ---------------------------------------------------------------------------

class TestInsertSpan:
    def test_stores_workflow_id(self, tmp_path):
        store = _make_store(tmp_path)
        store._insert_span(_make_otel_span("crewai.agent", workflow_id="wf-test"))
        store._conn.commit()
        row = store._conn.execute(
            "SELECT workflow_id FROM llm_traces WHERE workflow_id='wf-test'"
        ).fetchone()
        assert row is not None

    def test_error_status_stored(self, tmp_path):
        store = _make_store(tmp_path)
        store._insert_span(_make_otel_span("crewai.agent", is_error=True))
        store._conn.commit()
        row = store._conn.execute("SELECT status FROM llm_traces").fetchone()
        assert row[0] == "ERROR"

    def test_ok_status_stored(self, tmp_path):
        store = _make_store(tmp_path)
        store._insert_span(_make_otel_span("crewai.agent"))
        store._conn.commit()
        row = store._conn.execute("SELECT status FROM llm_traces").fetchone()
        assert row[0] == "OK"

    def test_parent_span_id_stored(self, tmp_path):
        store = _make_store(tmp_path)
        store._insert_span(_make_otel_span("crewai.agent", has_parent=True))
        store._conn.commit()
        row = store._conn.execute("SELECT parent_span_id FROM llm_traces").fetchone()
        assert row[0] is not None

    def test_no_parent_span_id_null(self, tmp_path):
        store = _make_store(tmp_path)
        store._insert_span(_make_otel_span("crewai.agent", has_parent=False))
        store._conn.commit()
        row = store._conn.execute("SELECT parent_span_id FROM llm_traces").fetchone()
        assert row[0] is None

    def test_attributes_serialised_as_json(self, tmp_path):
        store = _make_store(tmp_path)
        store._insert_span(_make_otel_span("crewai.agent", workflow_id="wf-x"))
        store._conn.commit()
        row = store._conn.execute("SELECT attributes_json FROM llm_traces").fetchone()
        parsed = json.loads(row[0])
        assert isinstance(parsed, dict)

    def test_duration_computed(self, tmp_path):
        store = _make_store(tmp_path)
        store._insert_span(_make_otel_span("crewai.agent"))
        store._conn.commit()
        row = store._conn.execute("SELECT duration_ms FROM llm_traces").fetchone()
        assert row[0] == pytest.approx(1000.0)

    def test_zero_times_produces_zero_duration(self, tmp_path):
        store = _make_store(tmp_path)
        span = _make_otel_span("crewai.agent")
        span.start_time = 0
        span.end_time = 0
        store._insert_span(span)
        store._conn.commit()
        row = store._conn.execute("SELECT duration_ms FROM llm_traces").fetchone()
        assert row[0] == 0.0


# ---------------------------------------------------------------------------
# SQLiteSpanExporter — insert_litellm_call
# ---------------------------------------------------------------------------

class TestInsertLitellmCall:
    def _insert(self, store, **overrides):
        defaults = dict(
            span_id="span-abc",
            trace_id="trace-xyz",
            parent_span_id=None,
            name="gemini/gemini-2.5-flash.litellm",
            model="gemini/gemini-2.5-flash",
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            cost_usd=0.001,
            duration_ms=200.0,
            workflow_id="wf-1",
            prompt_text='[{"role":"user","content":"hi"}]',
            response_text="hello",
        )
        defaults.update(overrides)
        store.insert_litellm_call(**defaults)

    def test_row_is_inserted(self, tmp_path):
        store = _make_store(tmp_path)
        self._insert(store)
        row = store._conn.execute(
            "SELECT * FROM llm_traces WHERE id='span-abc'"
        ).fetchone()
        assert row is not None

    def test_model_stored(self, tmp_path):
        store = _make_store(tmp_path)
        self._insert(store)
        row = store._conn.execute(
            "SELECT model FROM llm_traces WHERE id='span-abc'"
        ).fetchone()
        assert row[0] == "gemini/gemini-2.5-flash"

    def test_tokens_stored(self, tmp_path):
        store = _make_store(tmp_path)
        self._insert(store)
        row = store._conn.execute(
            "SELECT prompt_tokens, completion_tokens, total_tokens FROM llm_traces WHERE id='span-abc'"
        ).fetchone()
        assert row[0] == 10
        assert row[1] == 5
        assert row[2] == 15

    def test_cost_stored(self, tmp_path):
        store = _make_store(tmp_path)
        self._insert(store)
        row = store._conn.execute(
            "SELECT cost_usd FROM llm_traces WHERE id='span-abc'"
        ).fetchone()
        assert row[0] == pytest.approx(0.001)

    def test_extra_attrs_serialised(self, tmp_path):
        store = _make_store(tmp_path)
        self._insert(store, span_id="span-extra", extra_attrs={"key": "val"})
        row = store._conn.execute(
            "SELECT attributes_json FROM llm_traces WHERE id='span-extra'"
        ).fetchone()
        parsed = json.loads(row[0])
        assert parsed["key"] == "val"

    def test_none_extra_attrs_stored_as_empty(self, tmp_path):
        store = _make_store(tmp_path)
        self._insert(store, span_id="span-none-attrs", extra_attrs=None)
        row = store._conn.execute(
            "SELECT attributes_json FROM llm_traces WHERE id='span-none-attrs'"
        ).fetchone()
        assert json.loads(row[0]) == {}

    def test_exception_does_not_raise(self, tmp_path):
        store = _make_store(tmp_path)
        store._conn.close()
        # Must not raise
        self._insert(store, span_id="span-fail")

    def test_duplicate_span_id_replaces(self, tmp_path):
        store = _make_store(tmp_path)
        self._insert(store, span_id="dup-span", cost_usd=0.001)
        self._insert(store, span_id="dup-span", cost_usd=0.999)
        row = store._conn.execute(
            "SELECT cost_usd FROM llm_traces WHERE id='dup-span'"
        ).fetchone()
        assert row[0] == pytest.approx(0.999)


# ---------------------------------------------------------------------------
# SQLiteSpanExporter — lifecycle methods
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_shutdown_does_not_raise(self, tmp_path):
        store = _make_store(tmp_path)
        store.shutdown()

    def test_shutdown_on_closed_conn_does_not_raise(self, tmp_path):
        store = _make_store(tmp_path)
        store._conn.close()
        store.shutdown()

    def test_force_flush_returns_true(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.force_flush() is True

    def test_cleanup_old_traces_deletes_old(self, tmp_path):
        store = _make_store(tmp_path)
        store._conn.execute(
            "INSERT INTO llm_traces "
            "(id, trace_id, name, start_time_ns, end_time_ns, duration_ms, created_at) "
            "VALUES ('old-1', 'tr-old', 'agent', 0, 1, 1.0, datetime('now', '-60 days'))"
        )
        store._conn.commit()
        deleted = store.cleanup_old_traces(max_age_days=30)
        assert deleted >= 1

    def test_cleanup_old_traces_keeps_recent(self, tmp_path):
        store = _make_store(tmp_path)
        store._conn.execute(
            "INSERT INTO llm_traces "
            "(id, trace_id, name, start_time_ns, end_time_ns, duration_ms, created_at) "
            "VALUES ('new-1', 'tr-new', 'agent', 0, 1, 1.0, datetime('now'))"
        )
        store._conn.commit()
        deleted = store.cleanup_old_traces(max_age_days=30)
        assert deleted == 0

    def test_cleanup_handles_exception(self, tmp_path):
        store = _make_store(tmp_path)
        store._conn.close()
        result = store.cleanup_old_traces()
        assert result == 0


# ---------------------------------------------------------------------------
# get_trace_store — singleton
# ---------------------------------------------------------------------------

class TestGetTraceStore:
    def test_returns_singleton(self, tmp_path):
        import src.backend.core.trace_store as ts
        saved = ts._singleton
        ts._singleton = None
        try:
            with patch.object(ts, "TRACE_DB_PATH", str(tmp_path / "singleton.db")):
                a = ts.get_trace_store()
                b = ts.get_trace_store()
            assert a is b
        finally:
            ts._singleton = saved

    def test_returns_none_on_init_failure(self, tmp_path):
        import src.backend.core.trace_store as ts
        saved = ts._singleton
        ts._singleton = None
        try:
            with patch(
                "src.backend.core.trace_store.SQLiteSpanExporter",
                side_effect=Exception("boom"),
            ):
                result = ts.get_trace_store()
            assert result is None
        finally:
            ts._singleton = saved

    def test_returns_existing_singleton_without_lock(self, tmp_path):
        import src.backend.core.trace_store as ts
        saved = ts._singleton
        fake = MagicMock()
        ts._singleton = fake
        try:
            result = ts.get_trace_store()
            assert result is fake
        finally:
            ts._singleton = saved

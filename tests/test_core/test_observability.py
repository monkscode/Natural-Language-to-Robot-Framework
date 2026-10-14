"""
Tests for src.backend.core.observability — init_observability, _get_exporter,
create_workflow_span.

Key design:
- observability.py imports settings and traceloop LOCALLY inside each function.
  Patch targets must therefore be the source modules, not the observability namespace.
- settings → patch "src.backend.core.config.settings"
- get_trace_store → patch "src.backend.core.trace_store.get_trace_store"
- Traceloop.init → patch "traceloop.sdk.Traceloop.init" directly (package is installed)
- ImportError path → set sys.modules entries to None (Python raises ImportError on import)
"""
import sys
import pytest
from unittest.mock import patch, MagicMock


def _make_settings(backend="none", otlp_endpoint="http://localhost:4318"):
    s = MagicMock()
    s.OBSERVABILITY_BACKEND = backend
    s.OTLP_ENDPOINT = otlp_endpoint
    return s


# ---------------------------------------------------------------------------
# init_observability
# ---------------------------------------------------------------------------

class TestInitObservability:
    def test_disabled_returns_false(self):
        with patch("src.backend.core.config.settings", _make_settings("none")):
            from src.backend.core.observability import init_observability
            assert init_observability() is False

    def test_import_error_returns_false(self):
        """Setting a sys.modules entry to None causes ImportError on import."""
        with patch("src.backend.core.config.settings", _make_settings("postgres")):
            with patch.dict(sys.modules, {"traceloop": None, "traceloop.sdk": None}):
                from src.backend.core.observability import init_observability
                result = init_observability()
                assert result is False

    def test_generic_exception_returns_false(self):
        with patch("src.backend.core.config.settings", _make_settings("postgres")):
            with patch("src.backend.core.observability._get_exporter", return_value=MagicMock()):
                with patch("traceloop.sdk.Traceloop.init", side_effect=RuntimeError("boom")):
                    from src.backend.core.observability import init_observability
                    result = init_observability()
                    assert result is False

    def test_litellm_presence_does_not_crash(self):
        """When litellm is already imported (common in tests), no exception raised."""
        with patch("src.backend.core.config.settings", _make_settings("postgres")):
            with patch("src.backend.core.observability._get_exporter", return_value=MagicMock()):
                with patch("traceloop.sdk.Traceloop.init"):
                    import litellm  # noqa: F401 — ensure it's in sys.modules
                    from src.backend.core.observability import init_observability
                    result = init_observability()
                    assert isinstance(result, bool)

    def test_success_returns_true(self):
        with patch("src.backend.core.config.settings", _make_settings("postgres")):
            with patch("src.backend.core.observability._get_exporter", return_value=MagicMock()):
                with patch("traceloop.sdk.Traceloop.init"):
                    from src.backend.core.observability import init_observability
                    result = init_observability()
                    assert result is True


# ---------------------------------------------------------------------------
# _get_exporter
# ---------------------------------------------------------------------------

class TestGetExporter:
    def test_postgres_backend_returns_store(self):
        from src.backend.core.observability import _get_exporter
        mock_store = MagicMock()
        with patch("src.backend.core.trace_store.get_trace_store", return_value=mock_store):
            result = _get_exporter("postgres", "")
            assert result is mock_store

    def test_postgres_backend_store_none_raises(self):
        from src.backend.core.observability import _get_exporter
        with patch("src.backend.core.trace_store.get_trace_store", return_value=None):
            with pytest.raises(RuntimeError, match="could not be initialized"):
                _get_exporter("postgres", "")

    def test_grafana_backend_returns_otlp_exporter(self):
        from src.backend.core.observability import _get_exporter
        mock_cls = MagicMock(return_value=MagicMock())
        with patch(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
            mock_cls,
        ):
            result = _get_exporter("grafana", "http://tempo:4318")
            assert result is not None
            mock_cls.assert_called_once()

    def test_otlp_backend_returns_otlp_exporter(self):
        from src.backend.core.observability import _get_exporter
        mock_cls = MagicMock(return_value=MagicMock())
        with patch(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
            mock_cls,
        ):
            result = _get_exporter("otlp", "http://jaeger:4318")
            assert result is not None

    def test_unknown_backend_raises_value_error(self):
        from src.backend.core.observability import _get_exporter
        with pytest.raises(ValueError, match="Unknown OBSERVABILITY_BACKEND"):
            _get_exporter("splunk", "http://example.com")


# ---------------------------------------------------------------------------
# create_workflow_span
# ---------------------------------------------------------------------------

class TestCreateWorkflowSpan:
    def test_returns_context_manager(self):
        from src.backend.core.observability import create_workflow_span
        cm = create_workflow_span("wf-1", "query", "gemini", "gemini-2.5-flash")
        assert hasattr(cm, "__enter__") and hasattr(cm, "__exit__")

    def test_context_manager_enters_and_exits(self):
        from src.backend.core.observability import create_workflow_span
        with create_workflow_span("wf-1", "query", "gemini", "gemini-2.5-flash"):
            pass

    def test_import_error_path_returns_usable_cm(self):
        """When OTel modules are absent, a usable null context manager is returned."""
        from src.backend.core.observability import create_workflow_span
        with patch.dict(sys.modules, {
            "opentelemetry.baggage": None,
            "opentelemetry.context": None,
            "opentelemetry.trace": None,
        }):
            cm = create_workflow_span("wf-1", "query", "gemini", "gemini-2.5-flash")
            with cm:
                pass

    def test_exception_during_span_creation_returns_nullcontext(self):
        from src.backend.core.observability import create_workflow_span
        with patch("opentelemetry.trace.get_tracer", side_effect=RuntimeError("tracer error")):
            cm = create_workflow_span("wf-1", "query", "gemini", "gemini-2.5-flash")
            with cm:
                pass

    def test_custom_library_type(self):
        """library_type is a free-form telemetry attribute — any string is
        recorded as-is (it documents what a run used, it is not validated)."""
        from src.backend.core.observability import create_workflow_span
        with create_workflow_span(
            "wf-lib", "query", "vertex", "gemini-2.5-flash", library_type="custom-lib"
        ):
            pass


# ---------------------------------------------------------------------------
# The suite must never install a live span exporter
# ---------------------------------------------------------------------------

class TestSuiteNeverExportsSpans:
    """main.py calls init_observability() at IMPORT time, so any test that
    imports the app installs a global OTel provider for the whole process. With
    OBSERVABILITY_BACKEND=postgres (the .env value) that provider's exporter
    writes to the live DATABASE_URL, and every later create_workflow_span() in
    the run — including from unit tests that never intended an export — lands in
    public.llm_traces. Measured: the gate suite wrote 6 rows per run.

    tests/conftest.py pins the backend to "none" before anything imports
    src.backend, which makes init_observability() return early and install
    nothing. These assertions fail if that pin is ever removed.
    """

    def test_ambient_backend_is_none(self):
        from src.backend.core.config import settings
        assert settings.OBSERVABILITY_BACKEND == "none"

    def test_init_observability_is_a_no_op_under_test(self):
        from src.backend.core.observability import init_observability
        assert init_observability() is False

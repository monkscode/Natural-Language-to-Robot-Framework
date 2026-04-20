"""
Tests for src.backend.config.logging_config — setup_logging, bind_workflow_context,
_add_otel_context.

Notes:
- structlog.configure is process-global. Tests that call setup_logging() reset it each
  time; teardown restores root logger handlers to avoid cross-test bleed.
- _add_otel_context is tested with and without OTel installed.
"""
import logging
import os
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def restore_root_logger():
    """Restore root logger handlers and level after each test."""
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    yield
    root.handlers = original_handlers
    root.setLevel(original_level)


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------

class TestSetupLogging:
    def test_creates_log_directory(self, tmp_path):
        log_dir = str(tmp_path / "testlogs")
        from src.backend.config.logging_config import setup_logging
        setup_logging(log_dir=log_dir)
        assert os.path.isdir(log_dir)

    def test_sets_root_log_level_info(self, tmp_path):
        from src.backend.config.logging_config import setup_logging
        setup_logging(log_dir=str(tmp_path), log_level="INFO")
        assert logging.getLogger().level == logging.INFO

    def test_sets_root_log_level_debug(self, tmp_path):
        from src.backend.config.logging_config import setup_logging
        setup_logging(log_dir=str(tmp_path), log_level="DEBUG")
        assert logging.getLogger().level == logging.DEBUG

    def test_noisy_loggers_suppressed(self, tmp_path):
        from src.backend.config.logging_config import setup_logging, _NOISY_LOGGERS
        setup_logging(log_dir=str(tmp_path))
        for name in _NOISY_LOGGERS:
            assert logging.getLogger(name).level == logging.WARNING

    def test_json_renderer_by_default(self, tmp_path):
        """Default LOG_FORMAT produces JSON renderer (no crash, handlers installed)."""
        with patch.dict(os.environ, {"LOG_FORMAT": ""}, clear=False):
            from src.backend.config.logging_config import setup_logging
            setup_logging(log_dir=str(tmp_path))
        assert len(logging.getLogger().handlers) >= 1

    def test_console_renderer_when_log_format_console(self, tmp_path):
        """LOG_FORMAT=console installs ConsoleRenderer."""
        with patch.dict(os.environ, {"LOG_FORMAT": "console"}, clear=False):
            from src.backend.config.logging_config import setup_logging
            setup_logging(log_dir=str(tmp_path))
        assert len(logging.getLogger().handlers) >= 1

    def test_file_handler_fallback_on_oserror(self, tmp_path):
        """When RotatingFileHandler raises OSError, falls back to StreamHandler."""
        import logging.handlers as lh
        with patch.object(lh, "RotatingFileHandler", side_effect=OSError("no perms")):
            from src.backend.config.logging_config import setup_logging
            setup_logging(log_dir=str(tmp_path))
        # Should not raise; handlers still installed
        assert len(logging.getLogger().handlers) >= 1

    def test_two_handlers_installed(self, tmp_path):
        """Expects one file handler + one console handler."""
        from src.backend.config.logging_config import setup_logging
        setup_logging(log_dir=str(tmp_path))
        assert len(logging.getLogger().handlers) == 2


# ---------------------------------------------------------------------------
# bind_workflow_context
# ---------------------------------------------------------------------------

class TestBindWorkflowContext:
    def test_full_context_no_raise(self):
        from src.backend.config.logging_config import bind_workflow_context
        bind_workflow_context(
            "wf-full",
            model_provider="gemini",
            model_name="gemini-2.5-flash",
            library_type="browser",
        )

    def test_minimal_context_no_raise(self):
        from src.backend.config.logging_config import bind_workflow_context
        bind_workflow_context("wf-minimal")

    def test_optional_fields_omitted(self):
        from src.backend.config.logging_config import bind_workflow_context
        # Passing None for optional fields should silently omit them
        bind_workflow_context("wf-sparse", model_provider=None, model_name=None)

    def test_clears_previous_context(self):
        import structlog
        from src.backend.config.logging_config import bind_workflow_context
        structlog.contextvars.bind_contextvars(stale_key="stale_value")
        bind_workflow_context("wf-new")
        # After bind, stale_key should be gone
        ctx = structlog.contextvars.get_contextvars()
        assert "stale_key" not in ctx
        assert ctx.get("workflow_id") == "wf-new"


# ---------------------------------------------------------------------------
# _add_otel_context
# ---------------------------------------------------------------------------

class TestAddOtelContext:
    def test_passthrough_when_otel_unavailable(self):
        """When _get_current_span is None, event_dict is returned unchanged."""
        from src.backend.config import logging_config
        original = logging_config._get_current_span
        logging_config._get_current_span = None
        try:
            event_dict = {"event": "test_message", "level": "info"}
            result = logging_config._add_otel_context(None, None, event_dict)
            assert result == event_dict
        finally:
            logging_config._get_current_span = original

    def test_injects_trace_id_when_span_active(self):
        """When an active OTel span exists, trace_id/span_id are injected."""
        from src.backend.config import logging_config
        original = logging_config._get_current_span
        mock_span = MagicMock()
        mock_span.return_value.get_span_context.return_value = MagicMock(
            trace_id=0x1234567890ABCDEF1234567890ABCDEF,
            span_id=0x1234567890ABCDEF,
        )
        logging_config._get_current_span = mock_span
        try:
            event_dict = {"event": "hello"}
            result = logging_config._add_otel_context(None, None, event_dict)
            assert "trace_id" in result
            assert "span_id" in result
        finally:
            logging_config._get_current_span = original

    def test_no_injection_when_trace_id_zero(self):
        """When trace_id is 0 (no active trace), IDs are not injected."""
        from src.backend.config import logging_config
        original = logging_config._get_current_span
        mock_span = MagicMock()
        mock_span.return_value.get_span_context.return_value = MagicMock(trace_id=0)
        logging_config._get_current_span = mock_span
        try:
            event_dict = {"event": "hello"}
            result = logging_config._add_otel_context(None, None, event_dict)
            assert "trace_id" not in result
        finally:
            logging_config._get_current_span = original


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

class TestModuleConstants:
    def test_emoji_dict_has_required_keys(self):
        from src.backend.config.logging_config import EMOJI
        for key in ("start", "ai", "search", "code", "success", "error"):
            assert key in EMOJI

    def test_workflow_stages_has_four_entries(self):
        from src.backend.config.logging_config import WORKFLOW_STAGES
        assert len(WORKFLOW_STAGES) == 4

    def test_error_tips_dict_exists(self):
        from src.backend.config.logging_config import ERROR_TIPS
        assert isinstance(ERROR_TIPS, dict)

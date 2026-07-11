"""
Logging configuration — structlog setup, EMOJI constants, and workflow context binding.

setup_logging() replaces the text-format logging block previously in main.py with
JSON-structured output. All existing logging.getLogger() calls continue to work unchanged.

OTel trace_id/span_id are injected into every log entry when observability is active.
Set LOG_FORMAT=console for human-readable colored output during local development.

Referenced by: main.py (setup_logging, EMOJI), workflow_service.py (EMOJI, bind_workflow_context)
Depends on: structlog>=24.1.0
"""
import logging
import logging.handlers
import os
import sys
from pathlib import Path

import structlog

# Cache OTel span getter at module level — avoids a sys.modules lookup inside
# _add_otel_context, which fires for every log entry.
_get_current_span = None
try:
    from opentelemetry import trace as _otel_trace
    _get_current_span = _otel_trace.get_current_span
except ImportError:
    pass

_NOISY_LOGGERS = ("httpx", "httpcore", "urllib3", "asyncio")


def _add_otel_context(logger, method_name, event_dict):
    if _get_current_span is not None:
        ctx = _get_current_span().get_span_context()
        if ctx and ctx.trace_id:
            event_dict["trace_id"] = format(ctx.trace_id, "032x")
            event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


_SHARED_PROCESSORS = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
    _add_otel_context,
]


def setup_logging(log_dir: str = "logs", log_level: str = "INFO") -> None:
    """
    Configure structlog + standard logging with JSON output.

    Must be called once at startup BEFORE any logging calls and BEFORE importing
    modules that log at import time (e.g., before importing api/endpoints).
    """
    Path(log_dir).mkdir(exist_ok=True)

    structlog.configure(
        processors=_SHARED_PROCESSORS + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    renderer = (
        structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
        if os.environ.get("LOG_FORMAT", "").lower() == "console"
        else structlog.processors.JSONRenderer()
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=_SHARED_PROCESSORS,
    )

    try:
        file_handler: logging.Handler = logging.handlers.RotatingFileHandler(
            f"{log_dir}/application.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
    except (OSError, IOError) as e:
        logging.warning("Cannot open log file, falling back to stdout: %s", e)
        file_handler = logging.StreamHandler(sys.stdout)

    file_handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(file_handler)

    # Only add a separate console handler when file logging is active; if the
    # file handler already fell back to stdout, a second StreamHandler(stdout)
    # would double-emit every record.
    if not isinstance(file_handler, logging.StreamHandler) or file_handler.stream is not sys.stdout:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Mirror ERROR/CRITICAL records into logs/events.log for the Grafana live
    # error feed (structured, with resolution hints) — independent of LOG_FORMAT.
    from src.backend.core.grafana_events import ErrorEventHandler
    error_handler = ErrorEventHandler()
    error_handler.setLevel(logging.ERROR)
    root.addHandler(error_handler)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def bind_workflow_context(
    workflow_id: str,
    model_provider: str | None = None,
    model_name: str | None = None,
    library_type: str | None = None,
    org_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """
    Bind workflow context to all subsequent log entries in the current async context.

    Call once at the start of a workflow. Every logger.info/error/etc. call after this
    point automatically includes workflow_id (and optionally model info) without
    modifying any existing log call sites.
    """
    ctx = {"workflow_id": workflow_id}
    if model_provider:
        ctx["model_provider"] = model_provider
    if model_name:
        ctx["model_name"] = model_name
    if library_type:
        ctx["library_type"] = library_type
    if org_id:
        ctx["org_id"] = org_id
    if user_id:
        ctx["user_id"] = user_id
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(**ctx)


# ---------------------------------------------------------------------------
# Log-injection safety
# ---------------------------------------------------------------------------

def sanitize_for_log(value: object) -> str:
    """Strip CR/LF from a value before it is logged (CWE-117 log-injection guard).

    Carriage returns and line feeds are what an attacker uses to splice forged
    entries into a line-oriented log, so removing them is the standard mitigation
    for any user-controlled value reaching a log call. None/non-str values are
    coerced via str() so callers never have to guard the type at the call site.
    """
    return str(value).replace("\r", "").replace("\n", "")


# ---------------------------------------------------------------------------
# Emojis for different operations
EMOJI = {
    'start': '🎬',
    'ai': '🧠',
    'search': '🔍',
    'code': '⚡',
    'validate': '🔬',
    'docker': '🐳',
    'run': '🚀',
    'success': '🎉',
    'error': '⚠️',
    'info': '💡',
    'thinking': '🤔',
    'tool': '🔧',
    'browser': '🌐'
}

# Educational insights
INSIGHTS = {
    'planning': '� AI breaaks complex tasks into atomic steps for better accuracy',
    'elements': '🎯 Using vision-based detection with 95%+ accuracy',
    'code': '⚡ Browser Library is 2-3x faster than Selenium',
    'validation': '🔬 Validating syntax, structure, and best practices',
    'execution': '🐳 Running in isolated Docker container',
    'batch_processing': '🚀 Processing all elements in one browser session for better context'
}

# Agent workflow stages with progress weights
WORKFLOW_STAGES = {
    'planning': {
        'name': 'Planning test steps',
        'emoji': '🧠',
        'progress_start': 0,
        'progress_end': 25,
        'insight': INSIGHTS['planning']
    },
    'identifying': {
        'name': 'Identifying page elements',
        'emoji': '🔍',
        'progress_start': 25,
        'progress_end': 60,
        'insight': INSIGHTS['elements']
    },
    'generating': {
        'name': 'Generating test code',
        'emoji': '⚡',
        'progress_start': 60,
        'progress_end': 80,
        'insight': INSIGHTS['code']
    },
    'validating': {
        'name': 'Validating code',
        'emoji': '🔬',
        'progress_start': 80,
        'progress_end': 100,
        'insight': INSIGHTS['validation']
    }
}

# Error suggestions
ERROR_TIPS = {
    'element_not_found': [
        "Try describing the element differently",
        "Check if the element is visible on the page",
        "Verify the website URL is correct"
    ],
    'docker': [
        "Ensure Docker is running on your system",
        "Check Docker container logs for details",
        "Verify Docker has sufficient resources"
    ],
    'api': [
        "Check your internet connection",
        "Verify your API key is valid",
        "Check API service status"
    ],
    'generation': [
        "Check your internet connection",
        "Verify your API key is valid",
        "Try with a simpler query"
    ],
    'execution': [
        "Check if the website is accessible",
        "Review the generated code",
        "Try running again"
    ]
}

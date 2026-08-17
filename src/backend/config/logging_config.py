"""
Logging configuration — structlog setup, EMOJI constants, and workflow context binding.

setup_logging() replaces the text-format logging block previously in main.py with
JSON-structured output. All existing logging.getLogger() calls continue to work unchanged.

OTel trace_id/span_id are injected into every log entry when observability is active.
Set LOG_FORMAT=console for human-readable colored output during local development.

Referenced by: main.py (setup_logging, EMOJI), runner_exec/app.py (setup_logging,
bind_workflow_context), workflow_service.py (EMOJI, bind_workflow_context)
Depends on: structlog>=24.1.0
"""
import logging
import logging.handlers
import os
import sys
from pathlib import Path

import structlog

from src.backend.core.secret_redaction import SecretRedactingFilter, redact_processor

# Loggers that ship their OWN handler and therefore never reach our formatter.
# litellm logs the provider request URL, which for Google carries the API key.
_THIRD_PARTY_LOGGERS_WITH_OWN_HANDLERS = (
    "LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "litellm",
)


def _install_secret_redaction() -> None:
    """Install SecretRedactingFilter so no sink can print a credential.

    Two attachment points, because neither alone is sufficient:

    * On the named third-party LOGGERS. setup_logging() runs before litellm is
      imported, so its stderr handler does not exist yet and there is nothing to
      attach to — measured: the filter installed cleanly and the key still leaked.
      getLogger() returns a singleton, so a filter placed here survives litellm
      adding its handler later, and runs for anything logged through that logger.
    * On the root HANDLERS, which is where records propagating up from any other
      library are formatted.

    Idempotent — setup_logging can run more than once in a process.
    """
    for name in _THIRD_PARTY_LOGGERS_WITH_OWN_HANDLERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, SecretRedactingFilter) for f in logger.filters):
            logger.addFilter(SecretRedactingFilter())
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, SecretRedactingFilter) for f in handler.filters):
            handler.addFilter(SecretRedactingFilter())


# Docker polls /health every 30s on the API and on the executor, and nothing
# else polls it — the SPA never calls it and nginx only proxies the path
# through. Over the five days Loki held, uvicorn's access channel carried
# 6,408 lines against a healthcheck rate of 1,440 a day, so that channel is
# health polls and little else. They crowd out the lines that carry something.
_HEALTH_CHECK_PATHS = frozenset({"/health"})

# uvicorn logs an access line as
#   '%s - "%s %s HTTP/%s" %d' % (client_addr, method, path, http_version, status)
# so the request path is args[2]. Verified against uvicorn 0.51.0 — both call
# sites (protocols/http/h11_impl.py, httptools_impl.py) are identical.
_ACCESS_RECORD_ARITY = 5
_ACCESS_PATH_INDEX = 2


class HealthCheckAccessFilter(logging.Filter):
    """Drop uvicorn access records for health-check polls.

    Installed on the `uvicorn.access` LOGGER, not on a root handler. uvicorn
    ships that logger with its own handler and propagate=False, so its records
    never reach root and a handler-side filter would never see one. dictConfig
    replaces a logger's handlers but leaves its filters alone, so this survives
    anything uvicorn reconfigures afterwards.

    Drops only what it positively recognises: uvicorn's access format is not a
    stable contract across versions, and if it changes shape the acceptable
    failure is noisier logs, never silently discarded ones.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) != _ACCESS_RECORD_ARITY:
            return True
        path = args[_ACCESS_PATH_INDEX]
        if not isinstance(path, str):
            return True
        # Compared whole, not by prefix — /healthz belongs to somebody else.
        return path.split("?", 1)[0] not in _HEALTH_CHECK_PATHS


def _install_health_check_filter() -> None:
    """Idempotent — uvicorn.access is a singleton that outlives our setup."""
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, HealthCheckAccessFilter) for f in access_logger.filters):
        access_logger.addFilter(HealthCheckAccessFilter())


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
    # MUST stay after format_exc_info: that processor is what turns exc_info into
    # the 'exception' string, and provider tracebacks quote the failing request
    # URL — which for Google carries the caller's API key.
    redact_processor,
    _add_otel_context,
]


def setup_logging(log_dir: str | None = "logs", log_level: str = "INFO") -> None:
    """
    Configure structlog + standard logging with JSON output.

    Must be called once at startup BEFORE any logging calls and BEFORE importing
    modules that log at import time (e.g., before importing api/endpoints).

    log_dir=None configures stdout only, with no file handler at all. The
    executor needs that: docker-compose bind-mounts ./logs into both the API
    and the executor container, and run.sh starts both processes in the same
    working directory, so a second RotatingFileHandler on application.log puts
    two processes on one rotating file — concurrent rotation loses records and
    can truncate. Nothing is lost by not writing one, because Alloy scrapes
    container stdout rather than the file.
    """
    if log_dir is not None:
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

    handlers: list[logging.Handler] = []
    if log_dir is None:
        handlers.append(logging.StreamHandler(sys.stdout))
    else:
        try:
            handlers.append(logging.handlers.RotatingFileHandler(
                f"{log_dir}/application.log",
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            ))
        except (OSError, IOError) as e:
            logging.warning("Cannot open log file, falling back to stdout: %s", e)
            handlers.append(logging.StreamHandler(sys.stdout))

        # Only add a separate console handler when file logging is active; if the
        # file handler already fell back to stdout, a second StreamHandler(stdout)
        # would double-emit every record. RotatingFileHandler subclasses
        # StreamHandler, so the stream identity is what separates them.
        first = handlers[0]
        if not isinstance(first, logging.StreamHandler) or first.stream is not sys.stdout:
            handlers.append(logging.StreamHandler(sys.stdout))

    root = logging.getLogger()
    root.handlers.clear()
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    # Last, so the root handlers configured above are all in place to be filtered.
    # The third-party loggers are a different case: main.py calls setup_logging()
    # before litellm is imported, so its handler does not exist yet — see
    # _install_secret_redaction for why the filter goes on the logger there.
    _install_secret_redaction()
    _install_health_check_filter()


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


def clear_workflow_context() -> None:
    """Drop whatever bind_workflow_context bound.

    Pair the two on any handler that runs on a REUSED thread. FastAPI's sync
    endpoints are dispatched to a threadpool, so a binding left behind stamps
    the next thing that thread logs — another run, a health poll — with the
    previous run's id. A wrong workflow_id is worse than none: it corrupts the
    filter the log pipeline exists to serve.
    """
    structlog.contextvars.clear_contextvars()


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

"""Structured business-event emitter for the Grafana observability pipeline.

Writes one JSON line per business event to logs/events.log, which Promtail
(WSL) scrapes with labels {job="crewai", log_type="events"} and ships to Loki.
Grafana dashboard panels parse these lines with `| json` and unwrap numeric
fields, so every field emitted here is part of the dashboard contract.

Event schema (every record has ts + event + any bound workflow context):

| event               | fields                                                        |
|---------------------|---------------------------------------------------------------|
| workflow_started    | workflow_id, query, model_provider, model_name                 |
| workflow_completed  | workflow_id, duration_s, total_cost, total_tokens,             |
|                     | total_llm_calls, crewai_cost, crewai_tokens, crewai_llm_calls, |
|                     | browser_use_cost, browser_use_tokens, browser_use_llm_calls,   |
|                     | success_rate, total_elements, successful_elements,             |
|                     | failed_elements, dryrun_status, url                            |
| workflow_failed     | workflow_id, duration_s, error_type, error, resolution         |
| task_completed      | stage, agent, duration_s, output_len, tokens                   |
| tool_used           | tool, success_rate, elements_processed, custom_action_usage    |
| dryrun_completed    | passed ("true"/"false"), status, attempts, repairs             |
| guardrail_passed    | guardrail, attempts                                            |
| guardrail_retry     | guardrail, attempt                                             |
| error               | source, error_type, error, resolution                          |

Referenced by: workflow_service.py, dryrun_service.py, crew_ai/callbacks.py,
crew_ai/tasks.py, config/logging_config.py (error handler).
Depends on: stdlib only (structlog contextvars used opportunistically).

Design decisions:
- Independent of structlog/LOG_FORMAT: events.log must stay machine-readable
  JSON even when LOG_FORMAT=console renders application.log for humans.
- emit_event() must never raise — observability must not break the workflow.
- One shared line-buffered file handle guarded by a lock (event rate is tens
  per workflow; contention is negligible).
"""

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_EVENTS_FILE = Path("logs") / "events.log"

_lock = threading.Lock()
_events_fh = None

# PYTEST_CURRENT_TEST is set by pytest for the duration of each test (absent
# otherwise) — the standard way to detect "running under the test suite"
# without an explicit fixture at every call site.
_UNDER_PYTEST = "PYTEST_CURRENT_TEST" in os.environ


def _get_fh():
    global _events_fh
    if _events_fh is None:
        _EVENTS_FILE.parent.mkdir(exist_ok=True)
        _events_fh = open(_EVENTS_FILE, "a", encoding="utf-8", buffering=1)
    return _events_fh


def emit_event(event: str, **fields) -> None:
    """Write one JSON event line to logs/events.log. Never raises.

    No-op under pytest: nothing here mocks emit_event, so unguarded writes land
    directly in the real, Promtail-scraped events.log and pollute the Grafana
    dashboards with mock workflow data every time the suite runs.
    """
    if _UNDER_PYTEST:
        return
    try:
        # Opportunistically merge bound workflow context (workflow_id, model_name,
        # org_id, ...) so events correlate with application.log without every
        # call site having to thread the ids through.
        try:
            import structlog.contextvars
            ctx = structlog.contextvars.get_contextvars()
            for key in ("workflow_id", "model_provider", "model_name",
                        "library_type", "org_id", "user_id"):
                if key in ctx and key not in fields:
                    fields[key] = ctx[key]
        except Exception:
            pass

        record = {"ts": datetime.now(timezone.utc).isoformat(),
                  "event": event, **fields}
        line = json.dumps(record, default=str) + "\n"
        with _lock:
            _get_fh().write(line)
    except Exception:
        logger.debug("emit_event failed for %s", event, exc_info=True)


# ---------------------------------------------------------------------------
# Error resolution hints — matched against "<error_type>: <error_message>"
# ---------------------------------------------------------------------------

_RESOLUTION_HINTS: list[tuple[str, str]] = [
    (r"KeyboardInterrupt",
     "Run was manually aborted (Ctrl+C). No action needed unless unintentional."),
    (r"(?i)rate.?limit|429|resource.?exhausted|quota",
     "Vertex AI/Gemini rate limit or quota exceeded. Wait and retry, or check quotas in the GCP console."),
    (r"(?i)permission.?denied|forbidden|403|iam",
     "Permission denied. Verify the service account in credentials.json has the Vertex AI User role."),
    (r"(?i)unauthenticated|401|invalid.?credential|credentials",
     "Authentication failed. Check VERTEXAI_CREDENTIALS / GEMINI_API_KEY in src/backend/.env."),
    (r"(?i)timeout|timed.?out|deadline.?exceeded",
     "Request timed out. Check network/service health; consider a higher timeout."),
    (r"(?i)connection.?error|connection.?refused|network|unreachable|name.?resolution",
     "Network/service connectivity issue. Is the browser service (:4999) / runner-exec (:4998) / Postgres up?"),
    (r"(?i)docker|container",
     "Docker issue. Ensure Docker Desktop is running and the runner image is built."),
    (r"(?i)context.?length|context.?window|token.?limit|maximum.*token|too.?long",
     "Context window exceeded. Reduce task output length or use a larger-context model."),
    (r"(?i)model.?not.?found|model.?does.?not.?exist|invalid.?model",
     "Model not found. Verify ONLINE_MODEL in .env exists in your Vertex region."),
    (r"(?i)litellm|llm.*error|completion.*error",
     "LLM call failed via LiteLLM. Check application.log for the full traceback."),
    (r"(?i)browser.?use|playwright|locator",
     "Browser automation error. Check bus.log for the browser service traceback."),
    (r"(?i)robot|dryrun",
     "Robot Framework dryrun/execution error. Inspect the generated .robot file and dryrun errors."),
]

_FALLBACK_HINT = (
    "Unexpected error. Search application.log for 'Traceback'. Also check: "
    ".env loaded, credentials valid, all services (backend/browser/runner/postgres) up."
)


def resolve_hint(error_type: str, error_msg: str) -> str:
    """Return an actionable fix suggestion for an error (for the live error feed)."""
    combined = f"{error_type}: {error_msg}"
    for pattern, hint in _RESOLUTION_HINTS:
        if re.search(pattern, combined):
            return hint
    return _FALLBACK_HINT


class ErrorEventHandler(logging.Handler):
    """Mirror ERROR/CRITICAL records from any logger into events.log.

    Attached to the root logger by setup_logging(); powers the Grafana live
    error feed without touching individual logging call sites.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name == logger.name:
                return  # never recurse on our own failures
            error_msg = record.getMessage()[:300]
            emit_event("error", source=record.name, error_type=record.levelname,
                       error=error_msg,
                       resolution=resolve_hint(record.levelname, error_msg))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Guardrail attempt tracking
# ---------------------------------------------------------------------------
# CrewAI retries a task internally when a guardrail returns (False, ...), so a
# guardrail function has no view of its own attempt count. Track invocations
# per guardrail name here: each call is one attempt; a pass emits
# guardrail_passed with the total and resets. Workflows run one crew at a
# time per process, so a plain per-process counter is accurate in practice.

_guardrail_attempts: dict = {}
_guardrail_lock = threading.Lock()


def record_guardrail_result(guardrail: str, passed: bool) -> None:
    """Record one guardrail invocation and emit the matching event. Never raises."""
    try:
        with _guardrail_lock:
            attempts = _guardrail_attempts.get(guardrail, 0) + 1
            if passed:
                _guardrail_attempts[guardrail] = 0
            else:
                _guardrail_attempts[guardrail] = attempts
        if passed:
            emit_event("guardrail_passed", guardrail=guardrail, attempts=attempts)
        else:
            emit_event("guardrail_retry", guardrail=guardrail, attempt=attempts)
    except Exception:
        logger.debug("record_guardrail_result failed", exc_info=True)

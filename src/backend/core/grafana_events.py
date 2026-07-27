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
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_EVENTS_FILE = Path("logs") / "events.log"

# Promtail tails this file; rotation keeps a long-lived backend from filling the
# disk. Same scheme as _rotate_crewai_log() in crew.py.
_EVENTS_MAX_BYTES = 20 * 1024 * 1024   # 20MB
_EVENTS_BACKUP_COUNT = 5               # 5 backups = 120MB ceiling

_lock = threading.Lock()
_events_fh = None


def _under_pytest() -> bool:
    """True when running under the test suite. Checked at CALL time.

    NOT an import-time snapshot: pytest sets PYTEST_CURRENT_TEST per test item
    (setup/call/teardown) and does NOT set it during collection — which is when
    this module first gets imported, via workflow_service. A module-level
    snapshot is therefore False for the entire run, the guard never fires, and
    mock workflows land in the real, Promtail-scraped events.log.
    The sys.modules check also covers emissions from outside a test item
    (import side effects, session-scoped fixtures).
    """
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def _rotate_if_needed() -> None:
    """Roll events.log over when it exceeds the size cap. Caller holds _lock."""
    global _events_fh
    try:
        if _events_fh is not None:
            if _events_fh.tell() < _EVENTS_MAX_BYTES:
                return
            _events_fh.close()
            _events_fh = None
        elif not _EVENTS_FILE.exists() or _EVENTS_FILE.stat().st_size < _EVENTS_MAX_BYTES:
            return

        for i in range(_EVENTS_BACKUP_COUNT - 1, 0, -1):
            src = _EVENTS_FILE.with_suffix(f".log.{i}")
            if src.exists():
                src.replace(_EVENTS_FILE.with_suffix(f".log.{i + 1}"))
        if _EVENTS_FILE.exists():
            _EVENTS_FILE.replace(_EVENTS_FILE.with_suffix(".log.1"))
    except Exception:
        logger.debug("events.log rotation failed", exc_info=True)


def _get_fh():
    global _events_fh
    _rotate_if_needed()
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
    if _under_pytest():
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
            # record.levelname is only ever ERROR/CRITICAL, so it cannot group
            # anything on the live error feed. exc_info carries the real class
            # on any logger.exception()/logger.error(..., exc_info=True) call —
            # which is also what resolve_hint's type-name patterns
            # (KeyboardInterrupt, timeout, ...) are written to match.
            exc_type = (record.exc_info[0].__name__
                        if record.exc_info and record.exc_info[0]
                        else record.levelname)
            emit_event("error", source=record.name, error_type=exc_type,
                       level=record.levelname,
                       error=error_msg,
                       resolution=resolve_hint(exc_type, error_msg))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Guardrail attempt tracking
# ---------------------------------------------------------------------------
# CrewAI retries a task internally when a guardrail returns (False, ...), so a
# guardrail function has no view of its own attempt count. Track invocations
# here: each call is one attempt; a pass emits guardrail_passed with the total
# and clears the entry.
#
# Keyed by (workflow_id, guardrail) rather than the bare name. Both dimensions
# matter: concurrent workflows share this process, and the same guardrail
# function is attached at more than one call site within a single workflow
# (assemble_code_task and repair_code_task both use assembly_output_guardrail),
# so callers pass distinct names for distinct sites. Entries are popped on
# pass so the dict does not grow for the process lifetime.

_guardrail_attempts: dict = {}
_guardrail_lock = threading.Lock()


def _current_workflow_id() -> str:
    """Bound workflow_id, or "" outside a workflow. Never raises."""
    try:
        import structlog.contextvars
        return str(structlog.contextvars.get_contextvars().get("workflow_id", ""))
    except Exception:
        return ""


def record_guardrail_result(guardrail: str, passed: bool) -> None:
    """Record one guardrail invocation and emit the matching event. Never raises."""
    try:
        key = (_current_workflow_id(), guardrail)
        with _guardrail_lock:
            attempts = _guardrail_attempts.get(key, 0) + 1
            if passed:
                _guardrail_attempts.pop(key, None)
            else:
                _guardrail_attempts[key] = attempts
        if passed:
            emit_event("guardrail_passed", guardrail=guardrail, attempts=attempts)
        else:
            emit_event("guardrail_retry", guardrail=guardrail, attempt=attempts)
    except Exception:
        logger.debug("record_guardrail_result failed", exc_info=True)

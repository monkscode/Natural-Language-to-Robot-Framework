"""Strip credentials out of anything on its way to a log sink.

Provider SDK errors quote the request they failed on, and for Google that request
carries the API key in the query string. A single bad key therefore wrote the
user's own credential into application.log, stdout and Loki via
`logging.error(..., exc_info=True)`. Redaction happens as a structlog processor so
it covers structlog-native calls AND stdlib records routed through
ProcessorFormatter's foreign_pre_chain — one place, no call-site discipline needed.

Patterns are deliberately narrow: they mask the secret and keep the surrounding
diagnostic text, because an error that redacts its own context is unusable.

Third-party libraries that install their own handlers bypass the structlog chain
entirely — litellm attaches a StreamHandler to stderr on its 'LiteLLM' loggers, and
its copy of the same error still printed the key in full. SecretRedactingFilter
covers those; it must be attached to the HANDLER, because logger-level filters do
not run for records that propagate up from a child logger.

Referenced by: config/logging_config.py, services/workflow_service.py.
Depends on: logging, re, traceback (stdlib only — this must never fail to import).
"""
import logging
import re
import traceback

_PLACEHOLDER = "[REDACTED]"

# Google API keys have a fixed, recognisable shape (AIza + 35 chars). Matching the
# shape catches the key wherever it appears — query string, env echo, bare text.
_GOOGLE_API_KEY = re.compile(r"AIza[0-9A-Za-z_\-]{35}")

# key=/api_key=/access_token= as a URL query param, a bare assignment, or a JSON
# field (where the name itself is quoted). Stops at the first delimiter so the rest
# of the URL or object stays readable.
_QUERY_SECRET = re.compile(
    r"""(["']?(?:api[_-]?key|key|access[_-]?token|token)["']?\s*[=:]\s*)"""
    r"""("[^"]*"|'[^']*'|[^\s&"',}]+)""",
    re.IGNORECASE,
)

_BEARER = re.compile(r"(Bearer\s+)([A-Za-z0-9._\-]+)", re.IGNORECASE)

# A service-account JSON blob reaching an error message would otherwise print the
# whole private key.
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)


def redact_secrets(value):
    """Mask credentials in `value`. Non-strings are returned unchanged."""
    if not isinstance(value, str):
        return value
    out = _PRIVATE_KEY_BLOCK.sub(_PLACEHOLDER, value)
    out = _GOOGLE_API_KEY.sub(_PLACEHOLDER, out)
    out = _QUERY_SECRET.sub(lambda m: f"{m.group(1)}{_PLACEHOLDER}", out)
    out = _BEARER.sub(lambda m: f"{m.group(1)}{_PLACEHOLDER}", out)
    return out


# Event-dict keys that can carry a provider error verbatim. 'exception' is what
# structlog's format_exc_info produces from exc_info, and is where the observed
# leak lived.
_REDACTED_KEYS = ("event", "exception", "message", "error", "detail", "stack")


class SecretRedactingFilter(logging.Filter):
    """Redact a stdlib LogRecord in place. Attach to HANDLERS, not loggers.

    Always returns True — this filters content, never records.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        # With args present, msg is a %-format STRING: redacting it in place can eat
        # a placeholder ("key=%s" -> "key=[REDACTED]") and then getMessage() raises
        # TypeError, which would break logging for exactly the records we care about.
        # Render once, redact the result, and drop the args.
        if record.args:
            try:
                record.msg = redact_secrets(record.getMessage())
                record.args = ()
            except (TypeError, ValueError):
                # Pre-existing msg/args mismatch — leave it for the formatter to
                # report rather than masking someone else's bug as ours.
                pass
        elif isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        # Pre-render the traceback redacted. Formatter.format() reuses exc_text when
        # it is already set, so this is what reaches the stream.
        if record.exc_text:
            record.exc_text = redact_secrets(record.exc_text)
        elif record.exc_info:
            record.exc_text = redact_secrets(
                "".join(traceback.format_exception(*record.exc_info)).rstrip()
            )
        return True


def redact_processor(_logger, _method_name, event_dict):
    """structlog processor — must run AFTER format_exc_info.

    The first two arguments are structlog's fixed processor signature and are
    unused here; they are named with a leading underscore to say so.
    """
    for key in _REDACTED_KEYS:
        if key in event_dict:
            event_dict[key] = redact_secrets(event_dict[key])
    return event_dict

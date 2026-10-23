"""Strip credentials out of anything on its way to a log sink.

Provider SDK errors quote the request they failed on, and for Google that request
carries the API key in the query string. A single bad key therefore wrote the
user's own credential into application.log, stdout and Loki via
`logging.error(..., exc_info=True)`. Redaction happens as a structlog processor so
it covers structlog-native calls AND stdlib records routed through
ProcessorFormatter's foreign_pre_chain — one place, no call-site discipline needed.

What it masks: Google API keys by shape; `key=` / `token=` / `password=` style
assignments, JSON fields and Bearer tokens; private-key blocks; and, since #131, typed
values (`fill("<value>")` calls and a password field's `value="<value>"`) cut per line.
Each mask hides the secret and keeps the surrounding diagnostic text, because an error
that redacts its own context is unusable.

Third-party libraries that install their own handlers bypass the structlog chain
entirely — litellm attaches a StreamHandler to stderr on its 'LiteLLM' loggers, and
its copy of the same error still printed the key in full. SecretRedactingFilter
covers those; it must be attached to the HANDLER, because logger-level filters do
not run for records that propagate up from a child logger.

Referenced by: config/logging_config.py, services/workflow_service.py,
               crew_ai/optimization/failure_analyzer.py.
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
#
# Two tiers, because a single loose "key" alternative over-redacts badly: this
# codebase logs cache_key, hint_key and pattern_key, and even "monkey=5" matched,
# masking the diagnostics the log exists for. Names that are credentials whatever
# precedes them match anywhere; a bare key/token must be a standalone word, which
# the lookbehind enforces.
_CREDENTIAL_NAME = (
    r"""(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token"""
    r"""|client[_-]?secret|password|passwd|secret"""
    r"""|(?<![A-Za-z0-9_])(?:key|token))"""
)
_QUERY_SECRET = re.compile(
    rf"""(["']?{_CREDENTIAL_NAME}["']?\s*[=:]\s*)"""
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

# Typed values (issue #131): Playwright quotes the call it failed on, `fill("<value>")`,
# and an element preview echoes a password field's `value="<value>"`. Neither carries
# a credential NAME the tiers above could key on, so they are matched by shape.
# Playwright writes the value RAW — `fill("${value}")` and an element preview built
# from ` name="value"` pairs, cut at 500 characters — so an inner quote, a trailing
# backslash or a missing closing quote are all real shapes. Both masks therefore fail
# closed and work per line (a call-log entry or a preview is one line): a linear scan,
# no backtracking regex.
_CALL_START = re.compile(r"""\b(?:fill|elementHandle\.type)\((["'])""")
_INPUT_START = re.compile(r"<input\b", re.IGNORECASE)
_TYPE_PASSWORD = re.compile(r"""\btype\s*=\s*["']?password\b""", re.IGNORECASE)
_VALUE_OPEN = re.compile(r"""\bvalue\s*=\s*(["'])""", re.IGNORECASE)


def _mask_call_values(line: str) -> str:
    """`fill("<anything>")` -> `fill("***")`, running to the LAST `")` on the line
    (or, with none, to the end of the line)."""
    m = _CALL_START.search(line)
    if not m:
        return line
    quote = m.group(1)
    end = line.rfind(quote + ")")
    rest = line[end + 2:] if end >= m.end() else ""
    return f"{line[:m.end()]}***{quote}){rest}"


def _mask_password_tag(segment: str) -> str:
    """Hide everything from `value=<quote>` to the end of the tag (last `>`) or line."""
    if not _TYPE_PASSWORD.search(segment):
        return segment
    opened = _VALUE_OPEN.search(segment)
    if not opened:
        return segment
    close = segment.rfind(">")
    if close < opened.end():
        return f"{segment[:opened.end()]}***{opened.group(1)}"
    slash = "/" if segment[close - 1] == "/" else ""
    return f"{segment[:opened.end()]}***{opened.group(1)}{slash}{segment[close:]}"


def _mask_password_inputs(line: str) -> str:
    starts = [m.start() for m in _INPUT_START.finditer(line)]
    if not starts:
        return line
    ends = starts[1:] + [len(line)]
    return line[: starts[0]] + "".join(
        _mask_password_tag(line[s:e]) for s, e in zip(starts, ends)
    )


def _mask_typed_values(text: str) -> str:
    return "\n".join(
        _mask_password_inputs(_mask_call_values(line)) for line in text.split("\n")
    )


def redact_secrets(value):
    """Mask credentials in `value`. Non-strings are returned unchanged."""
    if not isinstance(value, str):
        return value
    out = _PRIVATE_KEY_BLOCK.sub(_PLACEHOLDER, value)
    out = _mask_typed_values(out)
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
                # uvicorn's colour formatter would re-render this un-redacted template with the args just dropped.
                record.__dict__.pop("color_message", None)
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

"""Secrets must never reach the logs.

Observed leak (2026-08-17): a bad AI Studio key produced a litellm traceback
containing the full request URL —
`https://generativelanguage.googleapis.com/v1beta/models/...:generateContent?key=AIza...`
— which logging.error(..., exc_info=True) wrote verbatim to application.log,
stdout and therefore Loki. Every user's own key was exposed by the single most
common misconfiguration.
"""
import sys

import pytest

from src.backend.core.secret_redaction import redact_secrets, redact_processor

_FAKE_KEY = "AIzaSy" + "B" * 33  # shaped like a Google API key, not a real one


class TestRedactSecrets:
    def test_masks_google_api_key_in_a_url_query_param(self):
        text = (
            "httpx.HTTPStatusError: Client error '400 Bad Request' for url "
            f"'https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.5-flash:generateContent?key={_FAKE_KEY}'"
        )
        out = redact_secrets(text)
        assert _FAKE_KEY not in out
        assert "[REDACTED]" in out
        # The surrounding diagnostic context must survive — redaction that eats
        # the whole message would trade one debugging problem for another.
        assert "400 Bad Request" in out
        assert "generativelanguage.googleapis.com" in out

    def test_masks_bare_google_api_key_anywhere(self):
        assert _FAKE_KEY not in redact_secrets(f"GEMINI_API_KEY={_FAKE_KEY} rejected")

    @pytest.mark.parametrize("template", [
        'api_key="{k}"',
        "api_key='{k}'",
        "api_key={k}",
        '"api_key": "{k}"',
        "Authorization: Bearer {k}",
        "&key={k}&alt=sse",
    ])
    def test_masks_common_credential_shapes(self, template):
        secret = "s3cr3t-value-not-google-shaped-000000"
        out = redact_secrets(template.format(k=secret))
        assert secret not in out
        assert "[REDACTED]" in out

    def test_masks_service_account_private_key_block(self):
        pem = (
            "-----BEGIN PRIVATE KEY-----\n"
            "MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQ\n"
            "-----END PRIVATE KEY-----"
        )
        out = redact_secrets(f"credentials={pem} failed to load")
        assert "MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQ" not in out
        assert "failed to load" in out

    @pytest.mark.parametrize("line", [
        "cache_key=abc123 lookup hit",
        "hint_key: kw_search_v2",
        "the monkey=5 counter",
        "idempotency_key=run-42",
        "pattern_key: browser.click",
        "prompt_tokens=1523 completion_tokens=88",
    ])
    def test_does_not_eat_non_secret_identifiers(self, line):
        """Over-redaction is its own outage: this codebase logs cache/hint/pattern
        keys, and masking them removes the diagnostics the log exists for. A bare
        'key'/'token' only counts when it is a standalone word."""
        assert redact_secrets(line) == line

    @pytest.mark.parametrize("line", [
        "x_api_key=REALSECRET0000",
        "client_secret=REALSECRET0000",
        "refresh_token=REALSECRET0000",
    ])
    def test_still_masks_prefixed_credential_names(self, line):
        # The narrowing must not create a hole: names that are credentials
        # regardless of prefix stay covered.
        assert "REALSECRET0000" not in redact_secrets(line)

    def test_leaves_clean_text_untouched(self):
        clean = "Unable to load vertex credentials from environment. Got=/app/credentials.json"
        assert redact_secrets(clean) == clean

    def test_handles_non_string_input(self):
        assert redact_secrets(None) is None
        assert redact_secrets(1234) == 1234


class TestRedactProcessor:
    def test_redacts_the_event_message(self):
        ev = redact_processor(None, "error", {"event": f"boom key={_FAKE_KEY}"})
        assert _FAKE_KEY not in ev["event"]

    def test_redacts_the_formatted_exception(self):
        # structlog's format_exc_info turns exc_info into this 'exception' string,
        # which is where the observed leak actually lived.
        ev = redact_processor(None, "error", {
            "event": "CrewAI workflow failed",
            "exception": f"Traceback...\n  url '...:generateContent?key={_FAKE_KEY}'",
        })
        assert _FAKE_KEY not in ev["exception"]
        assert "CrewAI workflow failed" in ev["event"]

    def test_leaves_other_fields_alone(self):
        ev = redact_processor(None, "info", {"event": "ok", "workflow_id": "abc-123"})
        assert ev["workflow_id"] == "abc-123"


class TestSecretRedactingFilter:
    """litellm ships its OWN StreamHandler to stderr on the 'LiteLLM' loggers, so
    its records never pass through our structlog processors. Measured in the
    container: the structlog copy of the error was redacted while litellm's own
    copy still printed `?key=AIza...` in full. A handler-level filter is what
    catches those, because logger-level filters do not run for propagated records.
    """

    def _record_with_exception(self):
        import logging as stdlib_logging

        try:
            raise ValueError(f"400 for url '...:generateContent?key={_FAKE_KEY}'")
        except ValueError:
            return stdlib_logging.LogRecord(
                name="LiteLLM", level=stdlib_logging.ERROR, pathname=__file__,
                lineno=1, msg="request failed", args=(), exc_info=sys.exc_info(),
            )

    def test_redacts_the_rendered_traceback(self):
        import logging as stdlib_logging

        from src.backend.core.secret_redaction import SecretRedactingFilter

        record = self._record_with_exception()
        assert SecretRedactingFilter().filter(record) is True
        rendered = stdlib_logging.Formatter("%(message)s").format(record)
        assert _FAKE_KEY not in rendered
        assert "request failed" in rendered

    def test_redacts_message_and_args(self):
        import logging as stdlib_logging

        from src.backend.core.secret_redaction import SecretRedactingFilter

        record = stdlib_logging.LogRecord(
            name="LiteLLM", level=stdlib_logging.INFO, pathname=__file__, lineno=1,
            msg="calling %s with key=%s", args=("gemini", _FAKE_KEY), exc_info=None,
        )
        SecretRedactingFilter().filter(record)
        # Must not raise: redacting a %-format string in place can consume a
        # placeholder and make getMessage() blow up with TypeError.
        rendered = record.getMessage()
        assert _FAKE_KEY not in rendered
        assert "gemini" in rendered

    def test_mismatched_msg_and_args_are_left_alone(self):
        # A pre-existing format bug elsewhere must not turn into an exception
        # raised from inside our filter.
        import logging as stdlib_logging

        from src.backend.core.secret_redaction import SecretRedactingFilter

        record = stdlib_logging.LogRecord(
            name="LiteLLM", level=stdlib_logging.INFO, pathname=__file__, lineno=1,
            msg="only one %s", args=("a", "b", "c"), exc_info=None,
        )
        assert SecretRedactingFilter().filter(record) is True

    def test_filter_is_installed_on_the_litellm_logger_itself(self):
        """Must survive litellm being imported AFTER setup_logging.

        Measured in the container: setup_logging() runs before litellm is imported,
        so attaching only to handlers found no handler to attach to and the key
        still reached stderr in full. Attaching to the logger singleton covers a
        handler that appears later.
        """
        import logging as stdlib_logging

        from src.backend.config.logging_config import _install_secret_redaction
        from src.backend.core.secret_redaction import SecretRedactingFilter

        _install_secret_redaction()
        litellm_logger = stdlib_logging.getLogger("LiteLLM")
        assert any(isinstance(f, SecretRedactingFilter) for f in litellm_logger.filters)

        # Idempotent — setup_logging may run more than once in a process.
        _install_secret_redaction()
        assert sum(isinstance(f, SecretRedactingFilter)
                   for f in litellm_logger.filters) == 1

    def test_a_handler_added_after_install_is_still_covered(self):
        """End-to-end of the lazy-import case: filter first, handler second."""
        import io
        import logging as stdlib_logging

        from src.backend.config.logging_config import _install_secret_redaction

        _install_secret_redaction()
        litellm_logger = stdlib_logging.getLogger("LiteLLM")
        stream = io.StringIO()
        handler = stdlib_logging.StreamHandler(stream)   # added AFTER the install
        handler.setFormatter(stdlib_logging.Formatter("%(message)s"))
        litellm_logger.addHandler(handler)
        previous_level = litellm_logger.level
        litellm_logger.setLevel(stdlib_logging.INFO)
        try:
            litellm_logger.info("POST ...:generateContent?key=%s", _FAKE_KEY)
            handler.flush()
            written = stream.getvalue()
            assert _FAKE_KEY not in written
            assert "[REDACTED]" in written
        finally:
            litellm_logger.removeHandler(handler)
            litellm_logger.setLevel(previous_level)


class TestWiredIntoTheRealFormatter:
    def test_stdlib_exc_info_is_redacted_end_to_end(self):
        """The leak arrived via logging.error(..., exc_info=True) from a stdlib
        logger, so the guarantee has to hold through ProcessorFormatter's
        foreign_pre_chain — not just when structlog is called directly."""
        import logging as stdlib_logging

        import structlog

        from src.backend.config.logging_config import _SHARED_PROCESSORS

        formatter = structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=_SHARED_PROCESSORS,
        )
        try:
            raise ValueError(
                "Client error '400 Bad Request' for url "
                f"'https://generativelanguage.googleapis.com/v1beta/models/x:generateContent?key={_FAKE_KEY}'"
            )
        except ValueError:
            record = stdlib_logging.LogRecord(
                name="root", level=stdlib_logging.ERROR, pathname=__file__, lineno=1,
                msg="CrewAI workflow failed", args=(), exc_info=sys.exc_info(),
            )
        rendered = formatter.format(record)
        assert _FAKE_KEY not in rendered
        assert "CrewAI workflow failed" in rendered


class TestTypedValueMasks:
    """A typed value must not survive in a failure reason (issue #131)."""

    @pytest.mark.parametrize("text, expected", [
        ('Error: fill("pw_marker_131") failed', 'Error: fill("***") failed'),
        ("locator.fill('pw_marker_131', timeout=1000)", "locator.fill('***')"),
        # Playwright writes the value RAW: an inner quote is not escaped...
        ('- fill("pw_mark"er_131")', '- fill("***")'),
        # ...and a trailing backslash is not an escape of the closing quote.
        ('- fill("pw_marker_131\\")\n  - waiting for element\n', '- fill("***")\n  - waiting for element\n'),
        # Type Text / Type Secret log elementHandle.type("...") (ruling R8).
        ('- elementHandle.type("pw_marker_131")', '- elementHandle.type("***")'),
        ('- elementHandle.type("pw_mark"er_131")', '- elementHandle.type("***")'),
    ])
    def test_masks_the_value_of_a_fill_call(self, text, expected):
        out = redact_secrets(text)
        assert "pw_marker_131" not in out and "er_131" not in out
        assert out == expected

    def test_a_fill_call_with_no_closing_pair_is_masked_to_the_end_of_the_line(self):
        out = redact_secrets('- fill("pw_marker_131\n  - next line stays')
        assert out == '- fill("***")\n  - next line stays'

    @pytest.mark.parametrize("tag", [
        '<input id="p" type="password" value="pw_marker_131">',
        '<input value="pw_marker_131" id="p" type="password" />',
        "<input id='p' type='password' value='pw_marker_131'>",
        '<input id="p" TYPE="PASSWORD" VALUE="pw_marker_131">',
        '<input id="p" type=password value="pw_marker_131">',
        '<input title="a>b" type="password" value="pw_marker_131">',
    ])
    def test_masks_the_value_of_a_password_input(self, tag):
        out = redact_secrets(f"element is not enabled: {tag} ...")
        assert "pw_marker_131" not in out
        assert 'value="***"' in out.lower() or "value='***'" in out.lower()
        assert "element is not enabled" in out

    @pytest.mark.parametrize("tag, expected", [
        # raw inner quote: Playwright does not escape attribute values
        ('<input id="p" type="password" value="pw_mark"er_131"/>',
         '<input id="p" type="password" value="***"/>'),
        # a value holding `>`
        ('<input id="p" type="password" value="pw_ma>rker_131"/>',
         '<input id="p" type="password" value="***"/>'),
        # preview cut at 500 chars inside the value: ellipsis, no closing quote
        ('<input id="p" type="password" value="pw_marker_131…',
         '<input id="p" type="password" value="***"'),
        # attributes after the value are lost by design (fail closed)
        ('<input id="p" type="password" value="pw_marker_131" name="pw">',
         '<input id="p" type="password" value="***">'),
    ])
    def test_a_password_input_fails_closed_on_raw_or_cut_previews(self, tag, expected):
        out = redact_secrets(f"locator resolved to {tag}")
        assert "pw_marker_131" not in out and "er_131" not in out
        assert out == f"locator resolved to {expected}"

    def test_text_after_the_tag_on_the_same_line_survives(self):
        out = redact_secrets('<input type="password" value="pw_marker_131"/> is not enabled')
        assert out == '<input type="password" value="***"/> is not enabled'

    def test_two_password_tags_on_one_line_are_both_masked(self):
        out = redact_secrets(
            '<input type="password" value="pw_marker_131"/><input type="password" value="pw_marker_132"/>'
        )
        assert "pw_marker_13" not in out
        assert out.count('value="***"') == 2

    def test_no_pattern_backtracks_on_a_huge_line(self):
        import time

        for unit in ('fill("', "<input type=password value='", '")', "<input ", 'fill("a" '):
            text = unit * (500_000 // len(unit))
            start = time.perf_counter()
            redact_secrets(text)
            assert time.perf_counter() - start < 5, f"slow on repeats of {unit!r}"

    def test_a_text_input_is_left_alone(self):
        text = '<input type="text" value="Cierra">'
        assert redact_secrets(text) == text

    def test_a_password_tag_without_a_value_is_left_alone(self):
        text = '<input id="p" type="password">'
        assert redact_secrets(text) == text

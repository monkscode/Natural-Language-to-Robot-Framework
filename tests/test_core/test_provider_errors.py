"""A misconfigured provider must produce an instruction, not a JSON dump.

Every string below was captured from a real litellm failure (2026-08-17) rather
than invented, so the matching cannot drift away from what the SDK actually
raises. Before this mapping existed, an invalid API key — the single most common
first-run mistake — rendered ~15 lines of google.rpc.ErrorInfo in the UI with no
mention of which setting was wrong or where to fix it.
"""
import pytest

from src.backend.core.provider_errors import friendly_setup_error

# --- verbatim captures -------------------------------------------------------

AI_STUDIO_BAD_KEY = (
    'litellm.AuthenticationError: geminiException - {   "error": {     "code": 400,'
    '     "message": "API key not valid. Please pass a valid API key.",'
    '     "status": "INVALID_ARGUMENT",     "details": [       {'
    '         "@type": "type.googleapis.com/google.rpc.ErrorInfo",'
    '         "reason": "API_KEY_INVALID",         "domain": "googleapis.com",'
    '         "metadata": {           "service": "generativelanguage.googleapis.com" } } ] } }'
)

VERTEX_MISSING_CREDENTIALS = (
    "litellm.APIConnectionError: Unable to load vertex credentials from environment. "
    "Got=/app/credentials.json Traceback (most recent call last): ..."
)

VERTEX_BILLING_DISABLED = (
    'litellm.BadRequestError: VertexAIException BadRequestError - {   "error": {'
    '     "code": 403,     "message": "This API method requires billing to be enabled.'
    ' Please enable billing on project #proj-123 by visiting'
    ' https://console.developers.google.com/billing/enable?project=proj-123 then retry.",'
    '     "status": "PERMISSION_DENIED" } }'
)


class TestRecognisedMisconfigurations:
    def test_invalid_ai_studio_key_names_the_setting_and_the_file(self):
        msg = friendly_setup_error(Exception(AI_STUDIO_BAD_KEY))
        assert msg is not None
        assert "GEMINI_API_KEY" in msg
        assert "src/backend/.env" in msg
        assert "aistudio.google.com" in msg
        # The raw SDK noise must not be forwarded to the user.
        assert "google.rpc.ErrorInfo" not in msg
        assert "INVALID_ARGUMENT" not in msg

    def test_missing_vertex_credentials_points_at_the_key_file(self):
        msg = friendly_setup_error(Exception(VERTEX_MISSING_CREDENTIALS))
        assert msg is not None
        assert "credentials.json" in msg
        # The compose overlay is exactly what a user forgets to pass.
        assert "docker-compose.vertex.yml" in msg

    def test_billing_disabled_is_explained_not_dumped(self):
        msg = friendly_setup_error(Exception(VERTEX_BILLING_DISABLED))
        assert msg is not None
        assert "billing" in msg.lower()
        assert "PERMISSION_DENIED" not in msg

    def test_vertex_api_not_enabled(self):
        raw = (
            'VertexAIException - {"error": {"code": 403, "message": '
            '"Vertex AI API has not been used in project 12345 before or it is disabled.", '
            '"status": "PERMISSION_DENIED", "details": [{"reason": "SERVICE_DISABLED"}]}}'
        )
        msg = friendly_setup_error(Exception(raw))
        assert msg is not None
        assert "aiplatform.googleapis.com" in msg or "Vertex AI API" in msg

    def test_rate_limit_is_explained(self):
        raw = "litellm.RateLimitError: VertexAIException - 429 Resource exhausted, quota exceeded"
        msg = friendly_setup_error(Exception(raw))
        assert msg is not None
        assert "rate" in msg.lower() or "quota" in msg.lower()


class TestDoesNotMisdiagnose:
    """A wrong instruction is worse than a raw error — it sends the user to fix
    something that was never broken. These are all shapes that a substring match
    on '429' or 'permission denied' would have mislabelled."""

    @pytest.mark.parametrize("raw", [
        "Workflow 429abc-def failed to assemble code",
        "completion_tokens=4290 exceeded budget",
        "Run id 8996de27-429f-4b92 produced no output.xml",
    ])
    def test_a_bare_429_substring_is_not_a_rate_limit(self, raw):
        assert friendly_setup_error(Exception(raw)) is None

    @pytest.mark.parametrize("raw", [
        "PermissionError: [Errno 13] Permission denied: '/app/robot_tests/run-1'",
        "docker: permission denied while trying to connect to the Docker daemon socket",
    ])
    def test_filesystem_permission_errors_are_not_vertex_iam(self, raw):
        # A Linux bind-mount ownership problem is common and has nothing to do
        # with roles/aiplatform.user.
        assert friendly_setup_error(Exception(raw)) is None

    def test_a_real_vertex_permission_error_is_still_caught(self):
        raw = ('VertexAIException - {"error": {"code": 403, "message": "Permission '
               '\'aiplatform.endpoints.predict\' denied", "status": "PERMISSION_DENIED"}}')
        msg = friendly_setup_error(Exception(raw))
        assert msg is not None
        assert "aiplatform.user" in msg

    def test_a_real_rate_limit_is_still_caught(self):
        for raw in ("litellm.RateLimitError: 429 Resource exhausted",
                    'VertexAIException - {"code": 429, "status": "RESOURCE_EXHAUSTED"}'):
            msg = friendly_setup_error(Exception(raw))
            assert msg is not None, raw
            assert "rate" in msg.lower() or "quota" in msg.lower()


class TestPassthrough:
    @pytest.mark.parametrize("raw", [
        "Failed to generate valid Robot Framework code: unexpected token",
        "runner-exec unreachable: Read timed out",
        "",
    ])
    def test_unrecognised_errors_return_none(self, raw):
        # Not every failure is a setup problem. Anything unrecognised must fall
        # through to the existing message rather than be mislabelled.
        assert friendly_setup_error(Exception(raw)) is None

    def test_secrets_never_survive_into_a_friendly_message(self):
        leaky = (
            "API key not valid. Please pass a valid API key. reason: API_KEY_INVALID "
            "url ...generateContent?key=AIzaSy" + "C" * 33
        )
        msg = friendly_setup_error(Exception(leaky))
        assert msg is not None
        assert "AIzaSy" not in msg

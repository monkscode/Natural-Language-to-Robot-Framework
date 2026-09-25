"""Turn provider misconfiguration into an instruction the user can act on.

A first run fails far more often on setup than on anything Mark 1 does: an unset
API key, a credentials.json the container cannot see, a Google Cloud project
without billing or without the Vertex AI API switched on. litellm surfaces all of
these as a nested google.rpc JSON blob, which the SSE stream forwarded verbatim —
so the most common newcomer mistake produced ~15 lines of `@type:
type.googleapis.com/google.rpc.ErrorInfo` and no hint about which setting was
wrong or which file to edit.

Every pattern here was matched against a real captured litellm exception, not a
guess. Anything unrecognised returns None so the caller keeps its existing
message — a wrong diagnosis is worse than a raw one.

Referenced by: services/workflow_service.py.
Depends on: core/secret_redaction.py, crew_ai/provider_retry.py (the retry
report, the per-day quota test), litellm (exception classes), re.
"""
import re

import litellm

from src.backend.core.secret_redaction import redact_secrets
from src.backend.crew_ai.provider_retry import names_per_day_quota, report_of

_ENV_FILE = "src/backend/.env"


def _ai_studio_key() -> str:
    return (
        "Google AI Studio rejected the API key. Set a valid GEMINI_API_KEY in "
        f"{_ENV_FILE} — create one free at https://aistudio.google.com/apikey — "
        "then restart with: docker compose up -d"
    )


def _vertex_credentials() -> str:
    return (
        "Vertex AI credentials could not be loaded. Check that credentials.json "
        "exists in the repo root and that you started the stack with the Vertex "
        "overlay, which mounts it into the container:\n"
        "  docker compose -f docker-compose.yml -f docker-compose.vertex.yml up -d\n"
        "Run bash setup_google_vertexAI.sh to create the key if you have not yet."
    )


def _vertex_api_disabled() -> str:
    return (
        "The Vertex AI API is not enabled on this Google Cloud project. Enable it "
        "with:\n  gcloud services enable aiplatform.googleapis.com --project=YOUR_PROJECT_ID\n"
        f"and confirm VERTEXAI_PROJECT in {_ENV_FILE} names that project."
    )


def _vertex_billing() -> str:
    return (
        "This Google Cloud project needs billing enabled before Vertex AI will "
        "serve requests. Enable it at "
        "https://console.cloud.google.com/billing, then retry. Check that "
        f"VERTEXAI_PROJECT in {_ENV_FILE} names the project you expect."
    )


def _daily_quota() -> str:
    return (
        "The model provider's daily quota for this model is used up, so retrying now "
        "will not help. Per-day quotas reset once a day (Google AI Studio's free tier "
        "at midnight Pacific time). To keep going now, use a key or project with more "
        f"quota, or switch MODEL_PROVIDER in {_ENV_FILE} to a provider with quota left."
    )


def _rate_limited() -> str:
    return (
        "The model provider rate-limited this request (quota exceeded). Wait a "
        "moment and try again. If it keeps happening, the free tier is likely too "
        "small for back-to-back runs — switch MODEL_PROVIDER to vertex in "
        f"{_ENV_FILE}, or request more quota."
    )


def _vertex_permission() -> str:
    return (
        "The Vertex AI service account is missing permission. Grant it the Vertex "
        "AI User role:\n"
        "  gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \\\n"
        "    --member=serviceAccount:vertex-ai-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com \\\n"
        "    --role=roles/aiplatform.user"
    )


# A bare "429" substring is NOT a rate limit: run ids and token counts contain it
# ("Workflow 429abc-...", "completion_tokens=4290"), and mislabelling those tells
# the user to sit and wait while the real fault goes unmentioned. Require either
# rate-limit vocabulary or 429 in an actual status-code position.
_RATE_LIMITED = re.compile(
    r"ratelimiterror"
    r"|resource[_ ]exhausted"
    r"|quota[_ ]exceeded|exceeded your current quota"
    r"|429\s*too\s*many\s*requests"
    r"|(?:code|status)\D{0,12}429\b",
    re.IGNORECASE,
)

_GOOGLE_API_MARKERS = ("aiplatform", "vertex", "googleapis")


def _is_vertex_permission(text: str) -> bool:
    """Permission failure that is actually Google's, not the filesystem's.

    "permission denied" alone is far more often a Linux bind-mount or Docker-socket
    problem, and sending someone to edit IAM roles over a file-ownership bug wastes
    real time — so a Google API marker must appear too. Plain substring tests, not
    a pair of `(?=.*x)` lookaheads: those rescan the whole string from every start
    position, which is quadratic on a multi-kilobyte traceback.
    """
    low = text.lower()
    denied = ("permission_denied" in low
              or "permission denied" in low
              or ("permission" in low and "denied" in low))
    return denied and any(marker in low for marker in _GOOGLE_API_MARKERS)

# Ordered: the first match wins, so the more specific causes come before the
# generic permission catch. Each entry is (predicate, message builder).
_RULES = (
    (lambda t: "API_KEY_INVALID" in t or "api key not valid" in t.lower(), _ai_studio_key),
    (lambda t: "unable to load vertex credentials" in t.lower(), _vertex_credentials),
    (lambda t: "requires billing to be enabled" in t.lower(), _vertex_billing),
    (lambda t: "SERVICE_DISABLED" in t
               or "has not been used in project" in t.lower()
               or "vertex ai api has not been used" in t.lower(), _vertex_api_disabled),
    # Google AI Studio per-day quota. Body shape from published real 429s
    # (inspect_ai#5526, vercel/ai#18627), replayed through litellm 1.75.3 — not
    # captured from our own traffic (we run vertex). Above _rate_limited: a
    # per-day 429 matches that pattern too, and "wait a moment" is wrong here.
    (names_per_day_quota, _daily_quota),
    (lambda t: bool(_RATE_LIMITED.search(t)), _rate_limited),
    (_is_vertex_permission, _vertex_permission),
)


def friendly_setup_error(exc: BaseException) -> str | None:
    """Return an actionable message for a known setup failure, else None."""
    text = str(exc)
    if not text:
        return None
    for matches, build in _RULES:
        if matches(text):
            # The raw text is never interpolated into the reply, but redact anyway
            # so a future message that quotes context cannot leak a key.
            return redact_secrets(build())
    return None


def _duration(seconds: float) -> str:
    whole = int(round(seconds))
    return f"{whole // 60} min {whole % 60} s" if whole >= 60 else f"{whole} s"


def _join_seconds(values: tuple[float, ...]) -> str:
    parts = [f"{v:g} s" for v in values]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def friendly_provider_error(exc: BaseException) -> str | None:
    """Say plainly that the model provider did not answer, else None.

    Covers the failures a cloud call ends on after its retries (W5): a timeout,
    a 503 or a 500. Rate limits are already explained by friendly_setup_error.
    APIConnectionError is deliberately NOT covered: LiteLLM also uses it as the
    catch-all for errors it cannot map, and a wrong diagnosis is worse than a
    raw one (see the module docstring). A timeout whose llm_provider is Ollama
    is a local-machine problem, not the provider's, and gets its own sentence.
    """
    if isinstance(exc, litellm.Timeout):
        what = "gave no reply"
    elif isinstance(exc, litellm.ServiceUnavailableError):
        what = "reported itself unavailable (HTTP 503)"
    elif isinstance(exc, litellm.InternalServerError):
        what = "failed with a server error (HTTP 500)"
    else:
        return None
    report = report_of(exc)
    if report is None:
        model = "/".join(p for p in (getattr(exc, "llm_provider", ""), getattr(exc, "model", "")) if p)
        detail = f"{model or 'The model'} {what}."
    else:
        detail = f"{report.model} {what} after {report.tries} tries over {_duration(report.elapsed_s)}"
        if report.timed_out_after_s:
            if len(report.timed_out_after_s) == 1:
                detail += f" (the try that timed out had a limit of {_join_seconds(report.timed_out_after_s)})"
            else:
                detail += f" (the tries that timed out had limits of {_join_seconds(report.timed_out_after_s)})"
        detail += "."
    if str(getattr(exc, "llm_provider", "") or "").startswith("ollama"):
        return redact_secrets(
            f"Your local model did not answer. {detail} This comes from your own Ollama "
            "server, not from your test description — check that Ollama is running at "
            "the OLLAMA_API_BASE address and that the model is pulled; a large model on "
            "a CPU can need longer than the 600 s LiteLLM allows per request by default."
        )
    return redact_secrets(
        f"The model provider did not answer. {detail} The problem is on the provider's "
        "side, not in your test description — try again in a few minutes."
    )

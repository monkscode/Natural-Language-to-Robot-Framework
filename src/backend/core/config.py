import os
import yaml
from pathlib import Path
from typing import Dict, Any, Optional
from pydantic_settings import BaseSettings
from pydantic import Field, validator
from dotenv import load_dotenv

load_dotenv("src/backend/.env")

class Settings(BaseSettings):
    # LLM Configuration
    # MODEL_PROVIDER controls which LLM backend is used:
    #   "vertex"  — Google Cloud Vertex AI (requires VERTEXAI_CREDENTIALS,
    #               VERTEXAI_PROJECT, VERTEXAI_LOCATION)
    #   "gemini"  — Google AI Studio (requires GEMINI_API_KEY)
    #   "local"   — Ollama (requires a running Ollama server)
    MODEL_PROVIDER: str = "vertex"
    GEMINI_API_KEY: str | None = None
    ONLINE_MODEL: str = "gemini-2.5-flash"
    LOCAL_MODEL: str = "llama3"

    # Vertex AI Configuration (only required when MODEL_PROVIDER=vertex)
    # VERTEXAI_CREDENTIALS is the env var LiteLLM reads to locate the service account
    # key file. Set it in .env (local dev) or via docker-compose.vertex.yml (Docker).
    VERTEXAI_PROJECT: str | None = None
    VERTEXAI_LOCATION: str | None = None
    
    # Note: SECONDS_BETWEEN_API_CALLS was removed during Phase 2 of codebase cleanup.
    # Rate limiting is no longer implemented as Google Gemini API has sufficient
    # rate limits (1500 RPM) for our use case. If rate limiting becomes necessary,
    # implement it at the API gateway level rather than wrapping individual LLM calls.
    
    # Service Configuration
    APP_PORT: int = Field(default=5000, description="Port for FastAPI service")
    # 127.0.0.1, NOT localhost: on Windows `localhost` resolves to IPv6 ::1 first
    # and the service binds IPv4 only, so every call stalls ~2s before the IPv4
    # fallback. Compose overrides this to the service name (browser-service:4999).
    BROWSER_USE_SERVICE_URL: str = Field(default="http://127.0.0.1:4999", description="URL for BrowserUse service")
    
    # Browser Configuration
    BROWSER_HEADLESS: bool = Field(default=True, description="Run browser in headless mode (no UI) for BrowserUse service")
    
    # Robot Framework Library Configuration. Browser Library (Playwright) is the
    # only supported target: the locator pipeline emits Playwright-only syntax
    # (role=, text=, >>> iframe piercing), so any other library would receive
    # valid-looking tests that fail on every step at runtime.
    ROBOT_LIBRARY: str = Field(default="browser", description="Robot Framework library to use (only 'browser' is supported)")

    # Artifact storage backend — pluggable, chosen per deployment, exactly like
    # MODEL_PROVIDER chooses an LLM backend. "local" = on-disk robot_tests/;
    # "s3" = upload to a bucket after each run (still stages locally for Docker).
    ARTIFACT_STORE: str = "local"
    ARTIFACT_S3_BUCKET: str = ""          # required when ARTIFACT_STORE=s3
    ARTIFACT_S3_PREFIX: str = "runs"
    ARTIFACT_S3_REGION: str = ""
    ARTIFACT_S3_ENDPOINT_URL: str = ""    # set for MinIO / S3-compatible; empty = AWS

    # Agent Retry Configuration
    MAX_AGENT_ITERATIONS: int = Field(default=3, description="Maximum iterations for agents with delegation enabled (retry attempts)")

    # Dryrun Validation Gate Configuration
    # Replaces the old CrewAI LLM validator agent with a deterministic
    # `robot --dryrun` gate plus a bounded Assembler repair loop. The gate is a
    # SOFT gate — Docker unavailable / any error degrades to dryrun_status:'unverified'
    # and still delivers the generated code.
    MAX_DRYRUN_FIXES: int = Field(
        default=2,
        description="Max Assembler repair attempts when robot --dryrun fails (0 = disabled).",
    )
    DRYRUN_ENABLED: bool = Field(
        default=True,
        description="Run a deterministic robot --dryrun gate after code generation.",
    )
    DRYRUN_TIMEOUT: int = Field(
        default=120,
        description="Seconds to wait for the dryrun container before skipping (graceful degrade).",
    )
    RUNNER_READ_ONLY_ROOTFS: bool = Field(
        default=False,
        description="Phase 4: opt-in read-only rootfs for runner containers; default off until a live run proves headless Chrome tolerates it (validated in a later phase task).",
    )
    # Phase 4: base URL of the socket-holding executor service. 127.0.0.1 for
    # `./run.sh` dev; compose overrides to the service name.
    # 127.0.0.1, NOT localhost: on Windows `localhost` resolves to IPv6 ::1 first
    # and the service binds IPv4 only, so every hop stalls ~2s before the IPv4
    # fallback (execute makes two hops → ~4s). Compose overrides to runner-exec:4998.
    RUNNER_EXEC_URL: str = Field(
        default="http://127.0.0.1:4998",  # NOSONAR — internal Docker network, no TLS needed
        description="Phase 4: base URL of the runner-exec service; 127.0.0.1 for run.sh dev, http://runner-exec:4998 in compose.",
    )

    # LLM Empty-Response Retry Configuration
    # Some Vertex AI Gemini models (notably gemini-3.5-flash) intermittently
    # return HTTP 200 with empty content (finish_reason=stop, 0 tokens emitted)
    # for otherwise-valid prompts. LiteLLM's num_retries only catches exceptions,
    # so these empty successes pass through and abort the workflow at CrewAI's
    # "Invalid response from LLM call - None or empty." check.
    # The wrapper (CleanedLLMWrapper.call) retries the SAME litellm.completion()
    # call — NOT the agent loop — so tool results already in the message history
    # are reused. browser-use, batch_browser_automation, etc. are NOT re-invoked.
    # Default 2 = up to two extra attempts (3 total calls). Max 5 to bound
    # worst-case extra latency (~12s with exponential backoff at 5s cap).
    LLM_EMPTY_RESPONSE_MAX_RETRIES: int = Field(
        default=2,
        description="Extra LLM call attempts when response is empty/whitespace (0 = disabled, max 5). Exponential backoff: 500ms × 2^n capped at 5s.",
    )
    
    # Custom Actions Configuration
    ENABLE_CUSTOM_ACTIONS: bool = Field(default=True, description="Enable/disable custom actions for browser automation")
    MAX_LOCATOR_STRATEGIES: int = Field(default=21, description="Maximum number of locator strategies to try")
    TRACK_LLM_COSTS: bool = Field(default=True, description="Enable/disable LLM cost tracking and logging")
    CREWAI_VERBOSE: bool = Field(default=False, description="Echo CrewAI agent reasoning and the full prompt to stdout. Off by default: measured 2026-08-14 it was 73.7% of the fastapi container's log stream, carries no workflow_id so it cannot be filtered to a run, and the same prompts are already in llm_traces.prompt_text on every model call")
    
    # Optimization Configuration
    OPTIMIZATION_ENABLED: bool = Field(default=True, description="Enable/disable optimization system (pattern learning, pgvector semantic search)")
    OPTIMIZATION_KEYWORD_SEARCH_TOP_K: int = Field(default=3, description="Number of keywords to return from search")
    OPTIMIZATION_PATTERN_CONFIDENCE_THRESHOLD: float = Field(default=0.7, description="Minimum confidence for pattern prediction (0.0-1.0)")
    OPTIMIZATION_CONTEXT_PRUNING_ENABLED: bool = Field(default=True, description="Enable smart context pruning")
    OPTIMIZATION_CONTEXT_PRUNING_THRESHOLD: float = Field(default=0.6, description="Minimum confidence for category classification (0.0-1.0)")
    HINT_TRACE_ENABLED: bool = Field(default=True, description="Capture the per-(workflow,hint) selection/attribution trace (N3 observability); gated by OPTIMIZATION_ENABLED")
    HINT_TRACE_RETENTION_DAYS: int = Field(default=90, description="Days to retain hint_workflow_trace rows; an opportunistic daily prune deletes older rows (0 = keep all)")
    
    # LLM Observability Configuration (Enhancement #3)
    # OBSERVABILITY_BACKEND controls where OTel traces are exported:
    #   "postgres" — Built-in Postgres trace store (the consolidated DB; default)
    #   "grafana"  — Grafana Tempo via OTLP (requires Tempo in docker-compose)
    #   "otlp"     — Any OTel-compatible endpoint (Langfuse, Jaeger, Datadog, etc.)
    #   "none"     — Tracing disabled
    OBSERVABILITY_BACKEND: str = Field(
        default="postgres",
        description="Trace export backend: postgres | grafana | otlp | none",
    )
    OTLP_ENDPOINT: str = Field(
        default="http://localhost:4318",
        description="OTLP HTTP endpoint for grafana or otlp backends",
    )

    # Concurrency configuration
    MAX_CONCURRENT_WORKFLOWS: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum number of concurrent test generation/execution workflows"
    )

    # ------------------------------------------------------------------
    # Authentication & Database (Phase 1: login/signup, roles, Google SSO)
    # ------------------------------------------------------------------
    # PostgreSQL DSN for the consolidated database: auth/users, the learning
    # stack (Postgres + pgvector), LLM traces and workflow metrics all live here.
    DATABASE_URL: str = Field(
        default="postgresql://nlrf:nlrf@localhost:5432/nlrf",
        description="PostgreSQL connection string for the consolidated database",
    )
    JWT_SECRET_KEY: str = Field(
        default="change-me-in-production",
        description="HMAC secret for signing JWT access tokens — MUST be overridden via env in production",
    )
    # Shorter lifetime bounds how long a stolen token is usable when the user
    # never explicitly revokes. Explicit revocation is via token_version
    # (logout-all), enforced against current DB state on every authenticated
    # request (require_user / require_admin / report access).
    JWT_EXPIRY_HOURS: int = Field(default=12, description="Access token lifetime in hours")
    # Comma-separated emails granted the 'admin' role at signup; everyone else
    # is 'user'. Stored as a string (not list) to avoid pydantic env JSON-parsing
    # pitfalls — read via the admin_emails_list property.
    ADMIN_EMAILS: str = Field(
        default="",
        description="Comma-separated emails granted the admin role at signup",
    )
    # Google SSO (server-side OAuth2/OIDC). Empty GOOGLE_CLIENT_ID disables the
    # Google routes gracefully (they return 503) so the app runs without it.
    GOOGLE_CLIENT_ID: str = Field(default="", description="Google OAuth client ID")
    GOOGLE_CLIENT_SECRET: str = Field(default="", description="Google OAuth client secret")
    GOOGLE_REDIRECT_URI: str = Field(
        default="http://localhost:5000/auth/google/callback",
        description="OAuth redirect URI registered with Google (must match exactly)",
    )
    # SPA origin the backend redirects to after Google login; and the CORS
    # allow-list (dev Vite :5173, containerized nginx :3000, fastapi :5000).
    FRONTEND_URL: str = Field(
        default="http://localhost:5173",
        description="Frontend origin for the post-OAuth redirect",
    )
    ALLOWED_ORIGINS: str = Field(
        default="http://localhost:5173,http://localhost:3000,http://localhost:5000",
        description="Comma-separated CORS allow-list for the SPA",
    )
    # Require a valid JWT on the API endpoints (and the admin role on admin
    # routes). False is an escape hatch for local API-only debugging — it lets
    # token-less requests through; never disable in production.
    AUTH_ENFORCED: bool = Field(
        default=True,
        description="Require a valid JWT (and admin role on admin routes) on API endpoints",
    )
    # Mark auth cookies (the Google OAuth state cookie) Secure so browsers only
    # send them over HTTPS. Keep False for local http dev; set True in production.
    COOKIE_SECURE: bool = Field(
        default=False,
        description="Set the Secure flag on auth cookies (enable behind HTTPS in production)",
    )
    # Deployment posture. 'production' enforces the auth security invariants at
    # startup (strong JWT secret + Secure cookies); 'development' only warns so
    # local http dev keeps working. See auth/security_posture.py.
    ENVIRONMENT: str = Field(
        default="development",
        description="Deployment environment: 'development' or 'production'",
    )
    # Per-IP rate limit for the unauthenticated auth endpoints (login, register,
    # forgot-password) — blunts brute-force / credential stuffing. slowapi syntax.
    AUTH_RATE_LIMIT: str = Field(
        default="10/minute",
        description="Per-IP rate limit on the auth endpoints (slowapi syntax, e.g. '10/minute')",
    )
    AUTH_RATE_LIMIT_ENABLED: bool = Field(
        default=True,
        description="Enable per-IP rate limiting on the auth endpoints",
    )

    @property
    def admin_emails_list(self) -> list[str]:
        """Normalized admin email allow-list (lowercased, de-blanked)."""
        return [e.strip().lower() for e in self.ADMIN_EMAILS.split(",") if e.strip()]

    @property
    def allowed_origins_list(self) -> list[str]:
        """CORS origins as a list for CORSMiddleware."""
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]

    @validator('MODEL_PROVIDER')
    def validate_model_provider(cls, v):
        """Validate that MODEL_PROVIDER is one of the supported providers."""
        if v.lower() not in ['gemini', 'vertex', 'local']:
            raise ValueError(f"MODEL_PROVIDER must be 'gemini', 'vertex', or 'local', got '{v}'")
        return v.lower()

    @validator('ROBOT_LIBRARY')
    def validate_robot_library(cls, v):
        """Fail fast at startup — only Browser Library is supported (E8/D1)."""
        if v.lower() == 'selenium':
            raise ValueError(
                "ROBOT_LIBRARY=selenium is no longer supported; this system "
                "generates Browser Library (Playwright) tests only. Remove the "
                "setting or set ROBOT_LIBRARY=browser."
            )
        if v.lower() != 'browser':
            raise ValueError(f"ROBOT_LIBRARY must be 'browser', got '{v}'")
        return v.lower()

    @validator('ARTIFACT_STORE')
    def validate_artifact_store(cls, v):
        """Validate that ARTIFACT_STORE is one of the supported backends."""
        if v.lower() not in ('local', 's3'):
            raise ValueError(f"ARTIFACT_STORE must be 'local' or 's3', got '{v}'")
        return v.lower()

    @validator('ENVIRONMENT')
    def validate_environment(cls, v):
        """Validate that ENVIRONMENT is 'development' or 'production'."""
        if v.lower() not in ('development', 'production'):
            raise ValueError(f"ENVIRONMENT must be 'development' or 'production', got '{v}'")
        return v.lower()

    @validator('MAX_AGENT_ITERATIONS')
    def validate_max_iterations(cls, v):
        """Validate that MAX_AGENT_ITERATIONS is between 1 and 5."""
        if v < 1 or v > 5:
            raise ValueError(f"MAX_AGENT_ITERATIONS must be between 1 and 5, got {v}")
        return v

    @validator('MAX_DRYRUN_FIXES')
    def validate_max_dryrun_fixes(cls, v):
        """Validate that MAX_DRYRUN_FIXES is between 0 (repair disabled) and 5."""
        if v < 0 or v > 5:
            raise ValueError(f"MAX_DRYRUN_FIXES must be between 0 and 5, got {v}")
        return v

    @validator('LLM_EMPTY_RESPONSE_MAX_RETRIES')
    def validate_llm_empty_response_max_retries(cls, v):
        """LLM_EMPTY_RESPONSE_MAX_RETRIES must be between 0 (disabled) and 5."""
        if v < 0 or v > 5:
            raise ValueError(f"LLM_EMPTY_RESPONSE_MAX_RETRIES must be between 0 and 5, got {v}")
        return v
    
    @validator('MAX_LOCATOR_STRATEGIES')
    def validate_max_locator_strategies(cls, v):
        """Validate that MAX_LOCATOR_STRATEGIES is between 1 and 50."""
        if v < 1 or v > 50:
            raise ValueError(f"MAX_LOCATOR_STRATEGIES must be between 1 and 50, got {v}")
        return v
    
    @validator('OBSERVABILITY_BACKEND')
    def validate_observability_backend(cls, v):
        if v.lower() not in ('postgres', 'grafana', 'otlp', 'none'):
            raise ValueError(f"OBSERVABILITY_BACKEND must be postgres, grafana, otlp, or none, got '{v}'")
        return v.lower()

    @validator('OPTIMIZATION_PATTERN_CONFIDENCE_THRESHOLD', 'OPTIMIZATION_CONTEXT_PRUNING_THRESHOLD')
    def validate_confidence_threshold(cls, v):
        """Validate that confidence thresholds are between 0.0 and 1.0."""
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"Confidence threshold must be between 0.0 and 1.0, got {v}")
        return v

    @validator('JWT_EXPIRY_HOURS')
    def validate_jwt_expiry_hours(cls, v):
        """1 hour to 30 days — zero/negative mints already-expired tokens."""
        if v < 1 or v > 720:
            raise ValueError(f"JWT_EXPIRY_HOURS must be between 1 and 720, got {v}")
        return v

    class Config:
        env_file = ".env"
        env_file_encoding = 'utf-8'
        extra = 'allow'  # Allow extra fields from .env file

settings = Settings()

# Connect timeout (seconds) for every psycopg connection/pool in the app.
# libpq's default is "wait forever": against an unreachable host (network
# partition, Docker Desktop's black-holed IPv6 binds) each connect would hang
# for minutes instead of failing fast into the existing degrade paths.
# Module constant, not a Settings field — it is an internal resilience bound,
# not a deployment knob.
PG_CONNECT_TIMEOUT_S = 5

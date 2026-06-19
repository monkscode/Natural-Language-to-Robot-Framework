import os
import sys
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Windows: reconfigure stdout/stderr to UTF-8 for emoji log compatibility.
if sys.platform.startswith('win'):
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    os.environ['PYTHONIOENCODING'] = 'utf-8'

# --- Structured Logging (must be FIRST — before any logger calls) ---
from src.backend.config.logging_config import setup_logging
setup_logging()

# Disable crewai tracing prompts to prevent log spam
os.environ['CREWAI_TRACING_ENABLED'] = 'false'

logger = logging.getLogger(__name__)
logger.info("Starting application with structured logging enabled at logs/application.log")

# --- LLM Observability (must be BEFORE any import that loads CrewAI/LiteLLM) ---
from src.backend.core.observability import init_observability
init_observability()

# This import chain pulls in endpoints → workflow_service → crew_ai → litellm.
# Observability must be initialized before this line so OpenLLMetry patches apply.
from src.backend.api.endpoints import router as api_router

# Auth (Phase 1): JWT/role guards + /auth router + Postgres users store.
from fastapi import Depends
from src.backend.core.config import settings
from src.backend.auth.jwt_utils import require_admin
from src.backend.auth.endpoints import auth_router
from src.backend.auth.db import init_auth_db, close_pool
from src.backend.auth.org_db import init_org_db

# --- FastAPI App ---
app = FastAPI(title="Mark 1 - AI Test Automation Platform")

# CORS — restricted to the SPA origins (dev Vite :5173, nginx container :3000,
# fastapi :5000). allow_credentials stays on for the Google OAuth state cookie.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Global Exception Handler ---
from src.backend.api.error_handlers import register_error_handlers
register_error_handlers(app)

# --- API Routers ---
# Auth routes (public entry points): /auth/register, /auth/login, /auth/me, /auth/google/*
app.include_router(auth_router)

# Generate/execute/feedback routes carry their own per-route guards (require_user).
app.include_router(api_router)

# Run history — every authenticated user; the endpoint scopes rows by role
# (own runs for regular users, all runs for validated admins).
from src.backend.api.history_endpoints import router as history_router
app.include_router(history_router, prefix="/api")

# Metrics dashboards — routes self-guard via is_dashboard_viewer (org-admin+).
from src.backend.api.workflow_metrics_endpoints import router as workflow_metrics_router
app.include_router(workflow_metrics_router, prefix="/api")

from src.backend.api.trace_endpoints import router as trace_router
app.include_router(trace_router, prefix="/api")  # routes self-guard via is_dashboard_viewer

from src.backend.api.learning_endpoints import router as learning_router
app.include_router(learning_router, prefix="/api/learning", dependencies=[Depends(require_admin)])

# --- Health Check Endpoints ---
from src.backend.api.health import health_check, api_health_check

app.get("/health")(health_check)
app.get("/api/health")(api_health_check)

# --- Report artifacts (generated Robot Framework reports) ---
# Reports are served by an explicit owner-gated route, not a StaticFiles mount.
# log.html records every keyword argument (including passwords typed during a
# test), so each file is authorized per-owner. The route parses one run_id that
# drives BOTH the ownership check and the file lookup, so the '//'/'..' parser-
# disagreement bypass the mount+middleware split allowed is structurally
# impossible. See src/backend/api/report_endpoints.py.
from src.backend.api.report_endpoints import router as report_router
app.include_router(report_router)

@app.on_event("startup")
async def startup_event():
    # Refuse to run with a missing/placeholder JWT secret — every minted token
    # would be forgeable. .env.example documents how to generate a real one.
    if settings.JWT_SECRET_KEY in ("", "change-me-in-production"):
        raise RuntimeError(
            "JWT_SECRET_KEY is unset or still the placeholder. Generate one with "
            "python -c \"import secrets; print(secrets.token_urlsafe(48))\" and "
            "set it in src/backend/.env (or the container environment)."
        )

    # Construct the artifact store once at startup: this ensures the staging
    # root exists and fails fast on an invalid backend config (e.g.
    # ARTIFACT_STORE=s3 with no bucket) instead of on the first report request.
    from src.backend.core.artifact_store import get_artifact_store
    get_artifact_store()

    logging.info("Application startup complete.")
    _check_learning_health()

    # Initialize the auth/users Postgres store. Best-effort: a DB outage must not
    # block the rest of the app from starting (auth fails until Postgres is up).
    try:
        init_auth_db()
        # Org tenancy lives in the same identity domain and must init AFTER users
        # (org_members references users). Same best-effort guard.
        init_org_db()
    except Exception as e:
        logging.warning(
            f"[AUTH] init_auth_db/init_org_db failed — auth unavailable until "
            f"Postgres is reachable: {e}"
        )

    # Clean up orphaned temp metrics files left by crashed/incomplete workflows
    try:
        from src.backend.core.temp_metrics_storage import get_temp_metrics_storage
        get_temp_metrics_storage().cleanup_old_files(max_age_hours=24)
    except Exception as e:
        logging.warning(f"Startup temp metrics cleanup failed (non-fatal): {e}")


def _check_learning_health():
    """Pre-flight validation for the learning store (Postgres + pgvector).

    Runs at startup when OPTIMIZATION_ENABLED=true. Ensures Postgres is
    reachable, the consolidated learning schema exists (created if missing),
    and the pgvector extension is installed. Logs warnings on failure but
    never blocks startup.
    """
    from src.backend.core.config import settings

    if not settings.OPTIMIZATION_ENABLED:
        logging.info("[LEARNING HEALTH] Learning system disabled (OPTIMIZATION_ENABLED=false)")
        return

    issues = []
    try:
        import psycopg
        from src.backend.core.config import PG_CONNECT_TIMEOUT_S
        from src.backend.crew_ai.optimization import pg_schema

        conn = psycopg.connect(
            settings.DATABASE_URL, autocommit=True,
            connect_timeout=PG_CONNECT_TIMEOUT_S)
        try:
            pg_schema.ensure_schema(conn)  # idempotent — creates if absent
            conn.execute("SELECT 1 FROM execution_records LIMIT 1")
            if not conn.execute(
                "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
            ).fetchone():
                issues.append("pgvector extension 'vector' is not installed")
        finally:
            conn.close()
    except Exception as e:
        issues.append(f"Postgres learning store unreachable: {e}")

    if issues:
        for issue in issues:
            logging.warning(f"[LEARNING HEALTH] {issue}")
        logging.warning(
            "[LEARNING HEALTH] Learning system may not function correctly. "
            "Fix the issues above or set OPTIMIZATION_ENABLED=false."
        )
    else:
        logging.info(
            "[LEARNING HEALTH] All checks passed — learning system ready (Postgres + pgvector)")

@app.on_event("shutdown")
async def shutdown_event():
    # Drain pending learning writes before exit
    try:
        from src.backend.crew_ai.optimization.learning_registry import get_feedback_loop
        feedback_loop = get_feedback_loop()
        if feedback_loop is not None:
            feedback_loop.write_queue.shutdown(timeout=5.0)
            logging.info("Learning write queue drained successfully.")
            feedback_loop.execution_memory.close()
            logging.info("Learning store connection closed.")
    except Exception as e:
        logging.warning(f"Learning write queue shutdown error: {e}")

    # Close the shared keyword-vector-store pool (no-op if never created).
    try:
        from src.backend.crew_ai.optimization.keyword_vector_store import (
            close_keyword_vector_store,
        )
        close_keyword_vector_store()
    except Exception as e:
        logging.warning(f"Keyword store shutdown error: {e}")

    # Close the auth Postgres pool.
    try:
        close_pool()
    except Exception as e:
        logging.warning(f"[AUTH] close_pool error: {e}")

    logging.info("Application shutdown complete.")

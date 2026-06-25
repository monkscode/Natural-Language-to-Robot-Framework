"""Per-IP rate limiting for the unauthenticated auth endpoints.

bcrypt's cost slows a single login attempt, but without a request cap an
attacker can still parallelize brute-force / credential-stuffing. slowapi adds a
per-client-IP cap on /auth/login, /auth/register, and /auth/forgot-password.

Storage is in-memory (per process) — correct for the single-container
deployment. A horizontally scaled deployment should point slowapi at Redis so
the counters are shared across replicas.

The client key comes from the proxy-set X-Real-IP / X-Forwarded-For headers (our
nginx sets them), so the limit is per real client rather than per nginx IP.
Behind an UNtrusted proxy these headers are spoofable; the trust assumption is
that the only hop in front of FastAPI is our own nginx, which overwrites them.

Referenced by: src/backend/main.py (limiter + handler wiring),
src/backend/auth/endpoints.py (per-route decorators).
Depends on: slowapi, src/backend/core/config.py.
"""
import logging

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from src.backend.core.config import settings

logger = logging.getLogger(__name__)


def client_ip(request: Request) -> str:
    """Best-effort real client IP for rate-limit keying.

    Precedence: X-Real-IP, then the first X-Forwarded-For hop, then the socket
    peer. Our nginx sets the first two; direct (no-proxy) dev falls back to the
    peer address.
    """
    real = request.headers.get("x-real-ip")
    if real and real.strip():
        return real.strip()
    fwd = request.headers.get("x-forwarded-for")
    if fwd and fwd.strip():
        return fwd.split(",")[0].strip()
    return get_remote_address(request)


def _auth_limit() -> str:
    """Read the limit at call time so config/tests can change it without re-import."""
    return settings.AUTH_RATE_LIMIT


# Module-level limiter shared by main.py (app.state) and the endpoint decorators.
# No default_limits: only the explicitly decorated auth routes are limited.
limiter = Limiter(
    key_func=client_ip,
    enabled=settings.AUTH_RATE_LIMIT_ENABLED,
    default_limits=[],
)


def auth_rate_limit():
    """Decorator for the sensitive auth endpoints (limit read dynamically)."""
    return limiter.limit(_auth_limit)

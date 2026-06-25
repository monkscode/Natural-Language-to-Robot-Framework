"""Startup security-posture checks for the auth subsystem.

Refuses to boot a *production* deployment with a forgeable JWT secret or
non-Secure auth cookies — the two misconfigurations that silently turn the
whole auth layer into theatre. In *development* these are warnings, not
failures, so local http dev keeps working.

The check is tiered so it can never be bypassed by a value that merely differs
from the literal placeholder:
  - ALWAYS fatal (any environment): an empty or placeholder JWT secret — every
    minted token would be forgeable.
  - PRODUCTION only: the secret must also be long and free of obvious dev
    markers, and COOKIE_SECURE must be on (auth cookies over HTTPS only).

Referenced by: src/backend/main.py (startup_event).
Depends on: src/backend/core/config.py.
"""
import logging

from src.backend.core.config import settings

logger = logging.getLogger(__name__)

# Secrets that are never acceptable, in any environment.
_PLACEHOLDER_SECRETS = frozenset({"", "change-me-in-production"})

# Production secret strength. token_urlsafe(48) yields 64 chars, so 32 is a
# comfortable floor that still rejects short/guessable values.
_MIN_SECRET_LEN = 32

# Substrings that mark a secret as a dev/sample value rather than a real random
# one. token_urlsafe output never contains these words; a value that does is a
# human-typed placeholder, not entropy.
_WEAK_SECRET_MARKERS = (
    "change", "dev-only", "dev_", "devsecret", "placeholder", "example",
    "localdev", "insecure", "test-secret", "changeme", "secret-key",
)


def _secret_is_weak(secret: str) -> bool:
    """True if the secret is empty/placeholder, too short, or carries a dev marker."""
    if secret in _PLACEHOLDER_SECRETS:
        return True
    if len(secret) < _MIN_SECRET_LEN:
        return True
    low = secret.lower()
    return any(marker in low for marker in _WEAK_SECRET_MARKERS)


def validate_security_posture() -> None:
    """Enforce the auth security invariants at startup.

    Raises RuntimeError when the posture is unsafe for the current environment.
    Always fatal: a missing/placeholder JWT secret. Production-only fatal: a weak
    JWT secret or COOKIE_SECURE off. In development the production checks downgrade
    to log warnings so http dev is unaffected.
    """
    secret = settings.JWT_SECRET_KEY
    is_prod = settings.ENVIRONMENT == "production"

    # Always fatal — a forgeable secret is never acceptable, dev or prod.
    if secret in _PLACEHOLDER_SECRETS:
        raise RuntimeError(
            "JWT_SECRET_KEY is unset or still the placeholder. Generate one with "
            'python -c "import secrets; print(secrets.token_urlsafe(48))" and set '
            "it in src/backend/.env (or the container environment)."
        )

    weak = _secret_is_weak(secret)

    if is_prod:
        if weak:
            raise RuntimeError(
                f"JWT_SECRET_KEY is too weak for production (must be >= {_MIN_SECRET_LEN} "
                "chars and not a dev/placeholder value). Generate one with "
                'python -c "import secrets; print(secrets.token_urlsafe(48))".'
            )
        if not settings.COOKIE_SECURE:
            raise RuntimeError(
                "COOKIE_SECURE must be true in production so auth cookies are sent "
                "only over HTTPS. Set COOKIE_SECURE=true."
            )
        logger.info("[AUTH] Security posture OK (production: strong secret + secure cookies).")
        return

    # Development: warn but allow, so local http dev is not blocked.
    if weak:
        logger.warning(
            "[AUTH] JWT_SECRET_KEY looks weak (a dev value or < %d chars). Allowed in "
            "development, but production (ENVIRONMENT=production) requires a strong "
            "random secret.", _MIN_SECRET_LEN,
        )
    if not settings.COOKIE_SECURE:
        logger.info(
            "[AUTH] COOKIE_SECURE is off — fine for local http dev; enable it behind HTTPS."
        )

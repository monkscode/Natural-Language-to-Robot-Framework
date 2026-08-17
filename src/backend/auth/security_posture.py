"""Startup security-posture checks for the auth subsystem.

Refuses to boot a *production* deployment with a forgeable JWT secret or
non-Secure auth cookies — the two misconfigurations that silently turn the
whole auth layer into theatre. In *development* these are warnings, not
failures, so local http dev keeps working.

The check is tiered so it can never be bypassed by a value that merely differs
from the literal placeholder:
  - PRODUCTION: an empty or placeholder JWT secret is fatal, the secret must
    also be long and free of obvious dev markers, and COOKIE_SECURE must be on
    (auth cookies over HTTPS only). The secret must be supplied explicitly — a
    provisioned development secret is never adopted here.
  - DEVELOPMENT: an empty or placeholder secret is provisioned instead of being
    fatal — a strong random secret is generated and persisted to _SECRET_PATH so
    a `docker compose up` works with no manual step and sessions survive a
    restart. Everything else only warns.

Referenced by: src/backend/main.py (startup_event).
Depends on: src/backend/core/config.py.
"""
import logging
import os
import secrets
from pathlib import Path

from src.backend.core.config import settings

logger = logging.getLogger(__name__)

# Secrets that are never acceptable as a configured value: in production they
# are fatal, in development they trigger provisioning.
_PLACEHOLDER_SECRETS = frozenset({"", "change-me-in-production"})

# Where a provisioned development secret is persisted so logins survive a
# restart. Relative to the process CWD — the repo root locally, /app in the
# image — the same convention as the fastembed cache in
# crew_ai/optimization/embedding.py. data/ is bind-mounted by docker-compose.yml
# and gitignored (.gitignore: data/*), so the secret never reaches git.
_SECRET_PATH = Path("data") / "jwt_secret"

# Entropy per generated secret. 48 bytes -> 64 urlsafe chars, comfortably above
# _MIN_RANDOM_CHARS.
_GENERATED_ENTROPY_BYTES = 48

# Production secret strength: the minimum character length a JWT secret must
# reach to be accepted. token_urlsafe(48) yields 64 chars, so 32 is a
# comfortable floor that still rejects short/guessable values. Named for the
# length threshold it is (a plain int) — deliberately NOT "*secret*"/"*key*",
# both so the name stops misleading readers into thinking it holds a secret and
# so logging it cannot be misread (by humans or scanners) as logging one.
_MIN_RANDOM_CHARS = 32

# Substrings that mark a secret as a dev/sample value rather than a real random
# one. token_urlsafe draws from the base64url alphabet, which includes '_', so
# it CAN emit 'dev_' by chance — measured at ~1 in 34,000 over 2M samples.
# _mint_secret redraws on that, so a provisioned secret never carries a marker;
# without the redraw it would pass in development and then fail the production
# check below for no reason a reader could diagnose.
_WEAK_SECRET_MARKERS = (
    "change", "dev-only", "dev_", "devsecret", "placeholder", "example",
    "localdev", "insecure", "test-secret", "changeme", "secret-key",
)


def _secret_is_weak(secret: str) -> bool:
    """True if the secret is empty/placeholder, too short, or carries a dev marker."""
    if secret in _PLACEHOLDER_SECRETS:
        return True
    if len(secret) < _MIN_RANDOM_CHARS:
        return True
    low = secret.lower()
    return any(marker in low for marker in _WEAK_SECRET_MARKERS)


def _mint_secret() -> str:
    """Return a fresh random secret that _secret_is_weak accepts."""
    while True:
        candidate = secrets.token_urlsafe(_GENERATED_ENTROPY_BYTES)
        if not _secret_is_weak(candidate):
            return candidate


def _read_secret_file() -> str | None:
    """Return the persisted development secret, or None if absent or unusable.

    ValueError covers UnicodeDecodeError: a truncated or corrupted file must be
    replaced, not crash the boot it exists to keep working.
    """
    try:
        stored = _SECRET_PATH.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    return stored if not _secret_is_weak(stored) else None


def _write_secret_file(secret: str) -> str:
    """Persist the secret, returning the value that actually landed on disk.

    The create is O_EXCL (0600), so concurrent starters converge on the first
    writer's key rather than each signing with its own — an atomic
    write-then-replace would be last-writer-wins, which does not converge. A
    file that already exists is adopted when usable; only an unusable one
    (empty, truncated, corrupt) is overwritten, and there any winner is correct
    because every writer is replacing garbage.
    """
    _SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(_SECRET_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        landed = _read_secret_file()
        if landed is not None:
            return landed
        fd = os.open(_SECRET_PATH, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(secret)
    return secret


def _provision_development_secret() -> str:
    """Return the development secret, generating and persisting one on first boot."""
    stored = _read_secret_file()
    if stored is not None:
        return stored

    secret = _mint_secret()
    try:
        landed = _write_secret_file(secret)
    except OSError as e:
        logger.warning(
            "[AUTH] Generated a development JWT secret but could not persist it to "
            "%s (%s). The app runs, but every restart mints a new one and logs "
            "everybody out. Set JWT_SECRET_KEY explicitly to stop that.",
            _SECRET_PATH, e,
        )
        return secret

    logger.info(
        "[AUTH] No JWT_SECRET_KEY configured — generated a development secret and "
        "stored it at %s. Any deployment that does not persist that path (plain "
        "docker run, k8s, more than one replica) must set JWT_SECRET_KEY instead.",
        _SECRET_PATH,
    )
    return landed


def validate_security_posture() -> None:
    """Enforce the auth security invariants at startup.

    Raises RuntimeError when the posture is unsafe for the current environment.
    Production-only fatal: a missing/placeholder or weak JWT secret, or
    COOKIE_SECURE off. In development a missing/placeholder secret is provisioned
    (see _provision_development_secret) and the remaining checks downgrade to log
    warnings, so http dev is unaffected.
    """
    secret = settings.JWT_SECRET_KEY
    # Normalize before comparing: "Production", "production " or " PRODUCTION"
    # must all trip the production-only hard failures, not slip past an exact
    # lowercase match and silently downgrade them to warnings.
    is_prod = (settings.ENVIRONMENT or "").strip().lower() == "production"

    if secret in _PLACEHOLDER_SECRETS:
        if is_prod:
            # Deliberately do NOT adopt a provisioned development secret here: a
            # production key belongs in the environment or a secret manager, not
            # in a bind-mounted file that a stray `rm -rf data/` would revoke.
            # Name the path (never the value) so the operator can copy it across
            # and keep existing sessions valid.
            hint = (
                f" A development secret exists at {_SECRET_PATH} — reuse that value "
                "to keep existing sessions valid." if _SECRET_PATH.exists() else ""
            )
            raise RuntimeError(
                "JWT_SECRET_KEY is unset or still the placeholder. Production requires "
                "an explicit secret — generate one with "
                'python -c "import secrets; print(secrets.token_urlsafe(48))" and inject '
                "it via the container environment or your secret manager." + hint
            )
        secret = _provision_development_secret()
        settings.JWT_SECRET_KEY = secret

    weak = _secret_is_weak(secret)

    if is_prod:
        if weak:
            raise RuntimeError(
                f"JWT_SECRET_KEY is too weak for production (must be >= {_MIN_RANDOM_CHARS} "
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
            "random secret.", _MIN_RANDOM_CHARS,
        )
    if not settings.COOKIE_SECURE:
        logger.info(
            "[AUTH] COOKIE_SECURE is off — fine for local http dev; enable it behind HTTPS."
        )

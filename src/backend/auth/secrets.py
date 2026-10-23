"""Encrypted per-org secret store: the rules, the .env reader and the table.

Values are Fernet-encrypted with SECRETS_ENCRYPTION_KEY and never leave this
module except through get_values(). Every message and log line here names
secrets and line numbers only, never a value. Each of list_names / set_many /
delete / get_values checks the key first and raises SecretsUnavailable when it
is unset or malformed, before any DB work.

Referenced by: auth/secrets_endpoints.py, query check, runner hop (later tasks).
Depends on: auth/db.py (pool), auth/org_repository.py, core/config.py.
"""
import io
import logging
import re
from collections.abc import Iterable

from cryptography.fernet import Fernet
from dotenv.parser import parse_stream
from fastapi import HTTPException

from src.backend.auth.db import get_pool
from src.backend.auth.org_repository import OrgRepository
from src.backend.core.config import settings

logger = logging.getLogger(__name__)

SECRET_ENV_PREFIX = "NLRF_SECRET_"
MAX_VALUE_CHARS = 1000
MAX_UPLOAD_BYTES = 65536
MAX_SECRETS_PER_ORG = 200

_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_QUERY_RE = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_]{0,63})\s*\}\}")
_CODE_RE = re.compile(
    r"%(?:\{" + SECRET_ENV_PREFIX + r"([A-Z][A-Z0-9_]{0,63})\}"
    r"|" + SECRET_ENV_PREFIX + r"([A-Z][A-Z0-9_]{0,63}))"
)

_DDL = """
CREATE TABLE IF NOT EXISTS org_secrets (
    org_id     UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    value_enc  BYTEA NOT NULL,
    updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, name)
)
"""


class SecretsUnavailable(Exception):
    """SECRETS_ENCRYPTION_KEY is unset or not a valid Fernet key."""


class SecretRuleError(ValueError):
    """A name, value, file or count broke a rule. The message never holds a value."""


def init_secrets_db() -> None:
    """Create org_secrets if absent. Idempotent; run at startup AFTER init_org_db()."""
    with get_pool().connection() as conn:
        conn.execute(_DDL)
        conn.commit()
    logger.info("[AUTH] org_secrets table ready")


def _fernet() -> Fernet:
    key = settings.SECRETS_ENCRYPTION_KEY
    try:
        return Fernet(key.encode())
    except Exception as exc:  # noqa: BLE001 — empty / malformed key; never echo it
        raise SecretsUnavailable(
            "Secrets are not set up on this server (SECRETS_ENCRYPTION_KEY)"
        ) from exc


def normalize_name(raw: str) -> str:
    if not _NAME_RE.fullmatch(raw):
        raise SecretRuleError(
            "A secret name starts with a letter and holds only letters, digits and "
            "underscores, up to 64 characters"
        )
    return raw.upper()


def check_value(value: str) -> None:
    if not 1 <= len(value) <= MAX_VALUE_CHARS:
        raise SecretRuleError(f"A secret value is 1 to {MAX_VALUE_CHARS:,} characters")
    if "\r" in value or "\n" in value:
        raise SecretRuleError("A secret value cannot hold a line break")


def parse_env_file(text: str) -> list[tuple[str, str]]:
    """Read .env text into (NAME, value) pairs; one bad line refuses the whole file."""
    if len(text.encode("utf-8")) > MAX_UPLOAD_BYTES:
        raise SecretRuleError(f"The file is larger than {MAX_UPLOAD_BYTES:,} bytes")
    pairs: dict[str, str] = {}
    for binding in parse_stream(io.StringIO(text.removeprefix("﻿"))):
        if binding.key is None and not binding.error:
            continue  # comment or blank line
        line = binding.original.line
        if binding.error or binding.value is None:
            raise SecretRuleError(f"Line {line}: not a NAME=value line")
        try:
            name = normalize_name(binding.key)
            check_value(binding.value)
        except SecretRuleError as exc:
            raise SecretRuleError(f"Line {line}: {exc}") from None
        pairs[name] = binding.value  # a repeated name: the last one wins
    return list(pairs.items())


def _unique(names: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(names))


def secret_names_in_query(text: str) -> list[str]:
    return _unique(m.group(1).upper() for m in _QUERY_RE.finditer(text))


def secret_names_in_code(robot_code: str) -> list[str]:
    return _unique(m.group(1) or m.group(2) for m in _CODE_RE.finditer(robot_code))


def org_of(user_id: str | None) -> str | None:
    """The caller's org, re-read from org_members (oldest membership), or None."""
    if not user_id:
        return None
    orgs = OrgRepository().get_orgs_for_user(user_id)
    return orgs[0]["org_id"] if orgs else None


def require_query_secrets(user_query: str, user: dict | None) -> None:
    """Raise a 400 before any generation when the query names a {{NAME}} the
    caller's org does not hold. No braces: returns at once, no key or DB work."""
    names = secret_names_in_query(user_query)
    if not names:
        return
    if user is None:
        raise HTTPException(400, "Secrets in a request need a signed-in user.")
    try:
        _fernet()  # key problems come first, whatever the org holds
        org_id = org_of(user.get("user_id"))
        held = {r["name"] for r in list_names(org_id)} if org_id else set()
    except SecretsUnavailable as exc:
        raise HTTPException(400, str(exc)) from None
    missing = [f"{{{{{n}}}}}" for n in names if n not in held]
    if len(missing) == 1:
        raise HTTPException(400, f"Unknown secret {missing[0]}: add it on the Secrets page first.")
    if missing:
        raise HTTPException(
            400, f"Unknown secrets {', '.join(missing)}: add them on the Secrets page first.")


def list_names(org_id: str) -> list[dict]:
    _fernet()
    with get_pool().connection() as conn:
        return conn.execute(
            "SELECT name, updated_at FROM org_secrets WHERE org_id = %s ORDER BY name",
            (org_id,),
        ).fetchall()


def set_many(org_id: str, user_id: str, pairs: Iterable[tuple[str, str]]) -> tuple[list[str], list[str]]:
    """Save pairs in one transaction; returns (added, replaced) names. Refuses,
    saving nothing, when the org would hold more than 200 secrets."""
    fernet = _fernet()
    unique: dict[str, str] = {}
    for name, value in pairs:
        check_value(value)
        unique[normalize_name(name)] = value
    added: list[str] = []
    replaced: list[str] = []
    with get_pool().connection() as conn:
        for name, value in unique.items():
            row = conn.execute(
                "INSERT INTO org_secrets (org_id, name, value_enc, updated_by) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (org_id, name) DO UPDATE SET value_enc = EXCLUDED.value_enc, "
                "updated_by = EXCLUDED.updated_by, updated_at = now() "
                "RETURNING (xmax = 0) AS inserted",
                (org_id, name, fernet.encrypt(value.encode("utf-8")), user_id),
            ).fetchone()
            (added if row["inserted"] else replaced).append(name)
        count = conn.execute(
            "SELECT count(*) AS n FROM org_secrets WHERE org_id = %s", (org_id,)
        ).fetchone()["n"]
        if count > MAX_SECRETS_PER_ORG:
            conn.rollback()
            raise SecretRuleError(
                f"An organisation holds at most {MAX_SECRETS_PER_ORG} secrets; nothing was saved"
            )
        conn.commit()
    logger.info("[AUTH] secrets saved: %d added, %d replaced", len(added), len(replaced))
    return added, replaced


def delete(org_id: str, name: str) -> bool:
    _fernet()
    with get_pool().connection() as conn:
        cur = conn.execute(
            "DELETE FROM org_secrets WHERE org_id = %s AND name = %s", (org_id, name.upper())
        )
        conn.commit()
    return cur.rowcount > 0


def get_values(org_id: str, names: Iterable[str]) -> dict[str, str]:
    """Decrypted values for the names this org holds; missing names are absent."""
    fernet = _fernet()
    wanted = [n.upper() for n in names]
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT name, value_enc FROM org_secrets WHERE org_id = %s AND name = ANY(%s)",
            (org_id, wanted),
        ).fetchall()
    return {r["name"]: fernet.decrypt(bytes(r["value_enc"])).decode("utf-8") for r in rows}

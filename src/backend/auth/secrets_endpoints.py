"""Org secrets API: list names, set one, upload a .env file, delete one.

Any member of an org may use all four routes (owner decision, 2026-10-22). The
org is re-read from org_members, never taken from the token. A value is never
returned: lists carry names and timestamps only, and no answer, error or audit
row holds one. The JSON body is read by hand instead of through a pydantic
model because FastAPI's default 422 echoes each rejected field's input, which
would send a value back; here a malformed body gets one plain sentence.

Referenced by: main.py (router registration), frontend SecretsPage.
Depends on: auth/secrets.py (store + rules), auth/jwt_utils.py (require_user),
core/audit_log.py (the floor that reads request.state.audit_detail).
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from src.backend.auth import secrets as store
from src.backend.auth.jwt_utils import require_user

logger = logging.getLogger(__name__)

secrets_router = APIRouter(prefix="/api/secrets", tags=["secrets"])


def _org_of_caller(user: dict | None) -> tuple[str, str]:
    """(org_id, user_id) of the caller; 401 with no identity, 403 with no org."""
    if user is None:
        raise HTTPException(401, "Sign in to manage secrets")
    org_id = store.org_of(user["user_id"])
    if org_id is None:
        raise HTTPException(403, "You are not a member of an organisation")
    return org_id, user["user_id"]


async def _field(request: Request, field: str) -> str:
    """The one string field of a JSON object body, or 422 with a fixed sentence."""
    try:
        body = await request.json()
    except ValueError:
        body = None
    value = body.get(field) if isinstance(body, dict) else None
    if not isinstance(value, str):
        raise HTTPException(422, f'Send a JSON object like {{"{field}": "text"}}')
    return value


async def _value(request: Request) -> str:
    return await _field(request, "value")


async def _content(request: Request) -> str:
    return await _field(request, "content")


def _guarded(fn, *args):
    """Run a store call; its rule sentence is a 400, an unset key a 503."""
    try:
        return fn(*args)
    except store.SecretsUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
    except store.SecretRuleError as exc:
        raise HTTPException(400, str(exc)) from None


@secrets_router.get("")
def list_secrets(user: dict | None = Depends(require_user)):
    org_id, _ = _org_of_caller(user)
    return _guarded(store.list_names, org_id)


@secrets_router.post("/upload")
def upload_secrets(
    request: Request,
    user: dict | None = Depends(require_user),
    content: str = Depends(_content),
):
    org_id, user_id = _org_of_caller(user)
    added, replaced = _guarded(store.set_many, org_id, user_id, _guarded(store.parse_env_file, content))
    request.state.audit_detail = {"org_id": org_id, "names": added + replaced}
    return {"added": added, "replaced": replaced}


@secrets_router.put("/{name}")
def set_secret(
    name: str,
    request: Request,
    user: dict | None = Depends(require_user),
    value: str = Depends(_value),
):
    org_id, user_id = _org_of_caller(user)
    stored = _guarded(store.normalize_name, name)
    _, replaced = _guarded(store.set_many, org_id, user_id, [(stored, value)])
    request.state.audit_detail = {"org_id": org_id, "names": [stored]}
    return {"name": stored, "replaced": bool(replaced)}


@secrets_router.delete("/{name}", status_code=204)
def delete_secret(name: str, request: Request, user: dict | None = Depends(require_user)):
    org_id, _ = _org_of_caller(user)
    stored = name.upper()
    if not _guarded(store.delete, org_id, stored):
        raise HTTPException(404, "No secret with that name")
    request.state.audit_detail = {"org_id": org_id, "names": [stored]}
    return Response(status_code=204)

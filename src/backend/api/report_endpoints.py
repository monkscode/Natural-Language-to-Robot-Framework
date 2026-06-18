"""Authenticated, owner-scoped serving of Robot Framework run artifacts.

Referenced by:
    src.backend.main — registers this router in place of the old
    StaticFiles("/reports") mount + reports-auth middleware.
Depends on:
    src.backend.auth.jwt_utils.authorize_report_access — owner-or-admin gate.
    src.backend.core.artifact_store.get_artifact_store — backend-agnostic
        artifact resolution + serving (local FileResponse / S3 StreamingResponse).

Why a route and not a StaticFiles mount:
    The mount split one URL across two parsers (auth middleware vs StaticFiles),
    which disagreed on '//' and '..', letting any logged-in user read another
    user's report — and log.html records every keyword argument, including
    passwords typed during a test. A single {run_id} path parameter is parsed
    ONCE here and drives BOTH the ownership check and the file lookup, so that
    whole bug class is structurally impossible.
"""

import asyncio

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from src.backend.auth.jwt_utils import authorize_report_access
from src.backend.core.artifact_store import get_artifact_store

router = APIRouter()


@router.api_route("/reports/{run_id}/{file_path:path}", methods=["GET", "HEAD"])
async def serve_report_file(run_id: str, file_path: str, request: Request) -> Response:
    """Owner-or-admin gated download of one run artifact.

    The blocking ownership lookup (Postgres) and the blocking store read
    (local stat or S3 GetObject) both run off the event loop, as the old
    middleware did, so report pages (log.html pulls several sub-resources) and
    the SSE streams on the loop never stall.
    """
    denied = await asyncio.to_thread(authorize_report_access, request, run_id)
    if denied is not None:
        return denied
    store = get_artifact_store()
    resp = await asyncio.to_thread(
        store.serve_artifact, run_id, file_path, request.headers.get("range"))
    if resp is None:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    # CSP sandbox: report HTML is user-generated (Robot's Log html=True) and is
    # served same-origin with the SPA. An opaque origin makes localStorage
    # unreachable (no token theft) AND makes the document's requests cross-site
    # (no same-origin confused-deputy API calls). allow-scripts keeps Robot's log
    # app interactive; the ABSENCE of allow-same-origin is what makes the origin
    # opaque — do not add it. Applied uniformly (a no-op for PNG/XML) so every
    # served HTML, including attacker-authored, is sandboxed.
    resp.headers["Content-Security-Policy"] = "sandbox allow-scripts allow-popups"
    return resp

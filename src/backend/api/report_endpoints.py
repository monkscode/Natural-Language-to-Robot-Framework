"""Authenticated, owner-scoped serving of Robot Framework run artifacts.

Referenced by:
    src.backend.main — registers this router in place of the old
    StaticFiles("/reports") mount + reports-auth middleware.
Depends on:
    src.backend.auth.jwt_utils.authorize_report_access — owner-or-admin gate.
    src.backend.services.docker_service.ROBOT_TESTS_DIR — the artifact root.

Why a route and not a StaticFiles mount:
    The mount split one URL across two parsers. The auth middleware parsed the
    run_id out of request.url.path one way; StaticFiles resolved the file
    another. They disagreed on '//' (an empty leading segment) and '..' (parent
    traversal), so any logged-in user could read another user's report — and
    log.html records every keyword argument, including passwords typed during a
    test. A single {run_id} path parameter is parsed ONCE here and drives BOTH
    the ownership check and the file lookup, so that disagreement — the whole
    bug class, not just the reported double slash — is structurally impossible.
"""

import asyncio
import os
import uuid

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from src.backend.auth.jwt_utils import authorize_report_access
from src.backend.services.docker_service import ROBOT_TESTS_DIR

router = APIRouter()


def resolve_report_file(run_id: str, file_path: str) -> str | None:
    """Safe-join *file_path* under ROBOT_TESTS_DIR/<run_id>.

    Returns the absolute path of an existing file contained in the run
    directory, or None when: run_id is not a UUID; the resolved path escapes
    the run directory ('..', an absolute file_path, or a symlink target); or
    the target is missing or a directory (e.g. a bare /reports/<id>/ request).
    realpath resolves symlinks before the containment check, so this is strictly
    stronger than StaticFiles' default traversal protection.
    """
    try:
        run_id = str(uuid.UUID(run_id))
    except ValueError:
        return None
    run_root = os.path.realpath(os.path.join(ROBOT_TESTS_DIR, run_id))
    target = os.path.realpath(os.path.join(run_root, file_path))
    if target != run_root and not target.startswith(run_root + os.sep):
        return None  # escaped the run directory
    if not os.path.isfile(target):
        return None  # missing, or a directory
    return target


@router.api_route("/reports/{run_id}/{file_path:path}", methods=["GET", "HEAD"])
async def serve_report_file(run_id: str, file_path: str, request: Request) -> Response:
    """Owner-or-admin gated download of one run artifact.

    The blocking ownership lookup (Postgres) runs off the event loop, as the old
    middleware did, so report pages (log.html pulls several sub-resources) and
    the SSE streams on the loop never stall.
    """
    denied = await asyncio.to_thread(authorize_report_access, request, run_id)
    if denied is not None:
        return denied
    path = resolve_report_file(run_id, file_path)
    if path is None:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    # private: an authorization-gated artifact must never be cached by a shared
    # proxy and then served to a different user.
    return FileResponse(path, headers={"Cache-Control": "private"})

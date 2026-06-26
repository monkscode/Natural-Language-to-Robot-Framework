"""Execution boundary service: the ONLY process that talks to the Docker socket.

It exposes a minimal HTTP contract over the container operations FastAPI used to
perform inline. Every container_config is built server-side by the reused
docker_service/dryrun_service primitives — callers never supply an image, a bind
path, a command, or privilege flags. run_id/test_filename are validated to safe
path components. This is what makes a FastAPI compromise unable to mount the host.

Run as: uvicorn src.backend.runner_exec.app:app --host 0.0.0.0 --port 4998

Referenced by: docker-compose.yml (runner-exec), run.sh, RunnerExecClient.
Depends on: services/docker_service.py, services/dryrun_service.py,
runner_exec/validation.py.
"""
import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.backend.runner_exec.validation import safe_run_id, safe_test_filename
from src.backend.services.docker_service import (
    get_docker_client,
    build_image,
    run_test_in_container,
    rebuild_image,
    get_docker_status,
    cleanup_test_containers,
)
from src.backend.services.dryrun_service import run_dryrun_in_container

logger = logging.getLogger(__name__)
app = FastAPI(title="nlrf-runner-exec")


class ExecuteRequest(BaseModel):
    run_id: str
    test_filename: str


class DryrunRequest(BaseModel):
    run_id: str
    code: str


def _validated(value: str, validator) -> str:
    try:
        return validator(value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/health")
def health() -> dict:
    try:
        get_docker_client()  # pings the socket
        return {"status": "ok"}
    except Exception as e:  # noqa: BLE001 — health must not raise
        return {"status": "unavailable", "error": str(e)}


@app.post("/ensure-image")
def ensure_image() -> dict:
    client = get_docker_client()
    for _event in build_image(client):
        pass  # consume the generator to guarantee the image is present
    return {"status": "ready"}


@app.post("/execute")
def execute(req: ExecuteRequest) -> dict:
    run_id = _validated(req.run_id, safe_run_id)
    test_filename = _validated(req.test_filename, safe_test_filename)
    client = get_docker_client()
    try:
        # run_test_in_container raises RuntimeError only on infra/system failure;
        # surface its detailed message (which carries the container's stderr) so
        # the FastAPI side can show it instead of a generic 500.
        return run_test_in_container(client, run_id, test_filename)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/dryrun")
def dryrun(req: DryrunRequest) -> dict:
    run_id = _validated(req.run_id, safe_run_id)
    client = get_docker_client()
    try:
        return run_dryrun_in_container(client, run_id, req.code)
    except RuntimeError as e:
        # Infra failure (no output.xml / timeout). FastAPI's validate_and_repair
        # turns the resulting hop error into dryrun_status='unverified'.
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/rebuild-image")
def rebuild() -> dict:
    return rebuild_image(get_docker_client())


@app.get("/docker-status")
def docker_status() -> dict:
    return get_docker_status(get_docker_client())


@app.post("/cleanup")
def cleanup() -> dict:
    return cleanup_test_containers(get_docker_client())

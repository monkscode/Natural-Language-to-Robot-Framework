"""Pluggable storage for Robot Framework run artifacts.

Two tiers (see docs/superpowers/specs/2026-06-17-artifact-store-design.md):
  STAGING — a real local directory in every backend; the Docker test container
            bind-mounts it and Robot Framework writes into it from inside.
  DURABLE — backend-specific persistence/read/serve (local disk or S3).

The backend is chosen per deployment by settings.ARTIFACT_STORE, exactly as
settings.MODEL_PROVIDER chooses an LLM backend.

Referenced by: services/docker_service.py, services/workflow_service.py,
services/dryrun_service.py, api/report_endpoints.py, api/history_endpoints.py,
main.py.
Depends on: core/config.py (settings).
"""

import logging
import mimetypes
import os
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from threading import Lock

from fastapi import Response
from fastapi.responses import FileResponse, StreamingResponse

from src.backend.core.config import settings

logger = logging.getLogger(__name__)

# <repo-root>/robot_tests — the local staging base. Computed here (NOT imported
# from docker_service) so this module is the single owner of the path. This file
# is src/backend/core/artifact_store.py, so parents[3] is the repo root.
STAGING_ROOT = Path(__file__).resolve().parents[3] / "robot_tests"


class ArtifactStore(ABC):
    """Shared local staging + abstract durable operations."""

    def __init__(self, staging_root: Path):
        self.staging_root = Path(staging_root)
        self.staging_root.mkdir(parents=True, exist_ok=True)

    # ── STAGING (shared, always local — the container's bind-mount source) ──
    def run_dir(self, run_id: str, create: bool = False) -> Path:
        """The run's local staging directory (staging_root/<run_id>)."""
        d = self.staging_root / run_id
        if create:
            d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _safe_subpath(base: Path, relpath: str) -> Path | None:
        """Resolve *relpath* under *base*. Returns None when the result escapes
        (via '..', an absolute path, or a symlink target). realpath resolves
        symlinks before the containment check — strictly stronger than
        StaticFiles' default traversal protection."""
        base_real = os.path.realpath(base)
        target = os.path.realpath(os.path.join(base_real, relpath))
        if target != base_real and not target.startswith(base_real + os.sep):
            return None
        return Path(target)

    @staticmethod
    def _valid_run_id(run_id: str) -> str | None:
        try:
            return str(uuid.UUID(run_id))
        except ValueError:
            return None

    # ── DURABLE (backend-specific) ──
    @abstractmethod
    def read_text(self, run_id: str, relpath: str) -> str | None:
        """One artifact as text, or None if absent/escaping/unreadable."""

    @abstractmethod
    def serve_artifact(self, run_id: str, relpath: str,
                       range_header: str | None = None) -> Response | None:
        """A Starlette Response for the artifact, or None for 404/escape."""

    @abstractmethod
    def persist_run(self, run_id: str) -> None:
        """Make a finished run's artifacts durable. No-op for local."""


class LocalArtifactStore(ArtifactStore):
    """Durable == staging. persist_run is a no-op (already on local disk)."""

    def read_text(self, run_id: str, relpath: str) -> str | None:
        rid = self._valid_run_id(run_id)
        if rid is None:
            return None
        target = self._safe_subpath(self.run_dir(rid), relpath)
        if target is None or not target.is_file():
            return None
        try:
            return target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def serve_artifact(self, run_id: str, relpath: str,
                       range_header: str | None = None) -> Response | None:
        rid = self._valid_run_id(run_id)
        if rid is None:
            return None
        target = self._safe_subpath(self.run_dir(rid), relpath)
        if target is None or not target.is_file():
            return None
        # FileResponse natively handles Range/416, ETag, Last-Modified, content
        # types and HEAD. private: an authorized artifact must never be cached by
        # a shared proxy and served to another user.
        return FileResponse(str(target), headers={"Cache-Control": "private"})

    def persist_run(self, run_id: str) -> None:
        return None


class S3ArtifactStore(ArtifactStore):
    """Local staging + durable S3. Reads/serve are staging-first, then bucket,
    so the immediate post-run read hits local disk and a replica that never ran
    the test falls through to the bucket. persist_run uploads the run dir and
    writes a .complete marker LAST: an out-of-band ops signal (an interrupted
    upload lacks it) for bucket inspection and lifecycle tooling. The app itself
    never reads it — read_text/serve_artifact do not gate on its presence."""

    def __init__(self, staging_root: Path):
        super().__init__(staging_root)
        if not settings.ARTIFACT_S3_BUCKET:
            raise ValueError("ARTIFACT_STORE=s3 requires ARTIFACT_S3_BUCKET")
        try:
            import boto3  # lazy: only the S3 backend needs it (optional 's3' extra)
        except ImportError as e:
            raise RuntimeError(
                "ARTIFACT_STORE=s3 requires boto3. Install the 's3' extra: "
                "pip install -e '.[s3]' (or rebuild the FastAPI image with "
                "--build-arg INSTALL_S3=true)."
            ) from e
        self.bucket = settings.ARTIFACT_S3_BUCKET
        self.prefix = settings.ARTIFACT_S3_PREFIX.strip("/")
        kwargs = {}
        if settings.ARTIFACT_S3_REGION:
            kwargs["region_name"] = settings.ARTIFACT_S3_REGION
        if settings.ARTIFACT_S3_ENDPOINT_URL:
            kwargs["endpoint_url"] = settings.ARTIFACT_S3_ENDPOINT_URL
        self._s3 = boto3.client("s3", **kwargs)

    def _key(self, run_id: str, relpath: str) -> str:
        return f"{self.prefix}/{run_id}/{relpath}".lstrip("/")

    def _durable_key(self, run_id: str, target: Path) -> str:
        """Bucket key for a containment-resolved *target*. Derives the relpath
        from the resolved path (not the caller's raw relpath) so it matches the
        normalized key persist_run uploads (path.relative_to(d).as_posix()); a
        raw relpath like 'a/../b' would otherwise become the literal key
        '.../a/../b' and miss the uploaded '.../b'."""
        base_real = os.path.realpath(self.run_dir(run_id))
        rel = os.path.relpath(os.path.realpath(target), base_real)
        return self._key(run_id, Path(rel).as_posix())

    def persist_run(self, run_id: str) -> None:
        rid = self._valid_run_id(run_id)
        if rid is None:
            return
        d = self.run_dir(rid)
        if not d.is_dir():
            logger.warning("[ARTIFACT_STORE] persist_run: no staging dir for %s", rid)
            return
        try:
            for path in sorted(d.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(d).as_posix()
                # rglob + is_file() follow symlinks, so a symlink planted in the
                # run dir (paste-and-execute Robot code can create one inside the
                # bind-mounted container) could resolve outside the run scope.
                # Re-resolve and containment-check before upload so a crafted
                # symlink cannot exfiltrate host files into the bucket.
                safe = self._safe_subpath(d, rel)
                if safe is None or not safe.is_file():
                    logger.warning(
                        "[ARTIFACT_STORE] skipping unsafe path during persist: %s", path)
                    continue
                self._s3.upload_file(str(safe), self.bucket, self._key(rid, rel))
            # Ops-only completeness marker (see class docstring); not read by the app.
            self._s3.put_object(
                Bucket=self.bucket, Key=self._key(rid, ".complete"), Body=b"")
            logger.info("[ARTIFACT_STORE] persisted run %s to s3://%s/%s/%s/",
                        rid, self.bucket, self.prefix, rid)
        except Exception as e:
            # A failed upload must not fail the user's (already complete) run.
            logger.error("[ARTIFACT_STORE] persist_run failed for %s: %s", rid, e)

    def read_text(self, run_id: str, relpath: str) -> str | None:
        rid = self._valid_run_id(run_id)
        if rid is None:
            return None
        target = self._safe_subpath(self.run_dir(rid), relpath)
        if target is None:
            return None  # escape attempt
        # staging-first: the immediate post-run read hits local disk.
        if target.is_file():
            try:
                return target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                pass
        # bucket fallback (a replica that never ran the test, or cleaned staging).
        try:
            obj = self._s3.get_object(Bucket=self.bucket, Key=self._durable_key(rid, target))
            return obj["Body"].read().decode("utf-8")
        except Exception:
            return None

    def serve_artifact(self, run_id: str, relpath: str,
                       range_header: str | None = None) -> Response | None:
        rid = self._valid_run_id(run_id)
        if rid is None:
            return None
        target = self._safe_subpath(self.run_dir(rid), relpath)
        if target is None:
            return None  # escape attempt (also guards the S3 key)
        if target.is_file():
            return FileResponse(str(target), headers={"Cache-Control": "private"})
        get_kwargs = {"Bucket": self.bucket, "Key": self._durable_key(rid, target)}
        if range_header:
            get_kwargs["Range"] = range_header
        try:
            obj = self._s3.get_object(**get_kwargs)
        except Exception:
            return None
        headers = {"Cache-Control": "private", "Accept-Ranges": "bytes"}
        if obj.get("ETag"):
            headers["ETag"] = obj["ETag"]
        content_range = obj.get("ContentRange")
        if content_range:
            headers["Content-Range"] = content_range
        media_type = mimetypes.guess_type(relpath)[0] or "application/octet-stream"
        return StreamingResponse(
            obj["Body"].iter_chunks(),
            status_code=206 if content_range else 200,
            media_type=media_type,
            headers=headers,
        )


_store: ArtifactStore | None = None
_store_lock = Lock()


def get_artifact_store() -> ArtifactStore:
    """Process-wide store instance (thread-safe double-checked init)."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = _build_store()
    return _store


def _build_store() -> ArtifactStore:
    if settings.ARTIFACT_STORE == "s3":
        return S3ArtifactStore(STAGING_ROOT)
    return LocalArtifactStore(STAGING_ROOT)

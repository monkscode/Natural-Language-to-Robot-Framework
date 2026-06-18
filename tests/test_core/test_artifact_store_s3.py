"""S3ArtifactStore against a real MinIO container (no AWS account, no mocking).

Spins MinIO with the docker SDK already used for test execution. Marked
integration so it can be selected/skipped; skips cleanly when Docker is absent.
"""
import socket
import time
import uuid

import pytest

pytestmark = pytest.mark.integration


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def minio():
    docker = pytest.importorskip("docker")
    boto3 = pytest.importorskip("boto3")
    try:
        client = docker.from_env()
        client.ping()
    except Exception:
        pytest.skip("Docker not available")
    port = _free_port()
    container = client.containers.run(
        "minio/minio:latest",
        command="server /data",
        environment={"MINIO_ROOT_USER": "minio",
                     "MINIO_ROOT_PASSWORD": "minio123"},
        ports={"9000/tcp": port},
        detach=True, remove=True,
    )
    endpoint = f"http://127.0.0.1:{port}"
    admin = boto3.client(
        "s3", endpoint_url=endpoint, aws_access_key_id="minio",
        aws_secret_access_key="minio123", region_name="us-east-1")
    ready = False
    for _ in range(60):
        try:
            admin.list_buckets()
            ready = True
            break
        except Exception:
            time.sleep(1)
    if not ready:
        container.stop()
        pytest.fail("MinIO did not become ready")
    try:
        yield {"endpoint": endpoint, "key": "minio",
               "secret": "minio123", "admin": admin}
    finally:
        container.stop()


@pytest.fixture
def s3_store(minio, tmp_path, monkeypatch):
    from src.backend.core.config import settings
    from src.backend.core.artifact_store import S3ArtifactStore
    bucket = "t" + uuid.uuid4().hex[:16]
    minio["admin"].create_bucket(Bucket=bucket)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", minio["key"])
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", minio["secret"])
    monkeypatch.setattr(settings, "ARTIFACT_S3_BUCKET", bucket)
    monkeypatch.setattr(settings, "ARTIFACT_S3_PREFIX", "runs")
    monkeypatch.setattr(settings, "ARTIFACT_S3_REGION", "us-east-1")
    monkeypatch.setattr(settings, "ARTIFACT_S3_ENDPOINT_URL", minio["endpoint"])
    return S3ArtifactStore(tmp_path)


def _seed(store, rid):
    run = store.run_dir(rid, create=True)
    (run / "log.html").write_text("hello report", encoding="utf-8")
    return run


def test_persist_then_read_from_bucket(s3_store):
    rid = str(uuid.uuid4())
    run = _seed(s3_store, rid)
    s3_store.persist_run(rid)
    # remove staging so the read MUST come from the bucket (replica scenario)
    (run / "log.html").unlink()
    assert s3_store.read_text(rid, "log.html") == "hello report"


def test_persist_writes_completion_marker(s3_store):
    rid = str(uuid.uuid4())
    _seed(s3_store, rid)
    s3_store.persist_run(rid)
    head = s3_store._s3.head_object(
        Bucket=s3_store.bucket, Key=s3_store._key(rid, ".complete"))
    assert head["ResponseMetadata"]["HTTPStatusCode"] == 200


def test_serve_staging_first_returns_fileresponse(s3_store):
    from fastapi.responses import FileResponse
    rid = str(uuid.uuid4())
    _seed(s3_store, rid)
    assert isinstance(s3_store.serve_artifact(rid, "log.html"), FileResponse)


def test_serve_from_bucket_when_staging_absent(s3_store):
    from fastapi.responses import StreamingResponse
    rid = str(uuid.uuid4())
    run = _seed(s3_store, rid)
    s3_store.persist_run(rid)
    (run / "log.html").unlink()
    resp = s3_store.serve_artifact(rid, "log.html")
    assert isinstance(resp, StreamingResponse)
    assert resp.status_code == 200


def test_serve_range_returns_206(s3_store):
    rid = str(uuid.uuid4())
    run = _seed(s3_store, rid)
    s3_store.persist_run(rid)
    (run / "log.html").unlink()
    resp = s3_store.serve_artifact(rid, "log.html", range_header="bytes=0-3")
    assert resp.status_code == 206
    assert "Content-Range" in resp.headers


def test_escape_and_bad_id_return_none(s3_store):
    rid = str(uuid.uuid4())
    _seed(s3_store, rid)
    assert s3_store.serve_artifact(rid, "../x/log.html") is None
    assert s3_store.serve_artifact("not-a-uuid", "log.html") is None
    assert s3_store.read_text(rid, "missing.html") is None

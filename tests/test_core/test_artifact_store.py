"""LocalArtifactStore — staging, safe-join, read_text, serve_artifact."""
import uuid

import pytest
from fastapi.responses import FileResponse

from src.backend.core.artifact_store import LocalArtifactStore

RID = str(uuid.uuid4())


@pytest.fixture
def store(tmp_path):
    s = LocalArtifactStore(tmp_path)
    run = s.run_dir(RID, create=True)
    (run / "log.html").write_text("ok", encoding="utf-8")
    shots = run / "browser" / "screenshot"
    shots.mkdir(parents=True)
    (shots / "s.png").write_bytes(b"\x89PNG")
    # a sibling run whose file must never be reachable via traversal
    sibling = s.run_dir("11111111-1111-1111-1111-111111111111", create=True)
    (sibling / "log.html").write_text("SECRET", encoding="utf-8")
    return s


def test_run_dir_creates_when_asked(tmp_path):
    s = LocalArtifactStore(tmp_path)
    d = s.run_dir(RID, create=True)
    assert d.is_dir()
    assert d == tmp_path / RID


def test_read_text_returns_contents(store):
    assert store.read_text(RID, "log.html") == "ok"


def test_read_text_missing_returns_none(store):
    assert store.read_text(RID, "nope.html") is None


def test_serve_contained_file_returns_fileresponse(store):
    resp = store.serve_artifact(RID, "log.html")
    assert isinstance(resp, FileResponse)
    assert resp.headers["cache-control"] == "private"
    assert store.serve_artifact(RID, "browser/screenshot/s.png") is not None


def test_serve_dot_dot_escape_returns_none(store):
    assert store.serve_artifact(
        RID, "../11111111-1111-1111-1111-111111111111/log.html") is None


def test_serve_absolute_path_returns_none(store):
    assert store.serve_artifact(RID, "/etc/passwd") is None


def test_serve_non_uuid_run_id_returns_none(store):
    assert store.serve_artifact("not-a-uuid", "log.html") is None


def test_serve_directory_returns_none(store):
    assert store.serve_artifact(RID, "browser") is None


def test_persist_run_is_noop(store):
    assert store.persist_run(RID) is None


def test_s3_store_requires_bucket(tmp_path, monkeypatch):
    from src.backend.core.config import settings
    from src.backend.core.artifact_store import S3ArtifactStore
    monkeypatch.setattr(settings, "ARTIFACT_S3_BUCKET", "")
    with pytest.raises(ValueError):
        S3ArtifactStore(tmp_path)


def test_factory_builds_local_by_default(monkeypatch):
    import src.backend.core.artifact_store as mod
    from src.backend.core.config import settings
    monkeypatch.setattr(settings, "ARTIFACT_STORE", "local")
    monkeypatch.setattr(mod, "_store", None)
    assert isinstance(mod.get_artifact_store(), mod.LocalArtifactStore)
    monkeypatch.setattr(mod, "_store", None)  # reset singleton for other tests


def test_local_read_text_invalid_rid_returns_none(store):
    assert store.read_text("not-a-uuid", "log.html") is None


def test_local_read_text_non_utf8_returns_none(store):
    # read_text(encoding="utf-8") raises UnicodeDecodeError (a ValueError) on
    # binary content; it must be caught and degrade to None, not bubble out.
    (store.run_dir(RID) / "bin.dat").write_bytes(b"\xff\xfe\x00\x01")
    assert store.read_text(RID, "bin.dat") is None


# ===========================================================================
# S3ArtifactStore — unit tests with a mocked boto3 client.
#
# boto3 is an optional ('s3' extra) dependency NOT installed in CI, and the real
# round-trip (MinIO) is Docker-gated in test_artifact_store_s3.py. These tests
# inject a fake boto3 via sys.modules so the S3 backend's branch logic (key
# building, staging-vs-bucket fallback, range/ETag headers, the symlink-escape
# guard, error swallowing) is covered deterministically with or without boto3.
# ===========================================================================
import logging
import os
import sys
from unittest.mock import MagicMock, Mock, patch


def _can_symlink() -> bool:
    import tempfile
    d = tempfile.mkdtemp()
    try:
        os.symlink(os.path.join(d, "t"), os.path.join(d, "l"))
        return True
    except (OSError, NotImplementedError):
        return False


def _make_s3_store(tmp_path, monkeypatch, region="", endpoint=""):
    """Build an S3ArtifactStore whose boto3 client is a MagicMock.

    Returns (store, fake_boto3_module, mock_s3_client)."""
    from src.backend.core.config import settings
    monkeypatch.setattr(settings, "ARTIFACT_S3_BUCKET", "test-bucket")
    monkeypatch.setattr(settings, "ARTIFACT_S3_PREFIX", "runs")
    monkeypatch.setattr(settings, "ARTIFACT_S3_REGION", region)
    monkeypatch.setattr(settings, "ARTIFACT_S3_ENDPOINT_URL", endpoint)
    mock_client = MagicMock()
    fake_boto3 = MagicMock()
    fake_boto3.client.return_value = mock_client
    with patch.dict(sys.modules, {"boto3": fake_boto3}):
        from src.backend.core.artifact_store import S3ArtifactStore
        store = S3ArtifactStore(tmp_path)
    return store, fake_boto3, mock_client


@pytest.fixture
def s3(tmp_path, monkeypatch):
    return _make_s3_store(tmp_path, monkeypatch,
                          region="us-east-1", endpoint="http://minio:9000")


# --- construction ---

def test_s3_init_forwards_region_and_endpoint(s3):
    _store, fake_boto3, _client = s3
    fake_boto3.client.assert_called_once_with(
        "s3", region_name="us-east-1", endpoint_url="http://minio:9000")


def test_s3_init_omits_blank_kwargs(tmp_path, monkeypatch):
    _store, fake_boto3, _client = _make_s3_store(tmp_path, monkeypatch)
    fake_boto3.client.assert_called_once_with("s3")


def test_s3_missing_boto3_raises_runtimeerror(tmp_path, monkeypatch):
    from src.backend.core.config import settings
    monkeypatch.setattr(settings, "ARTIFACT_S3_BUCKET", "b")
    # sys.modules[name] = None makes `import name` raise ImportError.
    with patch.dict(sys.modules, {"boto3": None}):
        from src.backend.core.artifact_store import S3ArtifactStore
        with pytest.raises(RuntimeError, match="boto3"):
            S3ArtifactStore(tmp_path)


def test_s3_key_joins_prefix_run_relpath(s3):
    store, _b, _c = s3
    assert store._key("RID", "browser/s.png") == "runs/RID/browser/s.png"


# --- persist_run ---

def test_s3_persist_uploads_all_files_then_marker(s3):
    store, _b, client = s3
    rid = str(uuid.uuid4())
    run = store.run_dir(rid, create=True)
    (run / "log.html").write_text("hi", encoding="utf-8")
    sub = run / "browser" / "screenshot"
    sub.mkdir(parents=True)
    (sub / "s.png").write_bytes(b"\x89PNG")
    store.persist_run(rid)
    uploaded = {c.args[2] for c in client.upload_file.call_args_list}
    assert uploaded == {f"runs/{rid}/log.html",
                        f"runs/{rid}/browser/screenshot/s.png"}
    assert client.put_object.call_args.kwargs["Key"] == f"runs/{rid}/.complete"


def test_s3_persist_invalid_rid_is_noop(s3):
    store, _b, client = s3
    store.persist_run("not-a-uuid")
    client.upload_file.assert_not_called()


def test_s3_persist_missing_dir_warns_and_skips(s3, caplog):
    store, _b, client = s3
    rid = str(uuid.uuid4())  # never created on disk
    with caplog.at_level(logging.WARNING):
        store.persist_run(rid)
    client.upload_file.assert_not_called()
    assert any("no staging dir" in r.message for r in caplog.records)


def test_s3_persist_upload_error_does_not_raise(s3):
    store, _b, client = s3
    rid = str(uuid.uuid4())
    run = store.run_dir(rid, create=True)
    (run / "log.html").write_text("hi", encoding="utf-8")
    client.upload_file.side_effect = RuntimeError("network down")
    store.persist_run(rid)  # a failed upload must never fail the user's run


@pytest.mark.skipif(not _can_symlink(), reason="symlink creation not permitted")
def test_s3_persist_skips_symlink_escape(s3, tmp_path, caplog):
    store, _b, client = s3
    rid = str(uuid.uuid4())
    run = store.run_dir(rid, create=True)
    (run / "log.html").write_text("hi", encoding="utf-8")
    secret = tmp_path / "host-secret.txt"
    secret.write_text("TOPSECRET", encoding="utf-8")
    os.symlink(str(secret), str(run / "evil.txt"))  # escapes the run dir
    with caplog.at_level(logging.WARNING):
        store.persist_run(rid)
    keys = {c.args[2] for c in client.upload_file.call_args_list}
    assert f"runs/{rid}/log.html" in keys
    assert f"runs/{rid}/evil.txt" not in keys  # symlink escape was not uploaded
    assert any("unsafe path" in r.message for r in caplog.records)


# --- read_text ---

def test_s3_read_text_staging_first(s3):
    store, _b, client = s3
    rid = str(uuid.uuid4())
    run = store.run_dir(rid, create=True)
    (run / "test.robot").write_text("*** Settings ***", encoding="utf-8")
    assert store.read_text(rid, "test.robot") == "*** Settings ***"
    client.get_object.assert_not_called()  # served from local staging


def test_s3_read_text_falls_back_to_bucket(s3):
    store, _b, client = s3
    rid = str(uuid.uuid4())
    store.run_dir(rid, create=True)  # dir exists, file absent
    client.get_object.return_value = {
        "Body": Mock(read=Mock(return_value=b"from-bucket"))}
    assert store.read_text(rid, "test.robot") == "from-bucket"
    client.get_object.assert_called_once()


def test_s3_read_text_unreadable_staging_falls_back_to_bucket(s3):
    # A corrupt / non-UTF-8 local staging copy must not mask the durable bucket
    # copy: the staging read fails and read_text falls through to S3.
    store, _b, client = s3
    rid = str(uuid.uuid4())
    run = store.run_dir(rid, create=True)
    (run / "test.robot").write_bytes(b"\xff\xfe\x00")  # exists but not valid UTF-8
    client.get_object.return_value = {
        "Body": Mock(read=Mock(return_value=b"clean-from-bucket"))}
    assert store.read_text(rid, "test.robot") == "clean-from-bucket"
    client.get_object.assert_called_once()


def test_s3_read_text_escape_returns_none(s3):
    store, _b, client = s3
    rid = str(uuid.uuid4())
    store.run_dir(rid, create=True)
    assert store.read_text(rid, "../secret") is None
    client.get_object.assert_not_called()


def test_s3_read_text_bucket_miss_returns_none(s3):
    store, _b, client = s3
    rid = str(uuid.uuid4())
    store.run_dir(rid, create=True)
    client.get_object.side_effect = Exception("NoSuchKey")
    assert store.read_text(rid, "gone.txt") is None


def test_s3_read_text_invalid_rid_returns_none(s3):
    store, *_ = s3
    assert store.read_text("not-a-uuid", "x") is None


def test_s3_bucket_fallback_key_is_normalized(s3):
    """A relpath with in-bounds '..' segments must hit the bucket under the same
    normalized key persist_run uploads (e.g. 'a/b/../log.html' -> 'a/log.html'),
    not the literal '.../a/b/../log.html' which would never match."""
    store, _b, client = s3
    rid = str(uuid.uuid4())
    store.run_dir(rid, create=True)  # no file on disk -> bucket fallback
    client.get_object.return_value = {
        "Body": Mock(read=Mock(return_value=b"from-bucket"))}
    assert store.read_text(rid, "a/b/../log.html") == "from-bucket"
    assert client.get_object.call_args.kwargs["Key"] == f"runs/{rid}/a/log.html"


# --- serve_artifact ---

def test_s3_serve_staging_first_fileresponse(s3):
    store, _b, client = s3
    rid = str(uuid.uuid4())
    run = store.run_dir(rid, create=True)
    (run / "log.html").write_text("x", encoding="utf-8")
    resp = store.serve_artifact(rid, "log.html")
    assert isinstance(resp, FileResponse)
    assert resp.headers["cache-control"] == "private"
    client.get_object.assert_not_called()


def test_s3_serve_bucket_streaming_200(s3):
    from fastapi.responses import StreamingResponse
    store, _b, client = s3
    rid = str(uuid.uuid4())
    store.run_dir(rid, create=True)
    client.get_object.return_value = {
        "Body": Mock(iter_chunks=Mock(return_value=iter([b"data"]))),
        "ETag": '"abc123"',
    }
    resp = store.serve_artifact(rid, "log.html")
    assert isinstance(resp, StreamingResponse)
    assert resp.status_code == 200
    assert resp.headers["etag"] == '"abc123"'
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["cache-control"] == "private"


def test_s3_serve_range_returns_206(s3):
    store, _b, client = s3
    rid = str(uuid.uuid4())
    store.run_dir(rid, create=True)
    client.get_object.return_value = {
        "Body": Mock(iter_chunks=Mock(return_value=iter([b"da"]))),
        "ContentRange": "bytes 0-1/4",
    }
    resp = store.serve_artifact(rid, "log.html", range_header="bytes=0-1")
    assert resp.status_code == 206
    assert resp.headers["content-range"] == "bytes 0-1/4"
    assert client.get_object.call_args.kwargs["Range"] == "bytes=0-1"


def test_s3_serve_escape_returns_none(s3):
    store, _b, client = s3
    rid = str(uuid.uuid4())
    store.run_dir(rid, create=True)
    assert store.serve_artifact(rid, "../secret") is None
    client.get_object.assert_not_called()


def test_s3_serve_bucket_miss_returns_none(s3):
    store, _b, client = s3
    rid = str(uuid.uuid4())
    store.run_dir(rid, create=True)
    client.get_object.side_effect = Exception("NoSuchKey")
    assert store.serve_artifact(rid, "gone.html") is None


def test_s3_serve_invalid_rid_returns_none(s3):
    store, *_ = s3
    assert store.serve_artifact("not-a-uuid", "x") is None


# --- factory ---

def test_factory_builds_s3_when_configured(tmp_path, monkeypatch):
    import src.backend.core.artifact_store as mod
    from src.backend.core.config import settings
    monkeypatch.setattr(settings, "ARTIFACT_STORE", "s3")
    monkeypatch.setattr(settings, "ARTIFACT_S3_BUCKET", "b")
    monkeypatch.setattr(mod, "_store", None)
    fake_boto3 = MagicMock()
    fake_boto3.client.return_value = MagicMock()
    with patch.dict(sys.modules, {"boto3": fake_boto3}):
        s = mod.get_artifact_store()
    assert isinstance(s, mod.S3ArtifactStore)
    monkeypatch.setattr(mod, "_store", None)  # reset singleton for other tests


class TestSharedRunDirMode:
    """The staging tree is a workspace shared by two containers running as
    DIFFERENT users, so the directory mode is a correctness constraint.

    The app writes as appuser (uid 1000). The test-runner container runs as root
    but with cap_drop ALL, which removes CAP_DAC_OVERRIDE — so its root is
    subject to normal permission checks like anybody else. mkdir's default 0755
    leaves it unable to create output.xml in the run directory, and Robot
    Framework dies with 'PermissionError: [Errno 13]' and exit code 252.

    Reproduced 2026-08-07 against monkscode/nlrf:test-runner-local: identical
    directory, image, mount and command, the ONLY variable being --cap-drop ALL.

    NOT asserted via st_mode: os.chmod cannot set POSIX bits on Windows, where
    this suite runs. The contract is that the creation path asks for the shared
    mode; whether the kernel honours it is the OS's business.
    """

    def test_run_dir_creation_requests_world_writable_mode(self, tmp_path):
        from unittest.mock import patch
        from src.backend.core.artifact_store import (
            SHARED_DIR_MODE, LocalArtifactStore,
        )

        store = LocalArtifactStore(tmp_path)
        with patch("src.backend.core.artifact_store.os.chmod") as chmod:
            d = store.run_dir("run-shared", create=True)

        assert d.is_dir()
        chmod.assert_any_call(d, SHARED_DIR_MODE)

    def test_shared_mode_grants_write_to_other(self):
        """uid 0 without CAP_DAC_OVERRIDE writes as 'other' here — it matches
        neither the owner (1000) nor the group (1000)."""
        from src.backend.core.artifact_store import SHARED_DIR_MODE

        assert SHARED_DIR_MODE & 0o002, "other must have the write bit"

    def test_shared_mode_has_no_sticky_bit(self):
        """dryrun_service deletes the runner's root-owned output.xml as appuser.
        A sticky bit would deny exactly that, trading one bug for another."""
        from src.backend.core.artifact_store import SHARED_DIR_MODE

        assert not SHARED_DIR_MODE & 0o1000

    def test_run_dir_survives_a_chmod_the_filesystem_refuses(self, tmp_path):
        """Windows and exotic mounts reject POSIX bits. Creating the directory
        must still succeed — the mount there already presents host-created dirs
        as world-writable, so the chmod is belt-and-braces, not load-bearing."""
        from unittest.mock import patch
        from src.backend.core.artifact_store import LocalArtifactStore

        store = LocalArtifactStore(tmp_path)
        with patch("src.backend.core.artifact_store.os.chmod",
                   side_effect=PermissionError("nope")):
            d = store.run_dir("run-chmod-refused", create=True)

        assert d.is_dir()

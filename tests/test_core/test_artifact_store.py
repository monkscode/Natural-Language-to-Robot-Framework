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

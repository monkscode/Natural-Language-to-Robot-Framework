# tests/test_core/test_request_id_middleware.py
"""Phase 3: every response carries X-Request-ID; an inbound value is echoed."""

from fastapi.testclient import TestClient

from src.backend.main import app

client = TestClient(app)


def test_request_id_minted_when_absent():
    r = client.get("/health")
    rid = r.headers.get("X-Request-ID")
    assert rid and len(rid) >= 8


def test_request_id_echoed_when_present():
    r = client.get("/health", headers={"X-Request-ID": "fixed-123"})
    assert r.headers.get("X-Request-ID") == "fixed-123"


def test_request_id_rejects_invalid_chars_mints_fresh():
    """A client id outside the allowed charset (space/slash here; newlines for
    log forging likewise) is dropped in favour of a minted UUID, never echoed."""
    bad = "inval d/id"
    r = client.get("/health", headers={"X-Request-ID": bad})
    rid = r.headers.get("X-Request-ID")
    assert rid != bad
    assert rid and len(rid) == 32  # uuid4().hex


def test_request_id_rejects_overlong_header():
    """A >128-char id is rejected so a hostile header can't bloat every log line."""
    overlong = "a" * 129
    r = client.get("/health", headers={"X-Request-ID": overlong})
    assert r.headers.get("X-Request-ID") != overlong
    assert len(r.headers.get("X-Request-ID")) == 32  # minted uuid4().hex


def test_request_id_accepts_full_allowed_charset_at_max_length():
    """A 128-char id using every allowed char class is accepted verbatim."""
    ok = "A1." + "-_:" + "b" * 122
    assert len(ok) == 128
    r = client.get("/health", headers={"X-Request-ID": ok})
    assert r.headers.get("X-Request-ID") == ok

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

"""Tests for per-IP auth rate limiting (DB-free).

Covers the client-IP key function (proxy-header precedence), the limiter
mechanism (429 after threshold, per-IP isolation), and that the real
/auth/forgot-password endpoint (a no-DB stub) is actually limited.
"""
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


def _whoami_app():
    from src.backend.auth.rate_limit import client_ip
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(request: Request):
        return {"ip": client_ip(request)}

    return TestClient(app)


def test_client_ip_prefers_x_real_ip():
    c = _whoami_app()
    r = c.get("/whoami", headers={"X-Real-IP": "203.0.113.5",
                                  "X-Forwarded-For": "10.0.0.9, 70.1.1.1"})
    assert r.json()["ip"] == "203.0.113.5"


def test_client_ip_falls_back_to_forwarded_for_first_hop():
    c = _whoami_app()
    r = c.get("/whoami", headers={"X-Forwarded-For": "203.0.113.6, 10.0.0.9"})
    assert r.json()["ip"] == "203.0.113.6"


def test_client_ip_falls_back_to_peer():
    c = _whoami_app()
    r = c.get("/whoami")
    assert r.json()["ip"] == "testclient"


def test_rate_limit_blocks_after_threshold():
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from src.backend.auth.rate_limit import client_ip

    app = FastAPI()
    lim = Limiter(key_func=client_ip)
    app.state.limiter = lim
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    @app.get("/ping")
    @lim.limit("3/minute")
    async def ping(request: Request):
        return {"ok": True}

    c = TestClient(app)
    h = {"X-Real-IP": "203.0.113.7"}
    for _ in range(3):
        assert c.get("/ping", headers=h).status_code == 200
    assert c.get("/ping", headers=h).status_code == 429
    # A different client IP is unaffected by the first IP's burst.
    assert c.get("/ping", headers={"X-Real-IP": "203.0.113.8"}).status_code == 200


def test_forgot_password_endpoint_is_rate_limited(monkeypatch):
    # The forgot-password endpoint is a no-DB stub, so we can exercise the REAL
    # decorated route without Postgres. TestClient without `with` does not run
    # startup events, so no DB / posture check fires.
    from src.backend.auth import rate_limit
    from src.backend.core.config import settings
    from src.backend.main import app

    monkeypatch.setattr(settings, "AUTH_RATE_LIMIT", "3/minute")
    rate_limit.limiter.enabled = True  # the global test fixture disables it

    c = TestClient(app)
    h = {"X-Real-IP": "198.51.100.42"}  # unique IP, no cross-test collision
    body = {"email": "ratelimit@example.com"}
    for _ in range(3):
        assert c.post("/auth/forgot-password", json=body, headers=h).status_code == 200
    assert c.post("/auth/forgot-password", json=body, headers=h).status_code == 429

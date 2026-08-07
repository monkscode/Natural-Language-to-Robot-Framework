"""The audit floor (record_request) via a mini-app; write_audit_log captured."""

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from src.backend.core import audit_log
from src.backend.auth.jwt_utils import create_access_token


@pytest.fixture
def captured(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setattr(audit_log, "write_audit_log", lambda **kw: rows.append(kw))
    return rows


@pytest.fixture
def client():
    app = FastAPI()

    @app.middleware("http")
    async def floor(request, call_next):
        response = await call_next(request)
        await audit_log.record_request(request, response, "rid-test")
        return response

    @app.post("/ok")
    async def ok():
        return {"ok": True}

    @app.post("/role")
    async def role(request: Request):
        request.state.audit_detail = {"from": "user", "to": "admin"}
        return {"ok": True}

    @app.post("/denied")
    async def denied():
        raise HTTPException(status_code=403, detail="nope")

    @app.post("/api/workflow-metrics/record")
    async def record():
        return {"ok": True}

    @app.get("/safe")
    async def safe():
        return {"ok": True}

    return TestClient(app)


def test_post_without_token_is_unknown(client, captured):
    client.post("/ok")
    assert len(captured) == 1
    row = captured[0]
    assert row["actor_email"] == "unknown"
    assert row["actor_user_id"] is None
    assert row["method"] == "POST"
    assert row["path"] == "/ok"
    assert row["status_code"] == 200
    assert row["request_id"] == "rid-test"
    assert row["source"] == "backend"
    assert row["detail"] is None


def test_post_with_token_records_email(client, captured):
    tok = create_access_token(
        {"id": "u1", "email": "a@b.com", "role": "user", "display_name": ""}
    )
    client.post("/ok", headers={"Authorization": f"Bearer {tok}"})
    row = captured[0]
    assert row["actor_email"] == "a@b.com"
    assert row["actor_user_id"] == "u1"
    assert row["source"] == "backend"


def test_failure_status_is_recorded(client, captured):
    client.post("/denied")
    assert captured[0]["status_code"] == 403


def test_detail_hook_is_recorded(client, captured):
    client.post("/role")
    assert captured[0]["detail"] == '{"from": "user", "to": "admin"}'


def test_machine_endpoint_without_token_is_system(client, captured):
    client.post("/api/workflow-metrics/record")
    row = captured[0]
    assert row["actor_email"] == "system"
    assert row["source"] == "browser-service"


def test_get_writes_no_row(client, captured):
    client.get("/safe")
    assert captured == []


def test_writer_failure_does_not_break_response(client, monkeypatch):
    def boom(**kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(audit_log, "write_audit_log", boom)
    r = client.post("/ok")
    assert r.status_code == 200  # floor fails open

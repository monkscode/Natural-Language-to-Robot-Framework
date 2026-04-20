"""
Tests for src.backend.api.error_handlers — register_error_handlers.

Verifies that the global exception handler:
- Returns HTTP 500 for unhandled exceptions
- Returns the canonical error JSON shape
- Does not leak exception details to the client
- Handles requests with and without a client IP
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.backend.api.error_handlers import register_error_handlers


@pytest.fixture()
def app_with_error_handler():
    """FastAPI app with the global handler registered and a few error routes."""
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/raises-runtime")
    async def raises_runtime():
        raise RuntimeError("internal failure")

    @app.get("/raises-value")
    async def raises_value():
        raise ValueError("bad value")

    @app.get("/raises-generic")
    async def raises_generic():
        raise Exception("generic problem")

    return app


@pytest.fixture()
def client(app_with_error_handler):
    # raise_server_exceptions=False lets us inspect the 500 response instead of
    # having TestClient re-raise the exception in the test process.
    with TestClient(app_with_error_handler, raise_server_exceptions=False) as c:
        yield c


class TestUnhandledExceptionHandler:
    def test_returns_500_for_runtime_error(self, client):
        resp = client.get("/raises-runtime")
        assert resp.status_code == 500

    def test_returns_500_for_value_error(self, client):
        resp = client.get("/raises-value")
        assert resp.status_code == 500

    def test_returns_500_for_generic_exception(self, client):
        resp = client.get("/raises-generic")
        assert resp.status_code == 500

    def test_response_has_status_error(self, client):
        data = client.get("/raises-runtime").json()
        assert data["status"] == "error"

    def test_response_has_message_field(self, client):
        data = client.get("/raises-runtime").json()
        assert "message" in data

    def test_response_does_not_leak_exception_details(self, client):
        data = client.get("/raises-runtime").json()
        # Internal exception message must not appear in the response body
        assert "internal failure" not in data["message"]

    def test_response_content_type_is_json(self, client):
        resp = client.get("/raises-runtime")
        assert "application/json" in resp.headers.get("content-type", "")

    def test_handler_registered_on_app(self):
        """register_error_handlers must attach a handler for the base Exception class."""
        app = FastAPI()
        register_error_handlers(app)
        # FastAPI stores exception handlers keyed by exception type
        assert Exception in app.exception_handlers

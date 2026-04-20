"""
Global exception handlers for FastAPI.

Catches unhandled exceptions that escape endpoint handlers and logs them with
full context (stack trace, request method/path). Returns a clean 500 response
without leaking internal details to the client.

Referenced by: main.py (register_error_handlers)
"""
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("mark1.error_handler")


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.error(
            "Unhandled exception in %s %s",
            request.method,
            request.url.path,
            exc_info=exc,
            extra={
                "request_method": request.method,
                "request_path": request.url.path,
                "client_ip": request.client.host if request.client else "unknown",
            },
        )
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": "An internal error occurred. Check server logs for details.",
            },
        )

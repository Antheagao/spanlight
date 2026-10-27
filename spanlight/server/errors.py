"""The `/api` error envelope: `{"error": {"code", "message", "field"?}}`.

`code` is stable and meant for clients to branch on; `message` is for people.
Every error the server produces, including the framework's own (unknown route,
wrong method, malformed JSON), goes through here so no response is an HTML
page or a stack trace.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db

log = logging.getLogger(__name__)

# Codes for the framework's own HTTP errors, which carry no code of their own.
_STATUS_CODES = {404: "not_found", 405: "method_not_allowed"}


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        field: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.field = field
        self.headers = headers


def api_error(
    status: int,
    code: str,
    message: str,
    field: str | None = None,
    headers: dict[str, str] | None = None,
) -> ApiError:
    return ApiError(status, code, message, field, headers)


def envelope(code: str, message: str, field: str | None = None) -> dict:
    error = {"code": code, "message": message}
    if field is not None:
        error["field"] = field
    return {"error": error}


def error_response(
    status: int,
    code: str,
    message: str,
    field: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(envelope(code, message, field), status_code=status, headers=headers)


def _field(location: tuple) -> str | None:
    """`("body", "spans", 0, "input_tokens")` becomes `spans[0].input_tokens`."""
    path = ""
    for part in location[1:]:
        path += f"[{part}]" if isinstance(part, int) else f".{part}" if path else str(part)
    return path or None


def install(app: FastAPI, security_headers: dict[str, str]) -> None:
    """Register the handlers. `security_headers` are added to the 500 response,
    which Starlette builds outside the middleware that normally adds them."""

    @app.exception_handler(ApiError)
    def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status, exc.code, exc.message, exc.field, exc.headers)

    @app.exception_handler(RequestValidationError)
    def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Only the location and Pydantic's short message: never the input,
        # which may be a prompt.
        first = exc.errors()[0]
        field = None if first["type"] == "json_invalid" else _field(first["loc"])
        return error_response(400, "invalid_body", first["msg"], field)

    @app.exception_handler(StarletteHTTPException)
    def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "http_error")
        message = "no such route" if exc.status_code == 404 else str(exc.detail)
        return error_response(exc.status_code, code, message, headers=exc.headers)

    @app.exception_handler(db.DatabaseBusy)
    def database_busy(request: Request, exc: db.DatabaseBusy) -> JSONResponse:
        # Another process held the write lock past the busy timeout. Clients
        # (including OTLP exporters) treat 503 + Retry-After as retryable.
        log.warning("%s %s: database busy", request.method, request.url.path)
        return error_response(
            503, "database_busy", "database busy, retry", headers={"Retry-After": "1"}
        )

    @app.exception_handler(Exception)
    def unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.error("%s %s failed", request.method, request.url.path, exc_info=exc)
        response = error_response(500, "internal", "internal error")
        response.headers.update(security_headers)
        return response

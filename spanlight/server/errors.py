"""Error responses.

Under `/api` the body is the envelope `{"error": {"code", "message", "field"?}}`:
`code` is stable and meant for clients to branch on, `message` is for people.
Under `/v1` (OTLP/HTTP) it is a `google.rpc.Status`, in protobuf or JSON to
match the request, as the OTLP spec says; the HTTP status carries what a
client acts on, such as retrying a 503.

Every error the server produces, including the framework's own (unknown route,
wrong method, malformed JSON), goes through here so no response is an HTML
page or a stack trace.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db
from .otlp.encode import encode_status

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


def is_otlp_path(path: str) -> bool:
    return path == "/v1" or path.startswith("/v1/")


def _grpc_code(http_status: int) -> int:
    """The google.rpc.Code closest to an HTTP status."""
    known = {401: 16, 403: 7, 404: 5, 405: 12, 429: 8, 503: 14}
    if http_status in known:
        return known[http_status]
    return 13 if http_status >= 500 else 3  # INTERNAL, else INVALID_ARGUMENT


def error_response(
    status: int,
    code: str,
    message: str,
    field: str | None = None,
    headers: dict[str, str] | None = None,
    *,
    path: str = "/api",
    content_type: str | None = None,
) -> Response:
    """The error body for a request to `path`; `content_type` is the request's,
    which picks protobuf or JSON for an OTLP `Status`."""
    if not is_otlp_path(path):
        return JSONResponse(envelope(code, message, field), status_code=status, headers=headers)
    protobuf = (content_type or "").partition(";")[0].strip().lower() == "application/x-protobuf"
    body = encode_status(_grpc_code(status), message, "protobuf" if protobuf else "json")
    media_type = "application/x-protobuf" if protobuf else "application/json"
    return Response(body, status_code=status, headers=headers, media_type=media_type)


def _field(location: tuple) -> str | None:
    """`("body", "spans", 0, "input_tokens")` becomes `spans[0].input_tokens`."""
    path = ""
    for part in location[1:]:
        path += f"[{part}]" if isinstance(part, int) else f".{part}" if path else str(part)
    return path or None


def install(app: FastAPI, security_headers: dict[str, str]) -> None:
    """Register the handlers. `security_headers` are added to the 500 response,
    which Starlette builds outside the middleware that normally adds them."""

    def respond(request: Request, *args, **kwargs) -> Response:
        return error_response(
            *args,
            **kwargs,
            path=request.url.path,
            content_type=request.headers.get("content-type"),
        )

    @app.exception_handler(ApiError)
    def api_error_handler(request: Request, exc: ApiError) -> Response:
        return respond(request, exc.status, exc.code, exc.message, exc.field, exc.headers)

    @app.exception_handler(RequestValidationError)
    def validation_error(request: Request, exc: RequestValidationError) -> Response:
        # Only the location and Pydantic's short message: never the input,
        # which may be a prompt.
        first = exc.errors()[0]
        field = None if first["type"] == "json_invalid" else _field(first["loc"])
        return respond(request, 400, "invalid_body", first["msg"], field)

    @app.exception_handler(StarletteHTTPException)
    def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        code = _STATUS_CODES.get(exc.status_code, "http_error")
        message = "no such route" if exc.status_code == 404 else str(exc.detail)
        return respond(request, exc.status_code, code, message, headers=exc.headers)

    @app.exception_handler(db.DatabaseBusy)
    def database_busy(request: Request, exc: db.DatabaseBusy) -> Response:
        # Another process held the write lock past the busy timeout. Clients
        # (including OTLP exporters) treat 503 + Retry-After as retryable.
        log.warning("%s %s: database busy", request.method, request.url.path)
        return respond(
            request, 503, "database_busy", "database busy, retry", headers={"Retry-After": "1"}
        )

    @app.exception_handler(Exception)
    def unexpected(request: Request, exc: Exception) -> Response:
        log.error("%s %s failed", request.method, request.url.path, exc_info=exc)
        response = respond(request, 500, "internal", "internal error")
        response.headers.update(security_headers)
        return response

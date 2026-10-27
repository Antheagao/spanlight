"""Request guard: a pure ASGI middleware in front of everything, including the
dashboard's static mount.

Checks, in order: the Host header on every request (DNS rebinding), then on
writes the Origin (cross-site browser requests), the bearer token (when one
is configured), the content type (a form or `text/plain` POST needs no CORS
preflight, so refusing those types is what stops cross-site writes), and the
body size. It also adds the security headers to every response.
"""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlsplit

from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .errors import error_response
from .settings import Settings

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
ALLOWED_CONTENT_TYPES = frozenset({"application/json", "application/x-protobuf"})
# `style-src 'self'` is enough for the dashboard: React sets inline styles
# through the CSSOM, which CSP does not govern.
SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cross-origin-resource-policy": "same-origin",
}


def hostname(authority: str) -> str:
    """The lower-cased host of a `Host` header value or URL authority, without
    its port: `Localhost:4318` is `localhost`, `[::1]:4318` is `[::1]`."""
    authority = authority.strip().lower()
    if authority.startswith("["):
        return authority[: authority.find("]") + 1]
    return authority.partition(":")[0]


def _origin_authority(origin: str) -> tuple[str, int] | None:
    """(host, port) of an Origin, with the scheme's default port filled in,
    or None if it is not an http(s) origin."""
    try:
        parts = urlsplit(origin)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return host, port or (443 if parts.scheme == "https" else 80)


def _host_authority(host_header: str, scheme: str) -> tuple[str, int] | None:
    """(host, port) of a Host header, defaulting the port from the request's
    own scheme, or None if the port is malformed."""
    host_header = host_header.strip().lower()
    host = hostname(host_header)
    rest = host_header[len(host):]
    if not rest:
        return host, 443 if scheme == "https" else 80
    if rest[0] != ":" or not rest[1:].isdigit():
        return None
    return host, int(rest[1:])


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


class _PayloadTooLarge(Exception):
    pass


class Guard:
    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.settings = settings
        self.allowed_hosts = frozenset(hostname(h) for h in settings.allowed_hosts)
        self.token_digest = _digest(settings.ingest_token) if settings.ingest_token else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                kept = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.decode() not in SECURITY_HEADERS
                ]
                added = [(k.encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]
                message = {**message, "headers": kept + added}
            await send(message)

        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in reversed(scope["headers"])}
        refusal = self._refuse_headers(scope, headers)
        if refusal is not None:
            await refusal(scope, receive, send_with_headers)
            return
        if scope["method"] not in WRITE_METHODS:
            await self.app(scope, receive, send_with_headers)
            return

        try:
            body = await self._read_body(headers, receive)
        except _PayloadTooLarge:
            cap = self.settings.max_request_bytes
            response = _refusal(
                scope, headers, 413, "payload_too_large", f"request body exceeds {cap} bytes"
            )
            await response(scope, receive, send_with_headers)
            return
        if body is not None:
            await self.app(scope, _replay(body, receive), send_with_headers)

    def _refuse_headers(self, scope: Scope, headers: dict[str, str]) -> Response | None:
        method = scope["method"]
        # Liveness probes often use the pod or container IP as Host, and the
        # endpoint reveals nothing.
        probe = scope["path"] == "/healthz" and method == "GET"
        if not probe and not self._host_allowed(headers.get("host", "")):
            return _refusal(scope, headers, 400, "invalid_host", "the Host header is not allowed")
        if method not in WRITE_METHODS:
            return None
        origin = headers.get("origin")
        if origin is not None and not self._origin_allowed(
            origin, headers.get("host", ""), scope.get("scheme", "http")
        ):
            return _refusal(scope, headers, 403, "forbidden_origin", "cross-site request refused")
        if self.token_digest is not None and not self._authorized(headers.get("authorization")):
            return _refusal(
                scope,
                headers,
                401,
                "unauthorized",
                "a valid bearer token is required",
                {"WWW-Authenticate": "Bearer"},
            )
        # A body needs an allowed type; a request without one has nothing to
        # misinterpret and is left to routing (so DELETE can be a 405).
        declared = headers.get("content-length", "").strip()
        has_body = "transfer-encoding" in headers or (declared.isdigit() and int(declared) > 0)
        media_type = headers.get("content-type", "").partition(";")[0].strip().lower()
        if has_body and media_type not in ALLOWED_CONTENT_TYPES:
            return _refusal(
                scope,
                headers,
                415,
                "unsupported_media_type",
                "content type must be application/json or application/x-protobuf",
            )
        return None

    def _host_allowed(self, host_header: str) -> bool:
        # The authority must be well formed, so `localhost:1:x` and `[::1]x` are not.
        return (
            _host_authority(host_header, "http") is not None
            and hostname(host_header) in self.allowed_hosts
        )

    def _origin_allowed(self, origin: str, host_header: str, scheme: str) -> bool:
        """Same-origin (the Origin names this request's own Host and port), or
        one of the configured dev origins. Comparing with the request's own
        authority, not the bind port, keeps default ports and reverse proxies
        working."""
        if origin.lower() in self.settings.dev_origins:
            return True
        claimed = _origin_authority(origin)
        return claimed is not None and claimed == _host_authority(host_header, scheme)

    def _authorized(self, authorization: str | None) -> bool:
        if authorization is None:
            return False
        parts = authorization.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return False
        # Digests make the comparison constant-time whatever the lengths.
        return hmac.compare_digest(_digest(parts[1].strip()), self.token_digest)

    async def _read_body(self, headers: dict[str, str], receive: Receive) -> bytes | None:
        """The whole request body, or None if the client went away. Bytes are
        counted as they arrive, so a missing or wrong Content-Length cannot get
        past the cap, and nothing is parsed before it is known to fit."""
        cap = self.settings.max_request_bytes
        declared = headers.get("content-length", "")
        if declared.isdigit() and int(declared) > cap:
            raise _PayloadTooLarge
        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return None
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > cap:
                raise _PayloadTooLarge
            chunks.append(chunk)
            if not message.get("more_body", False):
                return b"".join(chunks)


def _replay(body: bytes, receive: Receive) -> Receive:
    """A `receive` that delivers the already-read body, then defers to the
    server's (which reports the client disconnecting)."""
    delivered = False

    async def replay() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay


def _refusal(
    scope: Scope,
    headers: dict[str, str],
    status: int,
    code: str,
    message: str,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    """An error in the format the route uses: the `/api` envelope, or a
    `google.rpc.Status` for OTLP's `/v1`."""
    return error_response(
        status,
        code,
        message,
        headers=extra_headers,
        path=scope["path"],
        content_type=headers.get("content-type"),
    )

"""Encode the OTLP/HTTP responses: `ExportTraceServiceResponse` for a request
that was accepted (possibly in part) and `google.rpc.Status` for one that was
not, each in the encoding of the request.

The server decides the content type; this module only builds the body.
"""

from __future__ import annotations

import json
from typing import Literal

Format = Literal["protobuf", "json"]

_VARINT = 0
_LEN = 2


def _varint(value: int) -> bytes:
    value &= 0xFFFFFFFFFFFFFFFF  # a negative int is encoded as 64 bits
    out = bytearray()
    while value > 0x7F:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _field(number: int, wire_type: int, payload: bytes) -> bytes:
    return _varint(number << 3 | wire_type) + payload


def _string_field(number: int, text: str) -> bytes:
    data = text.encode()
    return _field(number, _LEN, _varint(len(data)) + data)


def encode_export_response(rejected_spans: int, error_message: str, fmt: Format) -> bytes:
    """An `ExportTraceServiceResponse`. When every span was stored, the
    response is empty (`{}` as JSON); otherwise it carries `partial_success`
    with the rejected count and a message."""
    if rejected_spans < 0:
        raise ValueError("rejected_spans must not be negative")
    if fmt == "json":
        partial: dict = {}
        if rejected_spans:
            partial["rejectedSpans"] = str(rejected_spans)  # int64 is a string in JSON
        if error_message:
            partial["errorMessage"] = error_message
        return json.dumps({"partialSuccess": partial} if partial else {}).encode()
    partial_bytes = b""
    if rejected_spans:
        partial_bytes += _field(1, _VARINT, _varint(rejected_spans))
    if error_message:
        partial_bytes += _string_field(2, error_message)
    if not partial_bytes:
        return b""
    return _field(1, _LEN, _varint(len(partial_bytes)) + partial_bytes)


def encode_status(code: int, message: str, fmt: Format) -> bytes:
    """A `google.rpc.Status` error body, without details. `code` is a
    google.rpc.Code (for example 3 for INVALID_ARGUMENT)."""
    if fmt == "json":
        return json.dumps({"code": code, "message": message}).encode()
    body = b""
    if code:
        body += _field(1, _VARINT, _varint(code))
    if message:
        body += _string_field(2, message)
    return body

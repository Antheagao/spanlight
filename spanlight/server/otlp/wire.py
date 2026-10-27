"""A strict protobuf wire-format reader.

Only what OTLP needs: varint, fixed64, length-delimited and fixed32 fields.
Anything malformed raises `OtlpDecodeError`, never another exception, and
nothing is sliced or allocated before it is known to fit in the buffer.
"""

from __future__ import annotations

from collections.abc import Iterator

VARINT = 0
FIXED64 = 1
LEN = 2
SGROUP = 3
EGROUP = 4
FIXED32 = 5

_MAX_TAG_BYTES = 5
_MAX_VARINT_BYTES = 10


class OtlpDecodeError(ValueError):
    """The request is not a valid OTLP export in the encoding it claims."""


class UnsupportedWireType(OtlpDecodeError):
    """A deprecated group field. A conforming parser may skip these; we reject
    them, as OTLP never uses them."""


def read_varint(
    buf: memoryview, pos: int, end: int, max_bytes: int = _MAX_VARINT_BYTES
) -> tuple[int, int]:
    """The value (taken modulo 2**64) and the position after it."""
    result = 0
    shift = 0
    for _ in range(max_bytes):
        if pos >= end:
            raise OtlpDecodeError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result & 0xFFFFFFFFFFFFFFFF, pos
        shift += 7
    raise OtlpDecodeError("varint is too long")


def fields(buf: memoryview) -> Iterator[tuple[int, int, int | memoryview]]:
    """The (field number, wire type, value) of each field of one message.

    The value is an int for varint, fixed32 and fixed64 fields (the raw
    unsigned bits), and a zero-copy view for length-delimited fields.
    """
    pos = 0
    end = len(buf)
    while pos < end:
        tag, pos = read_varint(buf, pos, end, _MAX_TAG_BYTES)
        if tag > 0xFFFFFFFF:
            raise OtlpDecodeError("field tag is too large")
        number, wire_type = tag >> 3, tag & 7
        if number == 0:
            raise OtlpDecodeError("field number 0")
        value: int | memoryview
        if wire_type == VARINT:
            value, pos = read_varint(buf, pos, end)
        elif wire_type == FIXED64:
            if end - pos < 8:
                raise OtlpDecodeError("truncated fixed64")
            value = int.from_bytes(buf[pos : pos + 8], "little")
            pos += 8
        elif wire_type == LEN:
            length, pos = read_varint(buf, pos, end)
            if length > end - pos:
                raise OtlpDecodeError("length-delimited field runs past the end of the message")
            value = buf[pos : pos + length]
            pos += length
        elif wire_type == FIXED32:
            if end - pos < 4:
                raise OtlpDecodeError("truncated fixed32")
            value = int.from_bytes(buf[pos : pos + 4], "little")
            pos += 4
        elif wire_type in (SGROUP, EGROUP):
            raise UnsupportedWireType("group wire types are not supported")
        else:
            raise OtlpDecodeError(f"invalid wire type {wire_type}")
        yield number, wire_type, value


def to_int64(bits: int) -> int:
    return bits - (1 << 64) if bits >> 63 else bits


def to_int32(bits: int) -> int:
    """A varint-encoded int32 or enum: the low 32 bits, signed."""
    bits &= 0xFFFFFFFF
    return bits - (1 << 32) if bits >> 31 else bits


def to_uint32(bits: int) -> int:
    return bits & 0xFFFFFFFF

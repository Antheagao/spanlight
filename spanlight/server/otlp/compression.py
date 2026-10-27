"""Request body decompression for OTLP/HTTP (`Content-Encoding: gzip` or
`deflate`), with the output bounded.

`deflate` is the zlib format of RFC 1950, as the OpenTelemetry exporters send
it. A raw deflate stream, and gzip data made of several concatenated members,
are not accepted.

A small compressed body can inflate to gigabytes, so the output is produced in
bounded steps and the whole request fails as soon as it would pass the cap.
"""

from __future__ import annotations

import zlib

_STEP_BYTES = 1 << 20


class UnsupportedContentEncoding(ValueError):
    """Any Content-Encoding other than identity, gzip or deflate."""


class MalformedCompression(ValueError):
    """The body is not a complete, valid stream of the declared encoding."""


class DecompressedTooLarge(ValueError):
    """The decompressed body is over the request size cap."""


def inflate(data: bytes, encoding: str | None, max_bytes: int) -> bytes:
    """`data` decoded per the `Content-Encoding` header value, at most
    `max_bytes` long. Raises one of the exceptions above."""
    name = (encoding or "identity").strip().lower()
    if name in ("", "identity"):
        return data
    if name in ("gzip", "x-gzip"):
        wbits = 16 + zlib.MAX_WBITS
    elif name == "deflate":
        wbits = zlib.MAX_WBITS
    else:
        raise UnsupportedContentEncoding(f"unsupported Content-Encoding {name[:40]!r}")
    decompressor = zlib.decompressobj(wbits)
    out: list[bytes] = []
    total = 0
    pending = data
    try:
        while not decompressor.eof:
            room = max_bytes - total
            chunk = decompressor.decompress(pending, min(room + 1, _STEP_BYTES))
            if len(chunk) > room:
                raise DecompressedTooLarge(f"decompressed body exceeds {max_bytes} bytes")
            out.append(chunk)
            total += len(chunk)
            pending = decompressor.unconsumed_tail
            if not chunk and not pending:
                break  # all input consumed and no more output: the stream is cut short
    except zlib.error as error:
        raise MalformedCompression(f"invalid {name} stream: {error}") from None
    if not decompressor.eof:
        raise MalformedCompression(f"truncated {name} stream")
    if decompressor.unused_data:
        raise MalformedCompression(f"data after the end of the {name} stream")
    return b"".join(out)

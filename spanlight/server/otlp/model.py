"""The decoded form of an OTLP trace export, independent of its encoding.

Both decoders produce these types, so everything after them (mapping to
spanlight's span model, storage) is encoding-agnostic. Field names follow the
OTLP proto. Ids stay `bytes`; the JSON decoder converts from hex.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# An OTLP AnyValue. `None` is an AnyValue with no value set. A KeyValueList
# becomes a dict (a repeated key keeps its last value), an ArrayValue a list.
AnyValue = None | str | bool | int | float | bytes | list["AnyValue"] | dict[str, "AnyValue"]
Attributes = dict[str, AnyValue]

TRACE_ID_BYTES = 16
SPAN_ID_BYTES = 8
STRING_TRUNCATED_MARKER = "[truncated]"


@dataclass(frozen=True)
class Limits:
    """Bounds applied while decoding, so one request cannot build an unbounded
    object graph. They match the OpenTelemetry SDK defaults. An attribute,
    event or link over its limit is dropped and counted in the matching
    `dropped_*_count`."""

    max_attributes: int = 128
    max_events: int = 128
    max_links: int = 128
    # A longer string is cut to this many UTF-8 bytes and marked.
    max_string_bytes: int = 32 * 1024
    # AnyValue nesting; a request over it is rejected.
    max_depth: int = 16


DEFAULT_LIMITS = Limits()


@dataclass(frozen=True)
class Status:
    message: str = ""
    code: int = 0


@dataclass(frozen=True)
class Event:
    time_unix_nano: int = 0
    name: str = ""
    attributes: Attributes = field(default_factory=dict)
    dropped_attributes_count: int = 0


@dataclass(frozen=True)
class Link:
    trace_id: bytes = b""
    span_id: bytes = b""
    trace_state: str = ""
    attributes: Attributes = field(default_factory=dict)
    dropped_attributes_count: int = 0
    flags: int = 0


@dataclass(frozen=True)
class Span:
    trace_id: bytes = b""
    span_id: bytes = b""
    trace_state: str = ""
    parent_span_id: bytes = b""
    name: str = ""
    kind: int = 0
    start_time_unix_nano: int = 0
    end_time_unix_nano: int = 0
    attributes: Attributes = field(default_factory=dict)
    dropped_attributes_count: int = 0
    events: tuple[Event, ...] = ()
    dropped_events_count: int = 0
    links: tuple[Link, ...] = ()
    dropped_links_count: int = 0
    status: Status = field(default_factory=Status)
    flags: int = 0
    # Why this span cannot be stored, or None. A reject fails the span, not
    # the request: OTLP answers with a partial success.
    reject: str | None = None


@dataclass(frozen=True)
class Resource:
    attributes: Attributes = field(default_factory=dict)
    dropped_attributes_count: int = 0


@dataclass(frozen=True)
class Scope:
    name: str = ""
    version: str = ""
    attributes: Attributes = field(default_factory=dict)
    dropped_attributes_count: int = 0


@dataclass(frozen=True)
class ScopeSpans:
    scope: Scope = field(default_factory=Scope)
    spans: tuple[Span, ...] = ()
    schema_url: str = ""


@dataclass(frozen=True)
class ResourceSpans:
    resource: Resource = field(default_factory=Resource)
    scope_spans: tuple[ScopeSpans, ...] = ()
    schema_url: str = ""


@dataclass(frozen=True)
class ExportRequest:
    resource_spans: tuple[ResourceSpans, ...] = ()


def span_reject_reason(
    trace_id: bytes, span_id: bytes, parent_span_id: bytes, start_time_unix_nano: int
) -> str | None:
    """The OTLP rules for a span that cannot be accepted: ids of the wrong
    length or all zeros, a parent id that is neither empty nor 8 bytes, or no
    start time."""
    if len(trace_id) != TRACE_ID_BYTES:
        return f"trace_id must be {TRACE_ID_BYTES} bytes"
    if not any(trace_id):
        return "trace_id must not be all zeros"
    if len(span_id) != SPAN_ID_BYTES:
        return f"span_id must be {SPAN_ID_BYTES} bytes"
    if not any(span_id):
        return "span_id must not be all zeros"
    if parent_span_id and len(parent_span_id) != SPAN_ID_BYTES:
        return f"parent_span_id must be empty or {SPAN_ID_BYTES} bytes"
    if start_time_unix_nano == 0:
        return "start_time_unix_nano must not be 0"
    return None


def clip_string(value: str, max_bytes: int) -> str:
    """Cut `value` to at most `max_bytes` of UTF-8, on a character boundary,
    and mark the cut."""
    # A character is at most 4 bytes, so a short string cannot be over.
    if len(value) <= max_bytes // 4:
        return value
    encoded = value.encode()
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode(errors="ignore") + STRING_TRUNCATED_MARKER

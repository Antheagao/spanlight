"""Decode an OTLP `ExportTraceServiceRequest` from protobuf or OTLP/JSON.

The protobuf decoder follows the proto3 parsing rules the reference
implementation applies, so the same bytes mean the same thing here:

- an unknown field, or a known field sent with the wrong wire type, is skipped;
- a singular scalar sent twice keeps the last value, a singular message sent
  twice is merged, and a repeated field is concatenated;
- in the AnyValue oneof the last member wins;
- strings must be valid UTF-8, and any framing error rejects the request.

Every failure is an `OtlpDecodeError`. Field numbers come from the OTLP proto
(opentelemetry-proto 1.45).
"""

from __future__ import annotations

import base64
import json
import re
import struct
from collections.abc import Callable
from typing import Any, TypeVar

from .model import (
    DEFAULT_LIMITS,
    AnyValue,
    Attributes,
    Event,
    ExportRequest,
    Limits,
    Link,
    Resource,
    ResourceSpans,
    Scope,
    ScopeSpans,
    Span,
    Status,
    clip_string,
    span_reject_reason,
)
from .wire import (
    FIXED32,
    FIXED64,
    LEN,
    VARINT,
    OtlpDecodeError,
    fields,
    to_int32,
    to_int64,
    to_uint32,
)

# AnyValue oneof members, by field number.
_STRING, _BOOL, _INT, _DOUBLE, _ARRAY, _KVLIST, _BYTES, _STRINDEX = range(1, 9)


# -- shared helpers ----------------------------------------------------------


class _Attributes:
    """Collects KeyValues up to the limit and counts the rest as dropped."""

    def __init__(self, limits: Limits):
        self.items: Attributes = {}
        self.excess = 0
        self._max = limits.max_attributes

    def add(self, key: str, value: AnyValue) -> None:
        if key in self.items or len(self.items) < self._max:
            self.items[key] = value
        else:
            self.excess += 1


def _string(view: memoryview, limits: Limits) -> str:
    try:
        text = str(view, "utf-8")
    except UnicodeDecodeError:
        raise OtlpDecodeError("string is not valid UTF-8") from None
    return clip_string(text, limits.max_string_bytes)


def _clip_bytes(view: memoryview, limits: Limits) -> bytes:
    return bytes(view[: limits.max_string_bytes])


T = TypeVar("T")


def _merged(chunks: list[memoryview], parse: Callable[[memoryview], T]) -> T:
    """Parse a singular message field that may appear several times.

    Proto merges the occurrences, which is the same as parsing their
    concatenation; but each occurrence must also be well-formed on its own, so
    a field split mid-way across two occurrences is rejected like the
    reference does.
    """
    if len(chunks) == 1:
        return parse(chunks[0])
    for chunk in chunks:
        parse(chunk)
    return parse(memoryview(b"".join(chunks)))


# -- protobuf ----------------------------------------------------------------


def decode_protobuf(data: bytes | memoryview, limits: Limits = DEFAULT_LIMITS) -> ExportRequest:
    resource_spans = []
    for number, wire_type, value in fields(memoryview(data)):
        if number == 1 and wire_type == LEN:
            resource_spans.append(_resource_spans(value, limits))
    return ExportRequest(tuple(resource_spans))


def _resource_spans(buf: memoryview, limits: Limits) -> ResourceSpans:
    resource_chunks: list[memoryview] = []
    scope_spans = []
    schema_url = ""
    for number, wire_type, value in fields(buf):
        if number == 1 and wire_type == LEN:
            resource_chunks.append(value)
        elif number == 2 and wire_type == LEN:
            scope_spans.append(_scope_spans(value, limits))
        elif number == 3 and wire_type == LEN:
            schema_url = _string(value, limits)
    resource = Resource()
    if resource_chunks:
        resource = _merged(resource_chunks, lambda b: _resource(b, limits))
    return ResourceSpans(resource, tuple(scope_spans), schema_url)


def _resource(buf: memoryview, limits: Limits) -> Resource:
    attributes = _Attributes(limits)
    dropped = 0
    for number, wire_type, value in fields(buf):
        if number == 1 and wire_type == LEN:
            attributes.add(*_key_value(value, 1, limits))
        elif number == 2 and wire_type == VARINT:
            dropped = to_uint32(value)
        elif number == 3 and wire_type == LEN:
            _check_entity_ref(value)
    return Resource(attributes.items, dropped + attributes.excess)


def _check_entity_ref(buf: memoryview) -> None:
    """Resource.entity_refs is not used, but its strings are validated like
    the reference does, so a malformed one rejects the request alike."""
    for number, wire_type, value in fields(buf):
        if 1 <= number <= 4 and wire_type == LEN:
            try:
                str(value, "utf-8")
            except UnicodeDecodeError:
                raise OtlpDecodeError("string is not valid UTF-8") from None


def _scope_spans(buf: memoryview, limits: Limits) -> ScopeSpans:
    scope_chunks: list[memoryview] = []
    spans = []
    schema_url = ""
    for number, wire_type, value in fields(buf):
        if number == 1 and wire_type == LEN:
            scope_chunks.append(value)
        elif number == 2 and wire_type == LEN:
            spans.append(_span(value, limits))
        elif number == 3 and wire_type == LEN:
            schema_url = _string(value, limits)
    scope = Scope()
    if scope_chunks:
        scope = _merged(scope_chunks, lambda b: _scope(b, limits))
    return ScopeSpans(scope, tuple(spans), schema_url)


def _scope(buf: memoryview, limits: Limits) -> Scope:
    name = version = ""
    attributes = _Attributes(limits)
    dropped = 0
    for number, wire_type, value in fields(buf):
        if number == 1 and wire_type == LEN:
            name = _string(value, limits)
        elif number == 2 and wire_type == LEN:
            version = _string(value, limits)
        elif number == 3 and wire_type == LEN:
            attributes.add(*_key_value(value, 1, limits))
        elif number == 4 and wire_type == VARINT:
            dropped = to_uint32(value)
    return Scope(name, version, attributes.items, dropped + attributes.excess)


def _span(buf: memoryview, limits: Limits) -> Span:
    trace_id = span_id = parent_span_id = b""
    trace_state = name = ""
    kind = start = end = flags = 0
    attributes = _Attributes(limits)
    dropped_attributes = dropped_events = dropped_links = 0
    events: list[Event] = []
    links: list[Link] = []
    event_excess = link_excess = 0
    status_chunks: list[memoryview] = []
    for number, wire_type, value in fields(buf):
        if wire_type == LEN:
            if number == 1:
                trace_id = bytes(value)
            elif number == 2:
                span_id = bytes(value)
            elif number == 3:
                trace_state = _string(value, limits)
            elif number == 4:
                parent_span_id = bytes(value)
            elif number == 5:
                name = _string(value, limits)
            elif number == 9:
                attributes.add(*_key_value(value, 1, limits))
            elif number == 11:
                event = _event(value, limits)
                if len(events) < limits.max_events:
                    events.append(event)
                else:
                    event_excess += 1
            elif number == 13:
                link = _link(value, limits)
                if len(links) < limits.max_links:
                    links.append(link)
                else:
                    link_excess += 1
            elif number == 15:
                status_chunks.append(value)
        elif wire_type == VARINT:
            if number == 6:
                kind = to_int32(value)
            elif number == 10:
                dropped_attributes = to_uint32(value)
            elif number == 12:
                dropped_events = to_uint32(value)
            elif number == 14:
                dropped_links = to_uint32(value)
        elif wire_type == FIXED64:
            if number == 7:
                start = value
            elif number == 8:
                end = value
        elif wire_type == FIXED32 and number == 16:
            flags = value
    status = Status()
    if status_chunks:
        status = _merged(status_chunks, _status(limits))
    return Span(
        trace_id=trace_id,
        span_id=span_id,
        trace_state=trace_state,
        parent_span_id=parent_span_id,
        name=name,
        kind=kind,
        start_time_unix_nano=start,
        end_time_unix_nano=end,
        attributes=attributes.items,
        dropped_attributes_count=dropped_attributes + attributes.excess,
        events=tuple(events),
        dropped_events_count=dropped_events + event_excess,
        links=tuple(links),
        dropped_links_count=dropped_links + link_excess,
        status=status,
        flags=flags,
        reject=span_reject_reason(trace_id, span_id, parent_span_id, start),
    )


def _status(limits: Limits) -> Callable[[memoryview], Status]:
    def parse(buf: memoryview) -> Status:
        message = ""
        code = 0
        for number, wire_type, value in fields(buf):
            if number == 2 and wire_type == LEN:
                message = _string(value, limits)
            elif number == 3 and wire_type == VARINT:
                code = to_int32(value)
        return Status(message, code)

    return parse


def _event(buf: memoryview, limits: Limits) -> Event:
    time = dropped = 0
    name = ""
    attributes = _Attributes(limits)
    for number, wire_type, value in fields(buf):
        if number == 1 and wire_type == FIXED64:
            time = value
        elif number == 2 and wire_type == LEN:
            name = _string(value, limits)
        elif number == 3 and wire_type == LEN:
            attributes.add(*_key_value(value, 1, limits))
        elif number == 4 and wire_type == VARINT:
            dropped = to_uint32(value)
    return Event(time, name, attributes.items, dropped + attributes.excess)


def _link(buf: memoryview, limits: Limits) -> Link:
    trace_id = span_id = b""
    trace_state = ""
    dropped = flags = 0
    attributes = _Attributes(limits)
    for number, wire_type, value in fields(buf):
        if wire_type == LEN:
            if number == 1:
                trace_id = bytes(value)
            elif number == 2:
                span_id = bytes(value)
            elif number == 3:
                trace_state = _string(value, limits)
            elif number == 4:
                attributes.add(*_key_value(value, 1, limits))
        elif number == 5 and wire_type == VARINT:
            dropped = to_uint32(value)
        elif number == 6 and wire_type == FIXED32:
            flags = value
    dropped += attributes.excess
    return Link(trace_id, span_id, trace_state, attributes.items, dropped, flags)


def _key_value(buf: memoryview, depth: int, limits: Limits) -> tuple[str, AnyValue]:
    """A KeyValue whose value sits at AnyValue nesting `depth`."""
    key = ""
    value_chunks: list[memoryview] = []
    for number, wire_type, value in fields(buf):
        if number == 1 and wire_type == LEN:
            key = _string(value, limits)
        elif number == 2 and wire_type == LEN:
            value_chunks.append(value)
    if not value_chunks:
        return key, None
    return key, _merged(value_chunks, lambda b: _any_value(b, depth, limits))


def _any_value(buf: memoryview, depth: int, limits: Limits) -> AnyValue:
    if depth > limits.max_depth:
        raise OtlpDecodeError(f"attribute value is nested deeper than {limits.max_depth}")
    case = 0
    payload: Any = None
    # Pending ArrayValue / KeyValueList occurrences of the current case.
    chunks: list[memoryview] = []

    def settle() -> None:
        # Occurrences replaced by another member are discarded, but a
        # malformed one still rejects the request.
        parse = _array_values if case == _ARRAY else _kvlist_values
        for chunk in chunks:
            parse(chunk, depth, limits)
        chunks.clear()

    for number, wire_type, value in fields(buf):
        if wire_type == LEN and number in (_ARRAY, _KVLIST):
            if case == number:
                chunks.append(value)
            else:
                settle()
                case, chunks = number, [value]
        elif wire_type == LEN and number in (_STRING, _BYTES):
            settle()
            case = number
            payload = _string(value, limits) if number == _STRING else _clip_bytes(value, limits)
        elif wire_type == VARINT and number in (_BOOL, _INT, _STRINDEX):
            settle()
            case = number
            payload = value
        elif wire_type == FIXED64 and number == _DOUBLE:
            settle()
            case, payload = number, value
    if case == _ARRAY:
        return _merged(chunks, lambda b: _array_values(b, depth, limits))
    if case == _KVLIST:
        return _merged(chunks, lambda b: _kvlist_values(b, depth, limits))
    if case in (_STRING, _BYTES):
        return payload
    if case == _BOOL:
        return payload != 0
    if case == _INT:
        return to_int64(payload)
    if case == _DOUBLE:
        return struct.unpack("<d", payload.to_bytes(8, "little"))[0]
    return None


def _array_values(buf: memoryview, depth: int, limits: Limits) -> list[AnyValue]:
    values: list[AnyValue] = []
    for number, wire_type, value in fields(buf):
        if number == 1 and wire_type == LEN:
            values.append(_any_value(value, depth + 1, limits))
    return values


def _kvlist_values(buf: memoryview, depth: int, limits: Limits) -> dict[str, AnyValue]:
    values: dict[str, AnyValue] = {}
    for number, wire_type, value in fields(buf):
        if number == 1 and wire_type == LEN:
            key, item = _key_value(value, depth + 1, limits)
            values[key] = item
    return values


# -- OTLP/JSON ---------------------------------------------------------------

_HEX = re.compile(r"[0-9a-fA-F]*")
_INTEGER = re.compile(r"-?[0-9]+")
_DOUBLE_TEXT = re.compile(r"-?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_SPAN_KINDS = {
    "SPAN_KIND_UNSPECIFIED": 0,
    "SPAN_KIND_INTERNAL": 1,
    "SPAN_KIND_SERVER": 2,
    "SPAN_KIND_CLIENT": 3,
    "SPAN_KIND_PRODUCER": 4,
    "SPAN_KIND_CONSUMER": 5,
}
_STATUS_CODES = {"STATUS_CODE_UNSET": 0, "STATUS_CODE_OK": 1, "STATUS_CODE_ERROR": 2}


def decode_json(data: bytes | str, limits: Limits = DEFAULT_LIMITS) -> ExportRequest:
    """Decode OTLP/JSON. Keys may be lowerCamelCase or snake_case, ids are hex
    in either case, 64-bit integers may be strings or numbers, enums may be
    numbers or names, and unknown keys are ignored."""
    try:
        text = data if isinstance(data, str) else bytes(data).decode("utf-8")
        document = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as error:
        raise OtlpDecodeError(f"body is not valid JSON: {type(error).__name__}") from None
    return _JsonDecoder(limits).request(document)


def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} is not valid JSON")


def _snake(camel: str) -> str:
    return re.sub(r"([A-Z])", lambda m: "_" + m.group(1).lower(), camel)


class _JsonDecoder:
    def __init__(self, limits: Limits):
        self.limits = limits

    # -- scalars --

    def _get(self, obj: dict, camel: str) -> Any:
        """The value under the camelCase or the snake_case spelling, with
        JSON null meaning absent."""
        value = obj.get(camel)
        return obj.get(_snake(camel)) if value is None else value

    def _object(self, value: Any, what: str) -> dict:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise OtlpDecodeError(f"{what} must be an object")
        return value

    def _list(self, value: Any, what: str) -> list:
        if value is None:
            return []
        if not isinstance(value, list):
            raise OtlpDecodeError(f"{what} must be an array")
        return value

    def _str(self, value: Any, what: str) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise OtlpDecodeError(f"{what} must be a string")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise OtlpDecodeError(f"{what} is not valid Unicode") from None
        return clip_string(value, self.limits.max_string_bytes)

    def _hex(self, value: Any, what: str) -> bytes:
        if value is None:
            return b""
        if not isinstance(value, str) or len(value) % 2 or not _HEX.fullmatch(value):
            raise OtlpDecodeError(f"{what} must be a hex string")
        return bytes.fromhex(value)

    def _int(self, value: Any, what: str, low: int, high: int) -> int:
        if value is None:
            return 0
        if isinstance(value, bool):
            raise OtlpDecodeError(f"{what} must be an integer")
        if isinstance(value, str):
            if not _INTEGER.fullmatch(value):
                raise OtlpDecodeError(f"{what} must be an integer")
            number = int(value)
        elif isinstance(value, int):
            number = value
        elif isinstance(value, float) and value.is_integer():
            number = int(value)
        else:
            raise OtlpDecodeError(f"{what} must be an integer")
        if not low <= number <= high:
            raise OtlpDecodeError(f"{what} is out of range")
        return number

    def _uint64(self, value: Any, what: str) -> int:
        return self._int(value, what, 0, (1 << 64) - 1)

    def _uint32(self, value: Any, what: str) -> int:
        return self._int(value, what, 0, (1 << 32) - 1)

    def _dropped(self, obj: dict, excess: int, name: str = "droppedAttributesCount") -> int:
        """The sender's dropped count plus what this decoder dropped."""
        return excess + self._uint32(self._get(obj, name), name)

    def _enum(self, value: Any, what: str, names: dict[str, int]) -> int:
        if isinstance(value, str) and not _INTEGER.fullmatch(value):
            if value not in names:
                raise OtlpDecodeError(f"{what} is not a known name")
            return names[value]
        return self._int(value, what, -(1 << 31), (1 << 31) - 1)

    def _double(self, value: Any, what: str) -> float:
        if isinstance(value, bool):
            raise OtlpDecodeError(f"{what} must be a number")
        if isinstance(value, (int, float)):
            return float(value)
        if value in ("NaN", "Infinity", "-Infinity"):
            return float(value)
        if isinstance(value, str) and _DOUBLE_TEXT.fullmatch(value):
            return float(value)
        raise OtlpDecodeError(f"{what} must be a number")

    def _bytes(self, value: Any, what: str) -> bytes:
        if not isinstance(value, str):
            raise OtlpDecodeError(f"{what} must be a base64 string")
        text = value.replace("-", "+").replace("_", "/").rstrip("=")
        try:
            decoded = base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
        except ValueError:
            raise OtlpDecodeError(f"{what} is not valid base64") from None
        return decoded[: self.limits.max_string_bytes]

    # -- messages --

    def request(self, document: Any) -> ExportRequest:
        if not isinstance(document, dict):
            raise OtlpDecodeError("the request must be a JSON object")
        root = document
        return ExportRequest(
            tuple(
                self._resource_spans(item)
                for item in self._list(self._get(root, "resourceSpans"), "resourceSpans")
            )
        )

    def _resource_spans(self, value: Any) -> ResourceSpans:
        obj = self._object(value, "resourceSpans item")
        resource = self._object(self._get(obj, "resource"), "resource")
        attributes, dropped = self._attributes(self._get(resource, "attributes"))
        return ResourceSpans(
            Resource(
                attributes,
                self._dropped(resource, dropped),
            ),
            tuple(
                self._scope_spans(item)
                for item in self._list(self._get(obj, "scopeSpans"), "scopeSpans")
            ),
            self._str(self._get(obj, "schemaUrl"), "schemaUrl"),
        )

    def _scope_spans(self, value: Any) -> ScopeSpans:
        obj = self._object(value, "scopeSpans item")
        scope = self._object(self._get(obj, "scope"), "scope")
        attributes, dropped = self._attributes(self._get(scope, "attributes"))
        return ScopeSpans(
            Scope(
                self._str(self._get(scope, "name"), "scope name"),
                self._str(self._get(scope, "version"), "scope version"),
                attributes,
                self._dropped(scope, dropped),
            ),
            tuple(self._span(item) for item in self._list(self._get(obj, "spans"), "spans")),
            self._str(self._get(obj, "schemaUrl"), "schemaUrl"),
        )

    def _span(self, value: Any) -> Span:
        obj = self._object(value, "span")
        trace_id = self._hex(self._get(obj, "traceId"), "traceId")
        span_id = self._hex(self._get(obj, "spanId"), "spanId")
        parent = self._hex(self._get(obj, "parentSpanId"), "parentSpanId")
        start = self._uint64(self._get(obj, "startTimeUnixNano"), "startTimeUnixNano")
        attributes, dropped_attributes = self._attributes(self._get(obj, "attributes"))
        events, event_excess = self._capped(
            self._get(obj, "events"), "events", self.limits.max_events, self._event
        )
        links, link_excess = self._capped(
            self._get(obj, "links"), "links", self.limits.max_links, self._link
        )
        status = self._object(self._get(obj, "status"), "status")
        return Span(
            trace_id=trace_id,
            span_id=span_id,
            trace_state=self._str(self._get(obj, "traceState"), "traceState"),
            parent_span_id=parent,
            name=self._str(self._get(obj, "name"), "span name"),
            kind=self._enum(self._get(obj, "kind"), "kind", _SPAN_KINDS),
            start_time_unix_nano=start,
            end_time_unix_nano=self._uint64(self._get(obj, "endTimeUnixNano"), "endTimeUnixNano"),
            attributes=attributes,
            dropped_attributes_count=self._dropped(obj, dropped_attributes),
            events=events,
            dropped_events_count=self._dropped(obj, event_excess, "droppedEventsCount"),
            links=links,
            dropped_links_count=self._dropped(obj, link_excess, "droppedLinksCount"),
            status=Status(
                self._str(self._get(status, "message"), "status message"),
                self._enum(self._get(status, "code"), "status code", _STATUS_CODES),
            ),
            flags=self._uint32(self._get(obj, "flags"), "flags"),
            reject=span_reject_reason(trace_id, span_id, parent, start),
        )

    def _capped(
        self, value: Any, what: str, cap: int, parse: Callable[[Any], Any]
    ) -> tuple[tuple, int]:
        parsed = [parse(item) for item in self._list(value, what)]
        return tuple(parsed[:cap]), max(0, len(parsed) - cap)

    def _event(self, value: Any) -> Event:
        obj = self._object(value, "event")
        attributes, dropped = self._attributes(self._get(obj, "attributes"))
        return Event(
            self._uint64(self._get(obj, "timeUnixNano"), "timeUnixNano"),
            self._str(self._get(obj, "name"), "event name"),
            attributes,
            self._dropped(obj, dropped),
        )

    def _link(self, value: Any) -> Link:
        obj = self._object(value, "link")
        attributes, dropped = self._attributes(self._get(obj, "attributes"))
        return Link(
            self._hex(self._get(obj, "traceId"), "traceId"),
            self._hex(self._get(obj, "spanId"), "spanId"),
            self._str(self._get(obj, "traceState"), "traceState"),
            attributes,
            self._dropped(obj, dropped),
            self._uint32(self._get(obj, "flags"), "flags"),
        )

    def _attributes(self, value: Any) -> tuple[Attributes, int]:
        collected = _Attributes(self.limits)
        for item in self._list(value, "attributes"):
            collected.add(*self._key_value(item, 1))
        return collected.items, collected.excess

    def _key_value(self, value: Any, depth: int) -> tuple[str, AnyValue]:
        obj = self._object(value, "key-value")
        return (
            self._str(obj.get("key"), "attribute key"),
            self._any_value(self._get(obj, "value"), depth),
        )

    _ANY_MEMBERS = (
        "stringValue",
        "boolValue",
        "intValue",
        "doubleValue",
        "arrayValue",
        "kvlistValue",
        "bytesValue",
    )

    def _any_value(self, value: Any, depth: int) -> AnyValue:
        if depth > self.limits.max_depth:
            raise OtlpDecodeError(f"attribute value is nested deeper than {self.limits.max_depth}")
        obj = self._object(value, "attribute value")
        present = [m for m in self._ANY_MEMBERS if self._get(obj, m) is not None]
        if not present:
            return None
        if len(present) > 1:
            raise OtlpDecodeError("attribute value sets more than one member")
        member = present[0]
        raw = self._get(obj, member)
        if member == "stringValue":
            return self._str(raw, member)
        if member == "boolValue":
            if not isinstance(raw, bool):
                raise OtlpDecodeError("boolValue must be a boolean")
            return raw
        if member == "intValue":
            return self._int(raw, member, -(1 << 63), (1 << 63) - 1)
        if member == "doubleValue":
            return self._double(raw, member)
        if member == "bytesValue":
            return self._bytes(raw, member)
        items = self._list(self._get(self._object(raw, member), "values"), "values")
        if member == "arrayValue":
            return [self._any_value(item, depth + 1) for item in items]
        result: dict[str, AnyValue] = {}
        for item in items:
            key, item_value = self._key_value(item, depth + 1)
            result[key] = item_value
        return result

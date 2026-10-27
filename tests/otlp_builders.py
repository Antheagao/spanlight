"""Test helpers for the OTLP decoder: Hypothesis strategies that build real
`opentelemetry-proto` messages, and canonical forms that let a decoded
`ExportRequest` be compared with the reference's parse of the same bytes.

The canonical forms tag each value with its type (`("i", 1)` is not
`("d", 1.0)` or `("b", True)`, though Python says 1 == 1.0 == True).
"""

from __future__ import annotations

import base64
import math
import struct

from hypothesis import strategies as st
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import (
    AnyValue,
    ArrayValue,
    InstrumentationScope,
    KeyValue,
    KeyValueList,
)
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span, Status

from spanlight.server.otlp import ExportRequest

# -- canonical forms ---------------------------------------------------------


def _double(value: float):
    return ("d", "nan") if math.isnan(value) else ("d", struct.pack("<d", value))


def canon_any(value):
    """Canonical form of a decoded AnyValue."""
    if value is None:
        return ("n",)
    if isinstance(value, bool):
        return ("b", value)
    if isinstance(value, int):
        return ("i", value)
    if isinstance(value, float):
        return _double(value)
    if isinstance(value, str):
        return ("s", value)
    if isinstance(value, bytes):
        return ("y", value)
    if isinstance(value, list):
        return ("a", [canon_any(item) for item in value])
    return ("m", {key: canon_any(item) for key, item in value.items()})


def canon_any_pb(value: AnyValue):
    which = value.WhichOneof("value")
    if which in (None, "string_value_strindex"):
        return ("n",)
    if which == "double_value":
        return _double(value.double_value)
    if which == "array_value":
        return ("a", [canon_any_pb(item) for item in value.array_value.values])
    if which == "kvlist_value":
        return ("m", {kv.key: canon_any_pb(kv.value) for kv in value.kvlist_value.values})
    kind = {"string_value": "s", "bool_value": "b", "int_value": "i", "bytes_value": "y"}[which]
    return (kind, getattr(value, which))


def _attrs_pb(items) -> dict:
    return {kv.key: canon_any_pb(kv.value) for kv in items}


def _attrs(items: dict) -> dict:
    return {key: canon_any(value) for key, value in items.items()}


def canon_model(request: ExportRequest) -> list:
    """A decoded request as plain data (the `reject` reason excluded)."""
    return [
        {
            "resource": {
                "attributes": _attrs(rs.resource.attributes),
                "dropped": rs.resource.dropped_attributes_count,
            },
            "schema_url": rs.schema_url,
            "scopes": [
                {
                    "scope": {
                        "name": ss.scope.name,
                        "version": ss.scope.version,
                        "attributes": _attrs(ss.scope.attributes),
                        "dropped": ss.scope.dropped_attributes_count,
                    },
                    "schema_url": ss.schema_url,
                    "spans": [
                        {
                            "trace_id": sp.trace_id,
                            "span_id": sp.span_id,
                            "trace_state": sp.trace_state,
                            "parent_span_id": sp.parent_span_id,
                            "name": sp.name,
                            "kind": sp.kind,
                            "start": sp.start_time_unix_nano,
                            "end": sp.end_time_unix_nano,
                            "attributes": _attrs(sp.attributes),
                            "dropped_attributes": sp.dropped_attributes_count,
                            "events": [
                                {
                                    "time": e.time_unix_nano,
                                    "name": e.name,
                                    "attributes": _attrs(e.attributes),
                                    "dropped": e.dropped_attributes_count,
                                }
                                for e in sp.events
                            ],
                            "dropped_events": sp.dropped_events_count,
                            "links": [
                                {
                                    "trace_id": link.trace_id,
                                    "span_id": link.span_id,
                                    "trace_state": link.trace_state,
                                    "attributes": _attrs(link.attributes),
                                    "dropped": link.dropped_attributes_count,
                                    "flags": link.flags,
                                }
                                for link in sp.links
                            ],
                            "dropped_links": sp.dropped_links_count,
                            "status": {"message": sp.status.message, "code": sp.status.code},
                            "flags": sp.flags,
                        }
                        for sp in ss.spans
                    ],
                }
                for ss in rs.scope_spans
            ],
        }
        for rs in request.resource_spans
    ]


def canon_pb(request: ExportTraceServiceRequest) -> list:
    """The reference's parse of the same bytes, in the same shape."""
    return [
        {
            "resource": {
                "attributes": _attrs_pb(rs.resource.attributes),
                "dropped": rs.resource.dropped_attributes_count,
            },
            "schema_url": rs.schema_url,
            "scopes": [
                {
                    "scope": {
                        "name": ss.scope.name,
                        "version": ss.scope.version,
                        "attributes": _attrs_pb(ss.scope.attributes),
                        "dropped": ss.scope.dropped_attributes_count,
                    },
                    "schema_url": ss.schema_url,
                    "spans": [
                        {
                            "trace_id": sp.trace_id,
                            "span_id": sp.span_id,
                            "trace_state": sp.trace_state,
                            "parent_span_id": sp.parent_span_id,
                            "name": sp.name,
                            "kind": sp.kind,
                            "start": sp.start_time_unix_nano,
                            "end": sp.end_time_unix_nano,
                            "attributes": _attrs_pb(sp.attributes),
                            "dropped_attributes": sp.dropped_attributes_count,
                            "events": [
                                {
                                    "time": e.time_unix_nano,
                                    "name": e.name,
                                    "attributes": _attrs_pb(e.attributes),
                                    "dropped": e.dropped_attributes_count,
                                }
                                for e in sp.events
                            ],
                            "dropped_events": sp.dropped_events_count,
                            "links": [
                                {
                                    "trace_id": link.trace_id,
                                    "span_id": link.span_id,
                                    "trace_state": link.trace_state,
                                    "attributes": _attrs_pb(link.attributes),
                                    "dropped": link.dropped_attributes_count,
                                    "flags": link.flags,
                                }
                                for link in sp.links
                            ],
                            "dropped_links": sp.dropped_links_count,
                            "status": {"message": sp.status.message, "code": sp.status.code},
                            "flags": sp.flags,
                        }
                        for sp in ss.spans
                    ],
                }
                for ss in rs.scope_spans
            ],
        }
        for rs in request.resource_spans
    ]


# -- building real messages --------------------------------------------------


def any_value(tree) -> AnyValue:
    """An AnyValue from a tree: None, ("s", str), ("b", bool), ("i", int),
    ("d", float), ("y", bytes), ("a", [tree]) or ("m", [(key, tree)])."""
    if tree is None:
        return AnyValue()
    kind, value = tree
    if kind == "s":
        return AnyValue(string_value=value)
    if kind == "b":
        return AnyValue(bool_value=value)
    if kind == "i":
        return AnyValue(int_value=value)
    if kind == "d":
        return AnyValue(double_value=value)
    if kind == "y":
        return AnyValue(bytes_value=value)
    if kind == "a":
        return AnyValue(array_value=ArrayValue(values=[any_value(item) for item in value]))
    return AnyValue(
        kvlist_value=KeyValueList(values=[KeyValue(key=k, value=any_value(v)) for k, v in value])
    )


def key_values(pairs) -> list[KeyValue]:
    return [KeyValue(key=key, value=any_value(tree)) for key, tree in pairs]


def nested_arrays(levels: int) -> AnyValue:
    """An AnyValue that is `levels` AnyValues deep (an array in an array ... of a string)."""
    value = AnyValue(string_value="leaf")
    for _ in range(levels - 1):
        value = AnyValue(array_value=ArrayValue(values=[value]))
    return value


# -- strategies --------------------------------------------------------------

# Hypothesis text never contains lone surrogates, so these are valid UTF-8.
texts = st.text(max_size=12)
keys = st.text(max_size=6)
int64s = st.integers(-(2**63), 2**63 - 1)
uint64s = st.integers(0, 2**64 - 1)
uint32s = st.integers(0, 2**32 - 1)
int32s = st.integers(-(2**31), 2**31 - 1)

leaves = st.one_of(
    st.none(),
    st.tuples(st.just("s"), texts),
    st.tuples(st.just("b"), st.booleans()),
    st.tuples(st.just("i"), int64s),
    st.tuples(st.just("d"), st.floats(allow_nan=True, allow_infinity=True)),
    st.tuples(st.just("y"), st.binary(max_size=12)),
)
trees = st.recursive(
    leaves,
    lambda inner: st.one_of(
        st.tuples(st.just("a"), st.lists(inner, max_size=3)),
        st.tuples(st.just("m"), st.lists(st.tuples(keys, inner), max_size=3)),
    ),
    max_leaves=8,
)
attribute_pairs = st.lists(st.tuples(keys, trees), max_size=4)

ids16 = st.one_of(st.binary(min_size=16, max_size=16), st.binary(max_size=20))
ids8 = st.one_of(st.binary(min_size=8, max_size=8), st.binary(max_size=12))


@st.composite
def events(draw):
    return Span.Event(
        time_unix_nano=draw(uint64s),
        name=draw(texts),
        attributes=key_values(draw(attribute_pairs)),
        dropped_attributes_count=draw(uint32s),
    )


@st.composite
def links(draw):
    return Span.Link(
        trace_id=draw(ids16),
        span_id=draw(ids8),
        trace_state=draw(texts),
        attributes=key_values(draw(attribute_pairs)),
        dropped_attributes_count=draw(uint32s),
        flags=draw(uint32s),
    )


@st.composite
def spans(draw):
    return Span(
        trace_id=draw(ids16),
        span_id=draw(ids8),
        trace_state=draw(texts),
        parent_span_id=draw(ids8),
        name=draw(texts),
        kind=draw(st.one_of(st.integers(0, 5), int32s)),
        start_time_unix_nano=draw(uint64s),
        end_time_unix_nano=draw(uint64s),
        attributes=key_values(draw(attribute_pairs)),
        dropped_attributes_count=draw(uint32s),
        events=draw(st.lists(events(), max_size=3)),
        dropped_events_count=draw(uint32s),
        links=draw(st.lists(links(), max_size=3)),
        dropped_links_count=draw(uint32s),
        status=Status(message=draw(texts), code=draw(st.one_of(st.integers(0, 2), int32s))),
        flags=draw(uint32s),
    )


@st.composite
def export_requests(draw):
    return ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(
                resource=Resource(
                    attributes=key_values(draw(attribute_pairs)),
                    dropped_attributes_count=draw(uint32s),
                ),
                scope_spans=[
                    ScopeSpans(
                        scope=InstrumentationScope(
                            name=draw(texts),
                            version=draw(texts),
                            attributes=key_values(draw(attribute_pairs)),
                            dropped_attributes_count=draw(uint32s),
                        ),
                        spans=draw(st.lists(spans(), max_size=3)),
                        schema_url=draw(texts),
                    )
                    for _ in range(draw(st.integers(0, 2)))
                ],
                schema_url=draw(texts),
            )
            for _ in range(draw(st.integers(0, 2)))
        ]
    )


# -- the reference's JSON form, with ids as OTLP/JSON writes them ------------


def otlp_json(request: ExportTraceServiceRequest) -> dict:
    """`MessageToDict` of a message, with ids rewritten from base64 (proto3
    JSON) to hex (OTLP/JSON)."""
    from google.protobuf.json_format import MessageToDict

    document = MessageToDict(request)

    def to_hex(obj: dict, *names: str) -> None:
        for name in names:
            if name in obj:
                obj[name] = base64.b64decode(obj[name]).hex()

    for rs in document.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                to_hex(sp, "traceId", "spanId", "parentSpanId")
                for link in sp.get("links", []):
                    to_hex(link, "traceId", "spanId")
    return document


# -- doc-pilot's real exporter output ---------------------------------------


def docpilot_capture() -> list[bytes]:
    """The protobuf bodies doc-pilot's OTLP exporter sent (the API process,
    then the worker), captured by fixtures/otlp/capture_docpilot.py."""
    import json
    from pathlib import Path

    path = Path(__file__).parent / "fixtures/otlp/docpilot_capture.json"
    document = json.loads(path.read_text())
    return [base64.b64decode(item["body_base64"]) for item in document["requests"]]


def single_span_requests(bodies: list[bytes]) -> list[bytes]:
    """One request per span, each keeping its resource and scope."""
    out = []
    for body in bodies:
        request = ExportTraceServiceRequest.FromString(body)
        for rs in request.resource_spans:
            for ss in rs.scope_spans:
                for span in ss.spans:
                    one = ExportTraceServiceRequest()
                    copy_rs = one.resource_spans.add(schema_url=rs.schema_url)
                    copy_rs.resource.CopyFrom(rs.resource)
                    copy_ss = copy_rs.scope_spans.add(schema_url=ss.schema_url)
                    copy_ss.scope.CopyFrom(ss.scope)
                    copy_ss.spans.add().CopyFrom(span)
                    out.append(one.SerializeToString())
    return out

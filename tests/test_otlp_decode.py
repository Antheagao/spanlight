"""The OTLP decoder against the reference implementation (`opentelemetry-proto`)
and against hostile input.

For protobuf, the reference's parse of the same bytes is the oracle: whatever
it accepts we must decode to the same thing, and whatever it rejects we must
reject, with nothing but `OtlpDecodeError` ever escaping.
"""

import base64
import copy
import json
import time
import tracemalloc
from pathlib import Path

import pytest
from google.protobuf.message import DecodeError
from google.rpc import status_pb2
from hypothesis import given, settings
from hypothesis import strategies as st
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTracePartialSuccess,
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import (
    AnyValue,
    ArrayValue,
    EntityRef,
    KeyValue,
    KeyValueList,
)
from otlp_builders import (
    canon_any,
    canon_model,
    canon_pb,
    export_requests,
    key_values,
    nested_arrays,
    otlp_json,
)

from spanlight.server.otlp import (
    DEFAULT_LIMITS,
    Limits,
    OtlpDecodeError,
    decode_json,
    decode_protobuf,
    dumps_json_safe,
    encode_export_response,
    encode_status,
    json_safe,
)
from spanlight.server.otlp.wire import UnsupportedWireType

FIXTURES = Path(__file__).parent / "fixtures" / "otlp"

# -- helpers -----------------------------------------------------------------


def varint(value: int) -> bytes:
    value &= 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while value > 0x7F:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def field(number: int, wire_type: int, payload: bytes) -> bytes:
    return varint(number << 3 | wire_type) + payload


def len_field(number: int, payload: bytes) -> bytes:
    return field(number, 2, varint(len(payload)) + payload)


def request_with_attribute(value_bytes: bytes, key: str = "k") -> bytes:
    """A request whose one span has one attribute whose AnyValue is `value_bytes`."""
    key_value = len_field(1, key.encode()) + len_field(2, value_bytes)
    span = len_field(9, key_value)
    return len_field(1, len_field(2, len_field(2, span)))


def attribute_of(data: bytes):
    request = decode_protobuf(data)
    return request.resource_spans[0].scope_spans[0].spans[0].attributes["k"]


def reference(data: bytes):
    message = ExportTraceServiceRequest()
    try:
        message.ParseFromString(data)
    except DecodeError:
        return None
    return message


def check_against_reference(data: bytes) -> bool:
    """Decode with both. Returns whether the request was accepted. Fails if we
    raise anything but OtlpDecodeError, accept what the reference rejects,
    reject what it accepts (but for a group, which we refuse on purpose), or
    decode differently."""
    expected = reference(data)
    try:
        ours = decode_protobuf(data)
    except UnsupportedWireType:
        return False  # a deliberate divergence: the reference skips groups
    except OtlpDecodeError:
        assert expected is None, "rejected what the reference accepts"
        return False
    assert expected is not None, "accepted what the reference rejects"
    assert canon_model(ours) == canon_pb(expected)
    return True


def rich_request() -> ExportTraceServiceRequest:
    """One request that touches every message type and field."""
    tree = (
        "m",
        [
            ("s", ("s", "text")),
            ("b", ("b", True)),
            ("i", ("i", -(2**63))),
            ("d", ("d", 1.5)),
            ("y", ("y", b"\x00\xff")),
            ("a", ("a", [("i", 1), None, ("m", [("deep", ("a", [("s", "x")]))])])),
        ],
    )
    request = ExportTraceServiceRequest()
    rs = request.resource_spans.add(schema_url="https://schema/rs")
    rs.resource.attributes.extend(key_values([("service.name", ("s", "svc")), ("tree", tree)]))
    rs.resource.dropped_attributes_count = 3
    rs.resource.entity_refs.append(EntityRef(schema_url="u", type="service", id_keys=["a", "b"]))
    ss = rs.scope_spans.add(schema_url="https://schema/ss")
    ss.scope.name, ss.scope.version, ss.scope.dropped_attributes_count = "lib", "1.2", 1
    ss.scope.attributes.extend(key_values([("scope.attr", ("i", 7))]))
    span = ss.spans.add()
    span.trace_id, span.span_id = bytes(range(1, 17)), bytes(range(1, 9))
    span.parent_span_id = b"\x09" * 8
    span.trace_state, span.name, span.kind = "a=b", "root", 3
    span.start_time_unix_nano, span.end_time_unix_nano = 1_000_000_000, 2_000_000_000
    span.attributes.extend(key_values([("attr", tree), ("n", None)]))
    span.dropped_attributes_count, span.dropped_events_count, span.dropped_links_count = 1, 2, 3
    span.flags = 0x101
    span.status.message, span.status.code = "bad", 2
    event = span.events.add(
        time_unix_nano=1_500_000_000, name="exception", dropped_attributes_count=4
    )
    event.attributes.extend(key_values([("exception.type", ("s", "ValueError"))]))
    link = span.links.add(trace_id=b"\x07" * 16, span_id=b"\x08" * 8, trace_state="x=y", flags=5)
    link.attributes.extend(key_values([("link.attr", ("b", False))]))
    link.dropped_attributes_count = 6
    rs.scope_spans.add().spans.add()  # an empty span in a second scope
    request.resource_spans.add()  # and an empty resource
    return request


UNKNOWN_FIELDS = {
    "varint": bytes([0xC0, 0x3E, 0x05]),  # field 1000, varint 5
    "fixed64": bytes([0xC1, 0x3E]) + b"\x01" * 8,
    "len": bytes([0xC2, 0x3E, 0x03]) + b"abc",
    "fixed32": bytes([0xC5, 0x3E]) + b"\x01" * 4,
}

def _tree(request):
    return request.resource_spans[0].resource.attributes[1].value.kvlist_value


# Where to inject: a path from the request to each message type.
TARGETS = {
    "request": lambda r: r,
    "resource_spans": lambda r: r.resource_spans[0],
    "resource": lambda r: r.resource_spans[0].resource,
    "entity_ref": lambda r: r.resource_spans[0].resource.entity_refs[0],
    "scope_spans": lambda r: r.resource_spans[0].scope_spans[0],
    "scope": lambda r: r.resource_spans[0].scope_spans[0].scope,
    "span": lambda r: r.resource_spans[0].scope_spans[0].spans[0],
    "event": lambda r: r.resource_spans[0].scope_spans[0].spans[0].events[0],
    "link": lambda r: r.resource_spans[0].scope_spans[0].spans[0].links[0],
    "status": lambda r: r.resource_spans[0].scope_spans[0].spans[0].status,
    "key_value": lambda r: r.resource_spans[0].resource.attributes[1],
    "any_value": lambda r: r.resource_spans[0].resource.attributes[1].value,
    "kvlist": lambda r: r.resource_spans[0].resource.attributes[1].value.kvlist_value,
    "kvlist_entry": lambda r: _tree(r).values[5],
    "array": lambda r: _tree(r).values[5].value.array_value,
}


# -- agreement with the reference --------------------------------------------


@given(export_requests())
def test_decoding_matches_the_reference_on_generated_messages(message):
    data = message.SerializeToString()
    ours = decode_protobuf(data)
    assert canon_model(ours) == canon_pb(message)
    assert canon_model(ours) == canon_pb(ExportTraceServiceRequest.FromString(data))


def test_the_rich_message_decodes_to_what_it_says():
    request = decode_protobuf(rich_request().SerializeToString())
    rs = request.resource_spans[0]
    assert rs.resource.attributes["service.name"] == "svc"
    assert rs.resource.dropped_attributes_count == 3
    span = rs.scope_spans[0].spans[0]
    assert (span.name, span.kind, span.flags) == ("root", 3, 0x101)
    assert span.reject is None
    assert span.status.code == 2
    assert span.attributes["attr"]["i"] == -(2**63)
    assert span.attributes["attr"]["d"] == 1.5
    assert span.attributes["attr"]["y"] == b"\x00\xff"
    assert span.attributes["attr"]["a"] == [1, None, {"deep": ["x"]}]
    assert span.attributes["n"] is None
    assert (span.dropped_attributes_count, span.dropped_events_count, span.dropped_links_count) == (
        1,
        2,
        3,
    )
    assert span.events[0].attributes == {"exception.type": "ValueError"}
    assert span.links[0].trace_id == b"\x07" * 16
    # An empty span is decoded, and is a reject for the mapping to count.
    assert rs.scope_spans[1].spans[0].reject == "trace_id must be 16 bytes"


@given(export_requests())
def test_reject_reasons_follow_the_otlp_id_rules(message):
    request = decode_protobuf(message.SerializeToString())
    for rs in request.resource_spans:
        for ss in rs.scope_spans:
            for span in ss.spans:
                bad = (
                    len(span.trace_id) != 16
                    or not any(span.trace_id)
                    or len(span.span_id) != 8
                    or not any(span.span_id)
                    or (span.parent_span_id and len(span.parent_span_id) != 8)
                    or span.start_time_unix_nano == 0
                )
                assert (span.reject is not None) == bool(bad)


@pytest.mark.parametrize(
    ("trace_id", "span_id", "parent", "start", "reason"),
    [
        (b"\x01" * 16, b"\x02" * 8, b"", 1, None),
        (b"\x01" * 16, b"\x02" * 8, b"\x03" * 8, 1, None),
        (b"\x01" * 15, b"\x02" * 8, b"", 1, "trace_id must be 16 bytes"),
        (b"\x00" * 16, b"\x02" * 8, b"", 1, "trace_id must not be all zeros"),
        (b"\x01" * 16, b"\x02" * 7, b"", 1, "span_id must be 8 bytes"),
        (b"\x01" * 16, b"\x00" * 8, b"", 1, "span_id must not be all zeros"),
        (b"\x01" * 16, b"\x02" * 8, b"\x03" * 3, 1, "parent_span_id must be empty or 8 bytes"),
        (b"\x01" * 16, b"\x02" * 8, b"", 0, "start_time_unix_nano must not be 0"),
    ],
)
def test_reject_reasons(trace_id, span_id, parent, start, reason):
    message = ExportTraceServiceRequest()
    span = message.resource_spans.add().scope_spans.add().spans.add()
    span.trace_id, span.span_id, span.parent_span_id = trace_id, span_id, parent
    span.start_time_unix_nano = start
    decoded = decode_protobuf(message.SerializeToString())
    assert decoded.resource_spans[0].scope_spans[0].spans[0].reject == reason


# -- hostile input -----------------------------------------------------------


@given(st.binary(max_size=200))
def test_random_bytes_are_decoded_or_rejected_like_the_reference(data):
    check_against_reference(data)


@given(st.binary(max_size=60))
def test_random_bytes_inside_a_span_are_decoded_or_rejected_like_the_reference(data):
    """Random bytes reach the inner messages only when wrapped in valid framing."""
    for wrapped in (
        len_field(1, len_field(2, len_field(2, data))),  # a Span
        len_field(1, len_field(1, data)),  # a Resource
        len_field(1, len_field(2, len_field(1, data))),  # a Scope
        request_with_attribute(data),  # an AnyValue
    ):
        check_against_reference(wrapped)


def test_every_truncation_of_a_valid_message_is_rejected_or_matches_the_reference():
    message = rich_request()
    data = message.SerializeToString()
    accepted = [n for n in range(len(data)) if check_against_reference(data[:n])]
    # Only cuts between top-level fields leave a well-formed message.
    boundaries = [
        len(ExportTraceServiceRequest(resource_spans=message.resource_spans[:k]).SerializeToString())
        for k in range(len(message.resource_spans))
    ]
    assert accepted == boundaries
    assert check_against_reference(data)


@settings(max_examples=8)  # every offset of every example is a decode, so keep the count low
@given(export_requests())
def test_truncations_of_generated_messages(message):
    data = message.SerializeToString()
    for n in range(len(data)):
        check_against_reference(data[:n])


mutations = st.lists(
    st.tuples(
        st.sampled_from(["flip", "insert", "delete", "repeat", "zero"]),
        st.integers(0, 10**6),
        st.binary(min_size=1, max_size=6),
    ),
    min_size=1,
    max_size=3,
)


def mutate(data: bytes, steps) -> bytes:
    out = bytearray(data)
    for action, at, blob in steps:
        if not out:
            break
        at %= len(out)
        if action == "flip":
            out[at] ^= blob[0] or 1
        elif action == "insert":
            out[at:at] = blob
        elif action == "delete":
            del out[at : at + len(blob)]
        elif action == "repeat":
            out[at:at] = out[at : at + len(blob) * 3]
        else:
            out[at] = 0
    return bytes(out)


@given(export_requests(), mutations)
def test_mutated_messages_are_decoded_or_rejected_like_the_reference(message, steps):
    check_against_reference(mutate(message.SerializeToString(), steps))


@given(mutations)
def test_mutations_of_the_rich_message(steps):
    check_against_reference(mutate(rich_request().SerializeToString(), steps))


def test_invalid_utf8_rejects_the_request_wherever_a_string_is():
    request = rich_request()
    good = request.SerializeToString()
    for text in (b"name", b"https://schema/rs", b"svc", b"ValueError", b"exception", b"text"):
        assert text in good
        data = good.replace(text, b"\xff" * len(text), 1)
        with pytest.raises(OtlpDecodeError):
            decode_protobuf(data)
        assert reference(data) is None


def test_invalid_utf8_in_a_replaced_oneof_string_still_rejects():
    data = request_with_attribute(len_field(1, b"\xff") + AnyValue(int_value=1).SerializeToString())
    assert reference(data) is None
    with pytest.raises(OtlpDecodeError):
        decode_protobuf(data)


# -- length prefixes and allocation ------------------------------------------


@pytest.mark.parametrize(
    "length", [2**31 - 1, 2**31, 2**32 + 5, 2**63 - 1, 2**64 - 1, 10**6, 6]
)
def test_a_length_larger_than_the_rest_of_the_message_is_rejected_without_allocating(length):
    inner = field(2, 0, varint(1)) + field(5, 2, varint(length)) + b"short"
    for data in (field(1, 2, varint(length)) + b"tail", len_field(1, inner)):
        tracemalloc.start()
        try:
            with pytest.raises(OtlpDecodeError):
                decode_protobuf(data)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < 100_000
        assert reference(data) is None


@pytest.mark.parametrize(
    "data",
    [
        b"\x08",  # a tag with no value
        b"\x80",  # a tag cut short
        b"\x0a\x80",  # a length cut short
        b"\x09\x01\x02",  # fixed64 with 2 bytes
        b"\x0d\x01\x02",  # fixed32 with 2 bytes
        b"\x00\x00",  # field number 0
        b"\x0e",  # wire type 6
        b"\x0f",  # wire type 7
        b"\x08" + b"\xff" * 10 + b"\x01",  # an 11 byte varint
        b"\xff\xff\xff\xff\xff\x7f\x00",  # a 6 byte tag
    ],
)
def test_malformed_framing_is_rejected(data):
    with pytest.raises(OtlpDecodeError):
        decode_protobuf(data)
    assert reference(data) is None


def test_a_group_is_rejected_on_purpose():
    # Field 1000 as a start group, then an end group: a conforming parser skips it.
    data = bytes([0xC3, 0x3E, 0xC4, 0x3E])
    assert reference(data) is not None
    with pytest.raises(UnsupportedWireType):
        decode_protobuf(data)


def test_a_ten_byte_varint_wraps_to_64_bits_like_the_reference():
    # The 10th byte carries bits 63 and up; the reference keeps the low 64.
    span = field(6, 0, b"\xff" * 9 + b"\x7f")
    data = len_field(1, len_field(2, len_field(2, span)))
    assert check_against_reference(data)


# -- proto3 semantics --------------------------------------------------------


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("wire", UNKNOWN_FIELDS)
def test_unknown_fields_of_every_wire_type_are_skipped(target, wire):
    base = rich_request()
    expected = canon_pb(base)
    injected = copy.deepcopy(base)
    TARGETS[target](injected).MergeFromString(UNKNOWN_FIELDS[wire])
    data = injected.SerializeToString()
    assert data != base.SerializeToString(), "the unknown field must be in the bytes"
    assert canon_model(decode_protobuf(data)) == expected


def test_a_known_field_with_the_wrong_wire_type_is_skipped():
    for number, wrong in ((5, field(5, 0, varint(7))), (7, field(7, 0, varint(7))),
                          (6, field(6, 2, varint(1) + b"x")), (16, field(16, 1, b"\x00" * 8))):
        data = len_field(1, len_field(2, len_field(2, wrong)))
        assert check_against_reference(data), number


def test_a_singular_scalar_sent_twice_keeps_the_last():
    span = field(6, 0, varint(1)) + field(6, 0, varint(4)) + len_field(5, b"a") + len_field(5, b"b")
    data = len_field(1, len_field(2, len_field(2, span)))
    assert check_against_reference(data)
    parsed = decode_protobuf(data).resource_spans[0].scope_spans[0].spans[0]
    assert (parsed.kind, parsed.name) == (4, "b")


def test_a_singular_message_sent_twice_is_merged():
    first = len_field(15, len_field(2, b"first") + field(3, 0, varint(1)))
    second = len_field(15, len_field(2, b"second"))
    data = len_field(1, len_field(2, len_field(2, first + second)))
    assert check_against_reference(data)
    status = decode_protobuf(data).resource_spans[0].scope_spans[0].spans[0].status
    assert (status.message, status.code) == ("second", 1)


def test_resource_and_scope_sent_twice_are_merged():
    one = AnyValue(int_value=1).SerializeToString()

    def attribute(key):
        return len_field(1, len_field(1, key.encode()) + len_field(2, one))

    resource_a = attribute("a")
    resource_b = attribute("b") + field(2, 0, varint(9))
    data = len_field(1, len_field(1, resource_a) + len_field(1, resource_b))
    assert check_against_reference(data)
    resource = decode_protobuf(data).resource_spans[0].resource
    assert set(resource.attributes) == {"a", "b"}
    assert resource.dropped_attributes_count == 9


def test_a_split_message_must_be_well_formed_in_each_piece():
    # Together the two halves are one valid Status; alone the first is cut short.
    whole = len_field(2, b"message")
    first, second = whole[:3], whole[3:]
    data = len_field(1, len_field(2, len_field(2, len_field(15, first) + len_field(15, second))))
    assert reference(data) is None
    with pytest.raises(OtlpDecodeError):
        decode_protobuf(data)


def test_in_the_anyvalue_oneof_the_last_member_wins():
    parts = [
        AnyValue(string_value="s").SerializeToString(),
        AnyValue(int_value=5).SerializeToString(),
        AnyValue(bool_value=True).SerializeToString(),
    ]
    for ordering in (parts, parts[::-1], parts[:1] + parts[:1]):
        data = request_with_attribute(b"".join(ordering))
        assert check_against_reference(data)
    assert attribute_of(request_with_attribute(b"".join(parts))) is True
    assert attribute_of(request_with_attribute(b"".join(parts[::-1]))) == "s"


def test_the_same_array_sent_twice_is_concatenated_and_a_different_member_replaces_it():
    array = lambda *items: AnyValue(  # noqa: E731
        array_value=ArrayValue(values=[AnyValue(int_value=i) for i in items])
    ).SerializeToString()
    twice = request_with_attribute(array(1, 2) + array(3))
    assert check_against_reference(twice)
    assert attribute_of(twice) == [1, 2, 3]
    text = AnyValue(string_value="x").SerializeToString()
    replaced = request_with_attribute(array(1) + text + array(2))
    assert check_against_reference(replaced)
    assert attribute_of(replaced) == [2]


def test_the_unused_string_index_member_of_the_oneof_clears_the_value():
    text = AnyValue(string_value="x").SerializeToString()
    data = request_with_attribute(text + field(8, 0, varint(3)))
    assert check_against_reference(data)
    assert attribute_of(data) is None


def test_a_repeated_attribute_key_keeps_the_last_value():
    span = (
        len_field(9, len_field(1, b"k") + len_field(2, AnyValue(int_value=1).SerializeToString()))
        + len_field(9, len_field(1, b"k") + len_field(2, AnyValue(int_value=2).SerializeToString()))
    )
    data = len_field(1, len_field(2, len_field(2, span)))
    assert attribute_of(data) == 2


def test_negative_and_boundary_integers():
    for value in (-1, 0, 2**63 - 1, -(2**63)):
        data = request_with_attribute(AnyValue(int_value=value).SerializeToString())
        assert attribute_of(data) == value
    status = len_field(15, field(3, 0, varint(-1)))  # a negative enum is sign-extended to 64 bits
    data = len_field(1, len_field(2, len_field(2, status)))
    assert check_against_reference(data)
    assert decode_protobuf(data).resource_spans[0].scope_spans[0].spans[0].status.code == -1


def test_doubles_keep_their_bits():
    import math

    for value in (0.0, -0.0, 1.5, float("inf"), float("-inf"), 5e-324):
        got = attribute_of(request_with_attribute(AnyValue(double_value=value).SerializeToString()))
        assert got == value and math.copysign(1, got) == math.copysign(1, value)
    data = request_with_attribute(AnyValue(double_value=float("nan")).SerializeToString())
    nan = attribute_of(data)
    assert nan != nan


# -- depth -------------------------------------------------------------------


def request_with(value: AnyValue) -> bytes:
    message = ExportTraceServiceRequest()
    message.resource_spans.add().resource.attributes.append(KeyValue(key="k", value=value))
    return message.SerializeToString()


def test_nesting_up_to_the_limit_is_accepted_and_one_more_is_rejected():
    ok = request_with(nested_arrays(DEFAULT_LIMITS.max_depth))
    assert check_against_reference(ok)
    too_deep = request_with(nested_arrays(DEFAULT_LIMITS.max_depth + 1))
    assert reference(too_deep) is not None
    with pytest.raises(OtlpDecodeError, match="nested deeper"):
        decode_protobuf(too_deep)


def test_nesting_through_kvlists_counts_too():
    def kvlists(levels):
        value = AnyValue(string_value="leaf")
        for _ in range(levels - 1):
            value = AnyValue(kvlist_value=KeyValueList(values=[KeyValue(key="k", value=value)]))
        return value

    assert check_against_reference(request_with(kvlists(DEFAULT_LIMITS.max_depth)))
    with pytest.raises(OtlpDecodeError, match="nested deeper"):
        decode_protobuf(request_with(kvlists(DEFAULT_LIMITS.max_depth + 1)))


def test_very_deep_input_is_rejected_without_recursing_far():
    # 100k levels of "array containing an array": rejected at depth 17.
    data = b""
    for _ in range(20_000):
        data = len_field(5, len_field(1, data))
    with pytest.raises(OtlpDecodeError):
        decode_protobuf(request_with_attribute(data))


# -- limits ------------------------------------------------------------------


def span_request(span) -> bytes:
    message = ExportTraceServiceRequest()
    message.resource_spans.add().scope_spans.add().spans.append(span)
    return message.SerializeToString()


def test_attributes_events_and_links_over_the_limit_are_dropped_and_counted():
    from opentelemetry.proto.trace.v1.trace_pb2 import Span

    span = Span(
        trace_id=b"\x01" * 16,
        span_id=b"\x02" * 8,
        start_time_unix_nano=1,
        attributes=key_values([(f"k{i}", ("i", i)) for i in range(200)]),
        dropped_attributes_count=5,
        events=[Span.Event(name=f"e{i}") for i in range(150)],
        links=[Span.Link(trace_id=b"\x01" * 16, span_id=b"\x02" * 8) for _ in range(129)],
        dropped_links_count=1,
    )
    decoded = decode_protobuf(span_request(span)).resource_spans[0].scope_spans[0].spans[0]
    assert len(decoded.attributes) == 128
    assert list(decoded.attributes)[-1] == "k127"
    assert decoded.dropped_attributes_count == 5 + 72
    assert (len(decoded.events), decoded.dropped_events_count) == (128, 22)
    assert (len(decoded.links), decoded.dropped_links_count) == (128, 2)


def test_overwriting_an_attribute_at_the_limit_is_not_a_drop():
    from opentelemetry.proto.trace.v1.trace_pb2 import Span

    pairs = [(f"k{i}", ("i", i)) for i in range(128)] + [("k0", ("i", -1))]
    span = Span(trace_id=b"\x01" * 16, span_id=b"\x02" * 8, start_time_unix_nano=1,
                attributes=key_values(pairs))
    decoded = decode_protobuf(span_request(span)).resource_spans[0].scope_spans[0].spans[0]
    assert decoded.attributes["k0"] == -1
    assert decoded.dropped_attributes_count == 0


def test_a_long_string_is_cut_on_a_character_boundary_and_marked():
    limits = Limits(max_string_bytes=10)
    value = AnyValue(string_value="a" * 8 + "éé")  # 8 + 2 + 2 bytes
    got = attribute_of_with(request_with(value), limits)
    assert got == "a" * 8 + "é" + "[truncated]"
    exact = attribute_of_with(request_with(AnyValue(string_value="a" * 10)), limits)
    assert exact == "a" * 10
    default = DEFAULT_LIMITS.max_string_bytes
    data = request_with(AnyValue(string_value="x" * (default + 1)))
    assert attribute_of_with(data, DEFAULT_LIMITS) == "x" * default + "[truncated]"


def attribute_of_with(data: bytes, limits: Limits):
    request = decode_protobuf(data, limits)
    return request.resource_spans[0].resource.attributes["k"]


def test_a_large_request_decodes_in_bounded_time():
    from opentelemetry.proto.trace.v1.trace_pb2 import Span

    span = Span(
        trace_id=b"\x01" * 16,
        span_id=b"\x02" * 8,
        start_time_unix_nano=1,
        attributes=key_values([(f"k{i}", ("s", "v")) for i in range(20_000)]),
    )
    data = span_request(span)
    started = time.monotonic()
    decoded = decode_protobuf(data).resource_spans[0].scope_spans[0].spans[0]
    assert time.monotonic() - started < 5
    assert len(decoded.attributes) == 128 and decoded.dropped_attributes_count == 20_000 - 128


# -- OTLP/JSON ---------------------------------------------------------------


def camel_to_snake(document):
    import re

    if isinstance(document, dict):
        return {re.sub(r"([A-Z])", lambda m: "_" + m.group(1).lower(), k): camel_to_snake(v)
                for k, v in document.items()}
    if isinstance(document, list):
        return [camel_to_snake(item) for item in document]
    return document


def test_the_spec_shaped_fixture_decodes():
    request = decode_json((FIXTURES / "trace.json").read_bytes())
    rs = request.resource_spans[0]
    assert rs.resource.attributes == {"service.name": "doc-pilot-api", "service.instance.id": 42}
    assert rs.resource.dropped_attributes_count == 1
    scope = rs.scope_spans[0].scope
    assert (scope.name, scope.version, scope.attributes) == (
        "opentelemetry.instrumentation.fastapi", "0.60b0", {"scope.flag": True})
    span = rs.scope_spans[0].spans[0]
    assert span.trace_id == bytes.fromhex("5b8efff798038103d269b633813fc60c")
    assert span.span_id == bytes.fromhex("eee19b7ec3c1b174")
    assert span.parent_span_id == b""
    assert (span.name, span.kind, span.trace_state) == ("POST /ask", 2, "vendor=value")
    assert span.start_time_unix_nano == 1544712660000000000
    assert span.reject is None
    attrs = span.attributes
    assert attrs["http.response.status_code"] == 200 and attrs["gen_ai.usage.input_tokens"] == 1234
    assert attrs["docpilot.cost_usd"] == 0.0123
    assert attrs["retry"] != attrs["retry"]  # NaN
    assert attrs["payload"] == b"\x00\x01\x02\x03"
    assert attrs["tags"] == ["a", 2, False]
    assert attrs["ctx"] == {"user": "u1"}
    assert attrs["unset"] is None
    assert span.dropped_attributes_count == 2
    assert span.events[0].attributes["gen_ai.evaluation.score.value"] == 1.0
    assert (span.dropped_events_count, span.dropped_links_count) == (3, 4)
    assert span.links[0].span_id == bytes.fromhex("0102030405060708") and span.links[0].flags == 256
    assert (span.status.code, span.status.message, span.flags) == (2, "boom", 257)
    assert rs.scope_spans[0].schema_url.endswith("1.27.0")


def test_camel_case_and_snake_case_keys_decode_the_same():
    document = json.loads((FIXTURES / "trace.json").read_text())
    camel = canon_model_json(decode_json(json.dumps(document)))
    snake = canon_model_json(decode_json(json.dumps(camel_to_snake(document))))
    assert camel == snake


def canon_model_json(request):
    """Canonical form via the protobuf round trip is overkill; compare reprs."""
    return repr(request).replace("nan", "NaN")


def json_span(**changes) -> dict:
    span = {"traceId": "01" * 16, "spanId": "02" * 8, "name": "n", "startTimeUnixNano": "1"}
    span.update(changes)
    return span


def decode_span(**changes):
    document = {"resourceSpans": [{"scopeSpans": [{"spans": [json_span(**changes)]}]}]}
    return decode_json(json.dumps(document)).resource_spans[0].scope_spans[0].spans[0]


def test_int64_may_be_a_string_or_a_number():
    for value in ("123456789012345678", 123456789012345678):
        assert decode_span(startTimeUnixNano=value).start_time_unix_nano == 123456789012345678
    for value in ("9223372036854775807", 9223372036854775807):
        got = decode_span(attributes=[{"key": "k", "value": {"intValue": value}}]).attributes["k"]
        assert got == 2**63 - 1
    assert decode_span(attributes=[{"key": "k", "value": {"intValue": "-5"}}]).attributes["k"] == -5


@pytest.mark.parametrize(
    "changes",
    [
        {"startTimeUnixNano": "12x"},
        {"startTimeUnixNano": "-1"},
        {"startTimeUnixNano": "18446744073709551616"},
        {"startTimeUnixNano": 1.5},
        {"startTimeUnixNano": True},
        {"startTimeUnixNano": [1]},
        {"traceId": "abc"},
        {"traceId": "zz" * 16},
        {"traceId": "01 " * 16},
        {"traceId": 5},
        {"kind": "SPAN_KIND_NOPE"},
        {"kind": 2**31},
        {"name": 5},
        {"attributes": {"key": "k"}},
        {"attributes": [{"key": 5, "value": {}}]},
        {"attributes": [{"key": "k", "value": {"stringValue": 5}}]},
        {"attributes": [{"key": "k", "value": {"boolValue": "true"}}]},
        {"attributes": [{"key": "k", "value": {"intValue": "1.5"}}]},
        {"attributes": [{"key": "k", "value": {"intValue": "9223372036854775808"}}]},
        {"attributes": [{"key": "k", "value": {"doubleValue": "lots"}}]},
        {"attributes": [{"key": "k", "value": {"bytesValue": "not base64!"}}]},
        {"attributes": [{"key": "k", "value": {"stringValue": "a", "intValue": 1}}]},
        {"attributes": [{"key": "k", "value": [1]}]},
        {"attributes": [{"key": "k", "value": {"arrayValue": {"values": "x"}}}]},
        {"status": {"code": "STATUS_CODE_MAYBE"}},
        {"name": "lone surrogate \ud800"},
        {"droppedAttributesCount": 2**32},
    ],
)
def test_malformed_json_values_are_rejected(changes):
    with pytest.raises(OtlpDecodeError):
        decode_span(**changes)


def test_json_ids_are_hex_in_either_case_and_empty_means_absent():
    span = decode_span(traceId="AB" * 16, spanId="cd" * 8, parentSpanId="")
    assert (span.trace_id, span.span_id) == (b"\xab" * 16, b"\xcd" * 8)
    assert span.parent_span_id == b""
    assert decode_span(traceId="aB" * 16).trace_id == b"\xab" * 16


def test_json_enums_may_be_numbers_or_names_and_unknown_keys_are_ignored():
    span = decode_span(kind="SPAN_KIND_PRODUCER", status={"code": "STATUS_CODE_OK"}, extra={"x": 1})
    assert (span.kind, span.status.code) == (4, 1)
    assert decode_span(kind=5, status={"code": 2}).kind == 5
    assert decode_span(kind="3").kind == 3
    request = decode_json(b'{"resourceSpans": [], "somethingNew": [1, 2]}')
    assert request.resource_spans == ()


def test_json_doubles_and_bytes():
    value = lambda v: decode_span(attributes=[{"key": "k", "value": v}]).attributes["k"]  # noqa: E731
    assert value({"doubleValue": 1}) == 1.0 and isinstance(value({"doubleValue": 1}), float)
    assert value({"doubleValue": "Infinity"}) == float("inf")
    assert value({"doubleValue": "-Infinity"}) == float("-inf")
    assert value({"doubleValue": "1.5e3"}) == 1500.0
    assert value({"bytesValue": "AAEC"}) == b"\x00\x01\x02"
    assert value({"bytesValue": "AAE"}) == b"\x00\x01"  # padding optional
    assert value({"bytesValue": "-_8="}) == b"\xfb\xff"  # URL-safe alphabet too
    assert value({"stringValue": ""}) == ""
    assert value({}) is None and value(None) is None


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"[]",
        b'"x"',
        b"null",
        b'{"resourceSpans": {}}',
        b'{"resourceSpans": [5]}',
        b'{"resourceSpans": [NaN]}',
        b'{"resourceSpans": [Infinity]}',
        b"\xff\xfe{}",
        b'{"resourceSpans": []',
        b"[" * 100_000,
        b'{"a":' * 100_000,
        b'{"resourceSpans": [{"resource": {"droppedAttributesCount": ' + b"9" * 5000 + b"}}]}",
    ],
)
def test_garbage_json_is_rejected_with_the_decode_error(body):
    with pytest.raises(OtlpDecodeError):
        decode_json(body)


def test_json_nesting_over_the_limit_is_rejected():
    def nested(levels):
        value = {"stringValue": "leaf"}
        for _ in range(levels - 1):
            value = {"arrayValue": {"values": [value]}}
        return value

    def request(value):
        attributes = [{"key": "k", "value": value}]
        return json.dumps({"resourceSpans": [{"resource": {"attributes": attributes}}]})

    assert decode_json(request(nested(16))).resource_spans[0].resource.attributes["k"] is not None
    with pytest.raises(OtlpDecodeError, match="nested deeper"):
        decode_json(request(nested(17)))


def test_json_limits_match_the_protobuf_ones():
    attributes = [{"key": f"k{i}", "value": {"intValue": i}} for i in range(200)]
    events = [{"name": f"e{i}"} for i in range(130)]
    span = decode_span(attributes=attributes, events=events, droppedEventsCount=1)
    assert len(span.attributes) == 128 and span.dropped_attributes_count == 72
    assert len(span.events) == 128 and span.dropped_events_count == 3


def rejects(request):
    return [
        span.reject
        for rs in request.resource_spans
        for ss in rs.scope_spans
        for span in ss.spans
    ]


@given(export_requests())
def test_json_and_protobuf_of_the_same_message_decode_the_same(message):
    from_protobuf = decode_protobuf(message.SerializeToString())
    from_json = decode_json(json.dumps(otlp_json(message)))
    assert canon_model(from_json) == canon_model(from_protobuf)
    assert rejects(from_json) == rejects(from_protobuf)


def test_json_snake_case_of_a_generated_message():
    message = rich_request()
    document = otlp_json(message)
    assert canon_model(decode_json(json.dumps(camel_to_snake(document)))) == canon_pb(message)


# -- encoders ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("rejected", "text"),
    [(0, ""), (0, "warning"), (2, ""), (2, "bad ids"), (2**40, "x" * 300), (1, "é\U0001f600")],
)
def test_the_export_response_round_trips_through_the_reference(rejected, text):
    body = encode_export_response(rejected, text, "protobuf")
    parsed = ExportTraceServiceResponse.FromString(body)
    partial = parsed.partial_success
    assert (partial.rejected_spans, partial.error_message) == (rejected, text)
    expected = ExportTraceServiceResponse(
        partial_success=ExportTracePartialSuccess(rejected_spans=rejected, error_message=text)
        if rejected or text else None
    )
    assert body == expected.SerializeToString()


def test_an_empty_success_is_zero_bytes_or_an_empty_object():
    assert encode_export_response(0, "", "protobuf") == b""
    assert encode_export_response(0, "", "json") == b"{}"


def test_the_json_export_response_writes_int64_as_a_string():
    from google.protobuf.json_format import Parse

    body = encode_export_response(2, "bad", "json")
    assert json.loads(body) == {"partialSuccess": {"rejectedSpans": "2", "errorMessage": "bad"}}
    parsed = Parse(body, ExportTraceServiceResponse())
    partial = parsed.partial_success
    assert (partial.rejected_spans, partial.error_message) == (2, "bad")


def test_a_negative_rejected_count_is_a_bug():
    with pytest.raises(ValueError):
        encode_export_response(-1, "", "protobuf")


@pytest.mark.parametrize(
    ("code", "message"), [(3, "bad"), (3, ""), (14, "database busy"), (16, "é"), (0, "")]
)
def test_status_round_trips_through_the_reference(code, message):
    body = encode_status(code, message, "protobuf")
    parsed = status_pb2.Status.FromString(body)
    assert (parsed.code, parsed.message) == (code, message)
    assert body == status_pb2.Status(code=code, message=message).SerializeToString()
    assert json.loads(encode_status(code, message, "json")) == {"code": code, "message": message}


# -- stored JSON -------------------------------------------------------------


def test_json_safe_values_survive_a_strict_round_trip():
    value = {
        "nan": float("nan"),
        "inf": float("inf"),
        "ninf": float("-inf"),
        "big": 2**53,
        "small": 2**53 - 1,
        "negbig": -(2**53) - 1,
        "i64": 2**63 - 1,
        "bytes": b"\x00\xff",
        "list": [1, 1.5, True, None, "s"],
        "nested": {"x": [float("nan")]},
    }
    text = dumps_json_safe(value)
    loaded = json.loads(text, parse_constant=lambda c: pytest.fail(f"{c} in stored JSON"))
    assert loaded["nan"] == "NaN" and loaded["inf"] == "Infinity" and loaded["ninf"] == "-Infinity"
    assert loaded["big"] == "9007199254740992" and loaded["small"] == 2**53 - 1
    assert loaded["negbig"] == "-9007199254740993" and loaded["i64"] == str(2**63 - 1)
    assert loaded["bytes"] == base64.b64encode(b"\x00\xff").decode()
    assert loaded["list"] == [1, 1.5, True, None, "s"]
    assert loaded["nested"] == {"x": ["NaN"]}
    assert json_safe(True) is True and json_safe(1) == 1


def test_canon_distinguishes_python_equal_values():
    assert canon_any(1) != canon_any(1.0) != canon_any(True)
    assert canon_any(0.0) != canon_any(-0.0)

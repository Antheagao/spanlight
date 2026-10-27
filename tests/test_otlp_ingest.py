"""POST /v1/traces: mapping, derived traces, errors, and encodings."""

import dataclasses
import gzip
import json
import sqlite3
import threading
import time
import zlib

import pytest
from fastapi.testclient import TestClient
from google.protobuf.json_format import MessageToDict
from google.rpc import status_pb2
from hypothesis import given, settings
from hypothesis import strategies as st
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status
from otlp_builders import docpilot_capture, key_values, otlp_json, single_span_requests

from spanlight.server import create_app, db
from spanlight.server.otlp import decode_protobuf, map_request
from spanlight.server.settings import DEFAULT_COST_ATTRIBUTES

PROTOBUF = {"content-type": "application/x-protobuf"}
JSON = {"content-type": "application/json"}
TRACE = bytes(range(1, 17))
T0 = 1_700_000_000 * 10**9


def pb_span(
    span_id: int = 1,
    *,
    name="span",
    parent: int | None = None,
    start=T0,
    end=T0 + 10**9,
    trace=TRACE,
    **attributes,
) -> Span:
    span = Span(
        trace_id=trace,
        span_id=span_id.to_bytes(8, "big"),
        name=name,
        start_time_unix_nano=start,
        end_time_unix_nano=end,
    )
    if parent is not None:
        span.parent_span_id = parent.to_bytes(8, "big")
    span.attributes.extend(key_values([(k, v) for k, v in attributes.items()]))
    return span


def export(*spans: Span, service: str | None = "svc") -> bytes:
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    if service is not None:
        resource_spans.resource.attributes.extend(key_values([("service.name", ("s", service))]))
    resource_spans.scope_spans.add().spans.extend(spans)
    return request.SerializeToString()


def llm(span_id=1, **attributes) -> Span:
    attributes = {"gen_ai.operation.name": ("s", "chat"), **attributes}
    return pb_span(span_id, name="chat m", **attributes)


def post(client, body, headers=PROTOBUF, **kwargs):
    return client.post("/v1/traces", content=body, headers=headers, **kwargs)


def spans_of(client, trace_id=None):
    trace_id = trace_id or TRACE.hex()
    return {s["id"]: s for s in client.get(f"/api/traces/{trace_id}").json()["spans"]}


def only_span(client, span_id=1):
    return spans_of(client)[span_id.to_bytes(8, "big").hex()]


def status_of(response) -> status_pb2.Status:
    return status_pb2.Status.FromString(response.content)


# -- doc-pilot's real exporter output -----------------------------------------


@pytest.fixture()
def docpilot(client):
    for body in docpilot_capture():
        response = post(client, body)
        assert (response.status_code, response.content) == (200, b"")
    (trace,) = client.get("/api/traces").json()["traces"]
    return client, client.get(f"/api/traces/{trace['id']}").json(), trace


def by_name(trace, prefix):
    return [s for s in trace["spans"] if s["name"].startswith(prefix)]


def test_docpilots_real_trace_becomes_one_derived_trace(docpilot):
    client, trace, summary = docpilot
    assert trace["name"] == "POST /ask"
    assert trace["source"] == "otlp" and trace["status"] == "ok"
    assert trace["services"] == ["doc-pilot-api", "doc-pilot-worker"]
    assert trace["session"] == "conv-7"  # from gen_ai.conversation.id
    root = [s for s in trace["spans"] if s["parent_id"] is None]
    assert [s["name"] for s in root] == ["POST /ask"]
    assert trace["root_span_id"] == root[0]["id"]
    assert trace["started_at"] == min(s["started_at"] for s in trace["spans"])
    assert trace["ended_at"] == max(s["ended_at"] for s in trace["spans"])
    # The worker's job span is a child of the API's agent span, across services.
    (job,) = by_name(trace, "job judge")
    (agent,) = by_name(trace, "invoke_agent")
    assert job["parent_id"] == agent["id"] and job["service"] == "doc-pilot-worker"
    assert summary["span_count"] == len(trace["spans"]) == 14


def test_span_kinds_follow_the_gen_ai_operation(docpilot):
    _, trace, _ = docpilot
    kinds = {s["name"].split()[0]: set() for s in trace["spans"]}
    for s in trace["spans"]:
        kinds[s["name"].split()[0]].add(s["kind"])
    assert kinds["chat"] == {"llm"}
    assert kinds["execute_tool"] == {"tool"}
    assert kinds["retrieval"] == {"retrieval"}
    for other in ("invoke_agent", "embeddings", "job", "evaluate", "POST", "select", "connect"):
        assert kinds[other] == {"other"}, other


def test_only_llm_spans_carry_model_tokens_and_cost(docpilot):
    _, trace, summary = docpilot
    chats = by_name(trace, "chat ")
    assert len(chats) == 3
    haiku = next(s for s in chats if s["name"] == "chat claude-haiku-4-5")
    assert haiku["model"] == "claude-haiku-4-5-20251001"  # gen_ai.response.model wins
    assert (haiku["input_tokens"], haiku["output_tokens"]) == (1500, 240)
    assert haiku["cost_usd"] == 0.0027
    assert haiku["otel"]["cost_source"] == "attribute:docpilot.cost_usd"
    scripted = [s for s in chats if s["model"] == "scripted"]
    assert len(scripted) == 2 and all(s["cost_usd"] == 0.0 for s in scripted)
    for other in trace["spans"]:
        if other["kind"] != "llm":
            assert (other["model"], other["input_tokens"], other["cost_usd"]) == (None, None, None)
    # invoke_agent carries the run's total in docpilot.cost_usd; counting it
    # as well as the chat spans would double the trace's cost.
    (agent,) = by_name(trace, "invoke_agent")
    assert agent["attributes"]["docpilot.cost_usd"] == 0.0027
    assert summary["cost_usd"] == pytest.approx(0.0027)
    assert summary["llm_calls"] == 3
    assert (summary["input_tokens"], summary["output_tokens"]) == (812 + 1500 + 300, 96 + 240 + 40)


def test_the_judges_evaluation_events_and_the_otlp_extras_are_kept(docpilot):
    _, trace, _ = docpilot
    (evaluation,) = by_name(trace, "evaluate agent_answer")
    assert [e["name"] for e in evaluation["events"]] == ["gen_ai.evaluation.result"] * 2
    assert evaluation["events"][0]["attributes"] == {
        "gen_ai.evaluation.name": "groundedness",
        "gen_ai.evaluation.score.value": 1.0,
        "gen_ai.evaluation.score.label": "pass",
    }
    assert evaluation["events"][1]["attributes"]["gen_ai.evaluation.score.label"] == "fail"
    root = next(s for s in trace["spans"] if s["parent_id"] is None)
    assert root["otel"]["span_kind"] == "SERVER"
    resource = trace["resources"][root["otel"]["resource"]]
    assert resource["attributes"]["service.name"] == "doc-pilot-api"
    assert trace["scopes"][root["otel"]["scope"]]["name"]
    # The worker's spans refer to the worker's resource, not the API's.
    (job,) = by_name(trace, "job judge")
    assert job["otel"]["resource"] != root["otel"]["resource"]
    assert len(trace["resources"]) == 2 and len(trace["scopes"]) >= 2
    assert root["attributes"]["http.method"] == "POST"
    assert root["links"] == []


def test_the_captured_requests_decode_without_rejects():
    for body in docpilot_capture():
        batch = map_request(decode_protobuf(body), DEFAULT_COST_ATTRIBUTES)
        assert batch.rejected == 0 and batch.spans


# -- arrival order and resends -------------------------------------------------


def dump(conn) -> dict:
    return {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]
        for table, order in (
            ("traces", "id"),
            ("spans", "trace_id, id"),
            ("resources", "trace_id, digest"),
            ("scopes", "trace_id, digest"),
        )
    }


def store(database, body):
    batch = map_request(decode_protobuf(body), DEFAULT_COST_ATTRIBUTES)
    with database.write() as conn:
        db.ingest_otlp(conn, batch.spans)


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    database = db.Database(tmp_path_factory.mktemp("orders") / "orders.db")
    yield database
    database.close()


def reset(database):
    with database.write() as conn:
        conn.execute("DELETE FROM traces")  # the spans cascade


@pytest.fixture(scope="module")
def singles():
    return single_span_requests(docpilot_capture())


@pytest.fixture(scope="module")
def in_one_batch(database, singles):
    reset(database)
    store(database, b"".join(singles))
    with database.read() as conn:
        return dump(conn)


@settings(max_examples=40)
@given(st.data())
def test_any_order_and_batching_of_the_spans_gives_identical_rows(
    database, singles, in_one_batch, data
):
    order = data.draw(st.permutations(range(len(singles))))
    cuts = sorted(data.draw(st.sets(st.integers(1, len(singles) - 1), max_size=5)))
    batches = [order[a:b] for a, b in zip([0, *cuts], [*cuts, len(order)], strict=True)]
    reset(database)
    for batch in batches:
        store(database, b"".join(singles[i] for i in batch))
    with database.read() as conn:
        assert dump(conn) == in_one_batch


def test_root_first_root_last_and_children_first_agree(database, singles, in_one_batch):
    root = next(i for i, body in enumerate(singles) if b"http.method" in body)
    others = [i for i in range(len(singles)) if i != root]
    for order in ([root, *others], [*others, root], [*reversed(others), root]):
        reset(database)
        for i in order:
            store(database, singles[i])
        with database.read() as conn:
            assert dump(conn) == in_one_batch


def test_the_trace_is_provisional_until_its_root_arrives(client, singles):
    root = next(i for i, body in enumerate(singles) if b"http.method" in body)
    child = next(i for i, body in enumerate(singles) if b"search_documents" in body and i != root)
    post(client, singles[child])
    (before,) = client.get("/api/traces").json()["traces"]
    assert before["root_span_id"] is None and before["name"] == "execute_tool search_documents"
    post(client, singles[root])
    (after,) = client.get("/api/traces").json()["traces"]
    assert after["name"] == "POST /ask" and after["root_span_id"] is not None
    assert after["started_at"] <= before["started_at"]


def test_resending_the_same_body_changes_nothing(client, database):
    body = b"".join(docpilot_capture())
    post(client, body)
    with client.app.state.db.read() as conn:
        first = dump(conn)
    again = post(client, body)
    with client.app.state.db.read() as conn:
        assert dump(conn) == first
    assert (again.status_code, again.content) == (200, b"")


def test_a_resend_with_a_changed_span_updates_it_in_place(client):
    post(client, export(pb_span(1, name="old")))
    post(client, export(pb_span(1, name="new")))
    assert only_span(client)["name"] == "new"
    assert len(spans_of(client)) == 1


def test_the_same_span_id_in_two_traces_stays_two_spans(client):
    other = bytes(range(101, 117))
    post(client, export(pb_span(1, name="a"), pb_span(1, name="b", trace=other)))
    assert spans_of(client)[(1).to_bytes(8, "big").hex()]["name"] == "a"
    assert spans_of(client, other.hex())[(1).to_bytes(8, "big").hex()]["name"] == "b"


def test_an_sdk_trace_is_never_rewritten_by_otlp(client):
    sdk_trace = {"id": TRACE.hex(), "name": "sdk-trace", "started_at": 5.0, "status": "error"}
    client.post("/api/ingest", json={"traces": [sdk_trace], "spans": []})
    post(client, export(pb_span(1, name="otlp root")))
    trace = client.get(f"/api/traces/{TRACE.hex()}").json()
    assert (trace["name"], trace["source"], trace["started_at"], trace["status"]) == (
        "sdk-trace",
        "sdk",
        5.0,
        "error",
    )
    assert len(trace["spans"]) == 1


def test_session_prefers_session_id_over_the_conversation_id(client):
    spans = [
        pb_span(1, **{"gen_ai.conversation.id": ("s", "conv")}),
        pb_span(2, parent=1, start=T0 + 5, **{"session.id": ("s", "sess")}),
    ]
    post(client, export(*spans))
    assert client.get("/api/traces").json()["traces"][0]["session"] == "sess"


def test_a_trace_without_a_root_is_named_for_its_earliest_span_and_errors_if_any_span_did(client):
    failed = pb_span(3, name="late", parent=99, start=T0 + 2)
    failed.status.code = Status.STATUS_CODE_ERROR
    post(client, export(failed, pb_span(2, name="early", parent=99, start=T0 + 1)))
    (trace,) = client.get("/api/traces").json()["traces"]
    assert (trace["name"], trace["status"], trace["root_span_id"]) == ("early", "error", None)


def test_the_roots_status_decides_the_traces_status(client):
    child = pb_span(2, parent=1)
    child.status.code = Status.STATUS_CODE_ERROR
    post(client, export(pb_span(1, name="root"), child))
    (trace,) = client.get("/api/traces").json()["traces"]
    assert trace["status"] == "ok"
    assert spans_of(client)[(2).to_bytes(8, "big").hex()]["status"] == "error"


# -- mapping --------------------------------------------------------------------


def test_llm_spans_use_the_response_model_then_the_request_model(client):
    post(client, export(llm(1, **{"gen_ai.request.model": ("s", "req")}),
                        llm(2, **{"gen_ai.request.model": ("s", "req"),
                                  "gen_ai.response.model": ("s", "resp")})))
    spans = spans_of(client)
    assert spans[(1).to_bytes(8, "big").hex()]["model"] == "req"
    assert spans[(2).to_bytes(8, "big").hex()]["model"] == "resp"


@pytest.mark.parametrize("operation", ["chat", "text_completion", "generate_content"])
def test_the_llm_operations(client, operation):
    post(client, export(pb_span(1, **{"gen_ai.operation.name": ("s", operation)})))
    assert only_span(client)["kind"] == "llm"


@pytest.mark.parametrize("operation", ["embeddings", "invoke_agent", "create_agent", "", "CHAT"])
def test_other_operations_are_not_llm(client, operation):
    post(client, export(pb_span(1, **{"gen_ai.operation.name": ("s", operation)})))
    assert only_span(client)["kind"] == "other"


def test_a_non_string_operation_is_ignored(client):
    post(client, export(pb_span(1, **{"gen_ai.operation.name": ("i", 3)})))
    assert only_span(client)["kind"] == "other"


def test_tokens_fall_back_to_the_deprecated_names(client):
    post(client, export(llm(1, **{"gen_ai.usage.prompt_tokens": ("i", 7),
                                  "gen_ai.usage.completion_tokens": ("i", 3)}),
                        llm(2, **{"gen_ai.usage.input_tokens": ("i", 5),
                                  "gen_ai.usage.prompt_tokens": ("i", 7),
                                  "gen_ai.usage.output_tokens": ("i", 2)})))
    spans = spans_of(client)
    one, two = (spans[i.to_bytes(8, "big").hex()] for i in (1, 2))
    assert (one["input_tokens"], one["output_tokens"]) == (7, 3)
    assert (two["input_tokens"], two["output_tokens"]) == (5, 2)


def test_bad_token_attributes_are_ignored(client):
    post(client, export(llm(1, **{"gen_ai.usage.input_tokens": ("i", -4),
                                  "gen_ai.usage.output_tokens": ("s", "9")})))
    span = only_span(client)
    assert (span["input_tokens"], span["output_tokens"]) == (None, None)


def cost_of(client, **attributes):
    post(client, export(llm(1, **attributes)))
    span = only_span(client)
    return span["cost_usd"], span["otel"].get("cost_source")


def test_cost_comes_from_the_applications_attribute_first(client):
    cost, source = cost_of(client, **{"docpilot.cost_usd": ("d", 0.5),
                                      "gen_ai.request.model": ("s", "claude-haiku-4-5"),
                                      "gen_ai.usage.input_tokens": ("i", 10**6),
                                      "gen_ai.usage.output_tokens": ("i", 0)})
    assert (cost, source) == (0.5, "attribute:docpilot.cost_usd")


def test_the_first_listed_cost_attribute_wins_and_bad_values_are_skipped(client):
    cost, source = cost_of(client, **{"spanlight.cost_usd": ("d", float("nan")),
                                      "docpilot.cost_usd": ("i", 2)})
    assert (cost, source) == (2.0, "attribute:docpilot.cost_usd")
    cost, source = cost_of(client, **{"spanlight.cost_usd": ("d", 1.5),
                                      "docpilot.cost_usd": ("d", 2.0)})
    assert (cost, source) == (1.5, "attribute:spanlight.cost_usd")
    for bad in (("d", -1.0), ("b", True), ("s", "1.0"), ("d", float("inf"))):
        assert cost_of(client, **{"spanlight.cost_usd": bad})[0] is None


def test_zero_is_a_real_cost(client):
    cost = cost_of(client, **{"docpilot.cost_usd": ("d", 0.0)})
    assert cost == (0.0, "attribute:docpilot.cost_usd")


def test_the_cost_attributes_are_configurable(tmp_path, settings):
    custom = dataclasses.replace(settings, cost_attributes=("my.cost",))
    with TestClient(create_app(db_path=str(tmp_path / "c.db"), settings=custom)) as client:
        assert cost_of(client, **{"my.cost": ("d", 0.25), "docpilot.cost_usd": ("d", 9.0)}) == (
            0.25,
            "attribute:my.cost",
        )


def test_without_a_cost_attribute_the_price_table_prices_known_models(client):
    usage = {"gen_ai.usage.input_tokens": ("i", 1_000_000), "gen_ai.usage.output_tokens": ("i", 0)}
    assert cost_of(client, **usage, **{"gen_ai.request.model": ("s", "claude-haiku-4-5")}) == (
        1.0,
        "price_table",
    )


def test_a_sibling_model_is_not_priced_as_its_prefix(client):
    usage = {"gen_ai.usage.input_tokens": ("i", 1_000_000), "gen_ai.usage.output_tokens": ("i", 0)}
    cost, source = cost_of(client, **usage, **{"gen_ai.request.model": ("s", "claude-sonnet-5-5")})
    assert (cost, source) == (None, None)


def test_a_dated_model_id_is_priced_as_its_base(client):
    usage = {"gen_ai.usage.input_tokens": ("i", 1_000_000), "gen_ai.usage.output_tokens": ("i", 0)}
    attrs = {**usage, "gen_ai.response.model": ("s", "claude-haiku-4-5-20251001")}
    assert cost_of(client, **attrs) == (1.0, "price_table")


def test_a_price_needs_both_token_counts(client):
    assert cost_of(client, **{"gen_ai.request.model": ("s", "claude-haiku-4-5"),
                              "gen_ai.usage.input_tokens": ("i", 10)}) == (None, None)


def test_message_content_is_stored_only_when_the_instrumentation_sends_it(client):
    messages = '[{"role":"user","parts":[{"type":"text","content":"hi"}]}]'
    post(client, export(llm(1, **{"gen_ai.input.messages": ("s", messages),
                                  "gen_ai.output.messages": ("a", [("s", "structured")])}),
                        llm(2)))
    spans = spans_of(client)
    with_content, without = (spans[i.to_bytes(8, "big").hex()] for i in (1, 2))
    assert with_content["input"] == messages
    assert json.loads(with_content["output"]) == ["structured"]
    assert "gen_ai.input.messages" not in with_content["attributes"]
    assert (without["input"], without["output"]) == (None, None)


def test_times_are_float_seconds_and_an_unset_end_is_null(client):
    post(client, export(pb_span(1, start=1_700_000_000_123_456_789, end=1_700_000_001_000_000_000),
                        pb_span(2, start=T0, end=0),
                        pb_span(3, start=T0 + 10**9, end=T0)))
    spans = spans_of(client)
    one, two, three = (spans[i.to_bytes(8, "big").hex()] for i in (1, 2, 3))
    assert one["started_at"] == pytest.approx(1_700_000_000.123456789, abs=1e-6)
    assert one["ended_at"] == pytest.approx(1_700_000_001.0)
    assert two["ended_at"] is None
    assert three["ended_at"] == three["started_at"]  # an end before the start is clamped


def test_error_status_uses_the_status_message(client):
    failed = pb_span(1)
    failed.status.code, failed.status.message = Status.STATUS_CODE_ERROR, "it broke"
    failed.events.add(name="exception", attributes=key_values([("exception.type", ("s", "X"))]))
    post(client, export(failed))
    span = only_span(client)
    assert (span["status"], span["error"]) == ("error", "it broke")


def test_error_status_without_a_message_uses_the_last_exception_event(client):
    failed = pb_span(1)
    failed.status.code = Status.STATUS_CODE_ERROR
    for kind, message in (("First", "no"), ("ValueError", "bad input")):
        failed.events.add(name="exception", attributes=key_values(
            [("exception.type", ("s", kind)), ("exception.message", ("s", message))]))
    failed.events.add(name="other", attributes=key_values([("exception.type", ("s", "Nope"))]))
    post(client, export(failed))
    span = only_span(client)
    assert (span["status"], span["error"]) == ("error", "ValueError: bad input")


def test_ok_unset_and_exception_events_do_not_make_an_error(client):
    span = pb_span(1)
    span.events.add(name="exception", attributes=key_values([("exception.type", ("s", "X"))]))
    post(client, export(span))
    assert (only_span(client)["status"], only_span(client)["error"]) == ("ok", None)


def test_service_events_links_and_other_otlp_fields_are_kept(client):
    span = pb_span(1, **{"k": ("s", "v")})
    span.kind, span.trace_state, span.flags = Span.SPAN_KIND_CLIENT, "a=b", 257
    span.dropped_attributes_count = 4
    span.events.add(time_unix_nano=T0 + 5 * 10**8, name="ev",
                    attributes=key_values([("e", ("i", 1))]), dropped_attributes_count=2)
    span.links.add(trace_id=b"\x07" * 16, span_id=b"\x08" * 8, trace_state="x=y",
                   attributes=key_values([("l", ("b", True))]), flags=3)
    post(client, export(span, service="the-service"))
    stored = only_span(client)
    assert stored["service"] == "the-service"
    assert stored["attributes"] == {"k": "v"}
    assert stored["events"] == [{"time": pytest.approx(1_700_000_000.5), "name": "ev",
                                 "attributes": {"e": 1}, "dropped_attributes_count": 2}]
    assert stored["links"] == [{"trace_id": "07" * 16, "span_id": "08" * 8, "trace_state": "x=y",
                                "attributes": {"l": True}, "dropped_attributes_count": 0,
                                "flags": 3}]
    otel = stored["otel"]
    assert (otel["span_kind"], otel["trace_state"], otel["flags"]) == ("CLIENT", "a=b", 257)
    assert otel["dropped_attributes_count"] == 4


def test_a_resource_without_a_service_name_leaves_the_service_empty(client):
    post(client, export(pb_span(1), service=None))
    assert only_span(client)["service"] is None
    assert client.get("/api/traces").json()["traces"][0]["services"] == []


def test_limits_and_json_safe_storage(client):
    huge = "x" * 40_000
    span = pb_span(
        1,
        **{f"k{i}": ("i", i) for i in range(200)},
    )
    span.attributes.extend(key_values([
        ("long", ("s", huge)), ("nan", ("d", float("nan"))), ("inf", ("d", float("-inf"))),
        ("big", ("i", 2**62)), ("small", ("i", 2**53 - 1)), ("raw", ("y", b"\x00\xff")),
    ]))
    post(client, export(span))
    stored = only_span(client)
    attributes = stored["attributes"]
    assert len(attributes) == 128
    assert stored["otel"]["dropped_attributes_count"] == 206 - 128
    # The 128 attributes kept are the first 128, so the later ones were dropped.
    assert "long" not in attributes and "k0" in attributes
    # Stored text parses with a strict parser, whatever the browser would do.
    with client.app.state.db.read() as conn:
        raw = conn.execute("SELECT attributes, otel FROM spans").fetchone()
    for text in raw:
        json.loads(text, parse_constant=lambda c: pytest.fail(f"{c} stored"))
    for token in ("NaN", "Infinity"):
        assert f": {token}" not in raw[0] and f":{token}" not in raw[0]


def test_values_over_the_limits_are_cut_and_converted(client):
    span = pb_span(1)
    span.attributes.extend(key_values([
        ("long", ("s", "x" * 40_000)), ("nan", ("d", float("nan"))), ("ninf", ("d", float("-inf"))),
        ("big", ("i", 2**62)), ("small", ("i", 2**53 - 1)), ("raw", ("y", b"\x00\xff")),
    ]))
    post(client, export(span))
    attributes = only_span(client)["attributes"]
    assert attributes["long"] == "x" * 32768 + "[truncated]"
    assert attributes["nan"] == "NaN" and attributes["ninf"] == "-Infinity"
    assert attributes["big"] == str(2**62) and attributes["small"] == 2**53 - 1
    assert attributes["raw"] == "AP8="


# -- the HTTP protocol ------------------------------------------------------------


def test_an_accepted_export_is_an_empty_200_in_the_requests_encoding(client):
    r = post(client, export(pb_span(1)))
    assert (r.status_code, r.content, r.headers["content-type"]) == (
        200, b"", "application/x-protobuf")
    r = post(client, json.dumps(otlp_json(ExportTraceServiceRequest.FromString(
        export(pb_span(2))))), headers=JSON)
    assert (r.status_code, r.json(), r.headers["content-type"]) == (200, {}, "application/json")
    assert set(spans_of(client)) == {(1).to_bytes(8, "big").hex(), (2).to_bytes(8, "big").hex()}


def test_an_empty_export_is_accepted(client):
    assert post(client, b"").status_code == 200
    assert post(client, b"{}", headers=JSON).json() == {}
    assert client.get("/api/traces").json()["traces"] == []


def test_invalid_spans_are_a_partial_success_and_the_rest_are_stored(client):
    bad_span_id = pb_span(2)
    bad_span_id.span_id = b"1234567"
    zero_trace = pb_span(3, trace=b"\x00" * 16)
    r = post(client, export(pb_span(1), bad_span_id, zero_trace))
    assert r.status_code == 200
    partial = ExportTraceServiceResponse.FromString(r.content).partial_success
    assert partial.rejected_spans == 2
    assert "span_id must be 8 bytes" in partial.error_message
    assert "trace_id must not be all zeros" in partial.error_message
    assert set(spans_of(client)) == {(1).to_bytes(8, "big").hex()}


def test_partial_success_in_json(client):
    body = json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [
        {"traceId": "00" * 16, "spanId": "01" * 8, "startTimeUnixNano": "1"}]}]}]})
    r = post(client, body, headers=JSON)
    assert r.status_code == 200
    assert r.json()["partialSuccess"]["rejectedSpans"] == "1"
    assert "all zeros" in r.json()["partialSuccess"]["errorMessage"]


def test_json_requests_are_decoded_and_stored(client):
    document = otlp_json(ExportTraceServiceRequest.FromString(export(llm(1), pb_span(2, parent=1))))
    assert post(client, json.dumps(document), headers=JSON).status_code == 200
    assert {s["kind"] for s in spans_of(client).values()} == {"llm", "other"}


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "identity", "GZIP"])
def test_compressed_bodies_are_accepted(client, encoding):
    body = export(pb_span(1))
    compress = {"gzip": gzip.compress, "deflate": zlib.compress}.get(encoding.lower(), bytes)
    r = post(client, compress(body), headers={**PROTOBUF, "content-encoding": encoding})
    assert (r.status_code, r.content) == (200, b"")
    assert len(spans_of(client)) == 1


@pytest.mark.parametrize(
    "body",
    [b"not gzip at all", gzip.compress(export(pb_span(1)))[:-5], gzip.compress(b"x")[:10], b"",
     gzip.compress(export(pb_span(1))) + b"trailing"],
)
def test_a_bad_or_truncated_gzip_stream_is_400(client, body):
    r = post(client, body, headers={**PROTOBUF, "content-encoding": "gzip"})
    assert r.status_code == 400
    assert status_of(r).code == 3


def test_an_unsupported_content_encoding_is_415(client):
    for encoding in ("br", "zstd", "gzip, deflate", "compress"):
        r = post(client, export(pb_span(1)), headers={**PROTOBUF, "content-encoding": encoding})
        assert r.status_code == 415, encoding


def test_a_small_gzip_that_expands_past_the_cap_is_413(tmp_path, settings):
    tiny = dataclasses.replace(settings, max_request_bytes=100_000)
    bomb = gzip.compress(b"\x00" * 50_000_000)
    assert len(bomb) < 100_000
    with TestClient(create_app(db_path=str(tmp_path / "b.db"), settings=tiny)) as client:
        started = time.monotonic()
        r = post(client, bomb, headers={**PROTOBUF, "content-encoding": "gzip"})
        assert r.status_code == 413 and status_of(r).code == 3
        assert time.monotonic() - started < 2
        # Exactly the cap decompressed is fine.
        gz = {**PROTOBUF, "content-encoding": "gzip"}
        at_cap = post(client, gzip.compress(b"\x00" * 100_000), headers=gz)
        assert at_cap.status_code == 400  # allowed in size; zeros are not a protobuf message
        over = post(client, gzip.compress(b"\x00" * 100_001), headers=gz)
        assert over.status_code == 413


def test_a_malformed_protobuf_or_json_body_is_400_with_a_status(client):
    for body, headers in ((b"\xff\xff\xff", PROTOBUF), (b"{nope", JSON), (b"[]", JSON)):
        r = post(client, body, headers=headers)
        assert r.status_code == 400
        if headers is PROTOBUF:
            assert status_of(r).code == 3 and status_of(r).message
        else:
            assert r.json()["code"] == 3 and r.json()["message"]
    assert client.get("/api/traces").json()["traces"] == []


def test_errors_on_v1_are_a_google_rpc_status_in_the_requests_encoding(client):
    wrong_type = post(client, b"x", headers={"content-type": "text/plain"})
    assert wrong_type.status_code == 415
    assert wrong_type.headers["content-type"] == "application/json"
    assert wrong_type.json() == {"code": 3, "message": wrong_type.json()["message"]}
    assert "error" not in wrong_type.json()
    foreign = post(client, export(pb_span(1)), headers={**PROTOBUF, "host": "evil.example"})
    assert foreign.status_code == 400
    assert foreign.headers["content-type"] == "application/x-protobuf"
    assert status_of(foreign).code == 3
    cross_site = post(client, export(pb_span(1)), headers={**PROTOBUF, "origin": "https://evil.example"})
    assert (cross_site.status_code, status_of(cross_site).code) == (403, 7)
    assert client.get("/api/traces").json()["traces"] == []


def test_metrics_logs_and_other_paths_are_404_statuses(client):
    for path in ("/v1/metrics", "/v1/logs"):
        r = client.post(path, content=b"", headers=PROTOBUF)
        assert (r.status_code, status_of(r).code) == (404, 5)
        assert status_of(r).message == "spanlight accepts traces only"
        r = client.post(path, content=b"{}", headers=JSON)
        assert (r.status_code, r.json()["code"]) == (404, 5)
    r = client.post("/v1/profiles", content=b"{}", headers=JSON)
    assert (r.status_code, r.json()["code"]) == (404, 5)
    r = client.get("/v1/traces")
    assert (r.status_code, r.json()["code"]) == (405, 12)
    assert r.headers["allow"] == "POST"


def test_the_guard_applies_to_v1_with_a_token(tmp_path, settings):
    token = "t" * 40
    locked = dataclasses.replace(settings, ingest_token=token)
    with TestClient(create_app(db_path=str(tmp_path / "t.db"), settings=locked)) as client:
        for headers in (PROTOBUF, {**PROTOBUF, "authorization": "Bearer wrong" + "x" * 30}):
            r = post(client, export(pb_span(1)), headers=headers)
            assert r.status_code == 401
            assert r.headers["www-authenticate"] == "Bearer"
            assert status_of(r).code == 16
            assert token not in r.text
        good = {**PROTOBUF, "authorization": f"Bearer {token}"}
        assert post(client, export(pb_span(1)), headers=good).status_code == 200


def test_the_size_cap_is_a_413_status(tmp_path, settings):
    tiny = dataclasses.replace(settings, max_request_bytes=500)
    with TestClient(create_app(db_path=str(tmp_path / "s.db"), settings=tiny)) as client:
        r = post(client, b"\x00" * 501)
        assert (r.status_code, status_of(r).code) == (413, 3)


def test_a_busy_database_is_a_retryable_503_status(tmp_path, settings):
    path = tmp_path / "busy.db"
    app = create_app(db_path=str(path), settings=settings, busy_timeout_ms=50)
    with TestClient(app) as client:
        other = sqlite3.connect(path, isolation_level=None)
        other.execute("BEGIN IMMEDIATE")
        busy = post(client, export(pb_span(1)))
        json_busy = post(client, b"{}", headers=JSON)
        other.execute("COMMIT")
        other.close()
        retried = post(client, export(pb_span(1)))
    assert busy.status_code == 503 and busy.headers["retry-after"] == "1"
    assert status_of(busy).code == 14
    assert json_busy.status_code == 200  # an empty export never writes
    assert retried.status_code == 200


def test_a_busy_thread_holding_the_lock_is_a_503_too(tmp_path, settings):
    app = create_app(db_path=str(tmp_path / "w.db"), settings=settings, busy_timeout_ms=100)
    with TestClient(app) as client:
        holding, release = threading.Event(), threading.Event()

        def hold():
            with app.state.db.write():
                holding.set()
                release.wait(10)

        thread = threading.Thread(target=hold)
        thread.start()
        assert holding.wait(10)
        r = post(client, export(pb_span(1)))
        release.set()
        thread.join(10)
    assert (r.status_code, r.headers["retry-after"]) == (503, "1")


def test_the_native_ingest_is_unchanged(client):
    r = client.post("/api/ingest", json={"traces": [{"id": "t", "name": "n", "started_at": 1.0}],
                                         "spans": []})
    assert r.status_code == 202
    assert client.get("/api/traces/t").json()["source"] == "sdk"


def test_reference_json_of_a_message_round_trips_through_the_endpoint(client):
    body = export(llm(1, **{"docpilot.cost_usd": ("d", 1.0)}))
    message = ExportTraceServiceRequest.FromString(body)
    assert MessageToDict(message)  # the oracle can render it
    assert post(client, json.dumps(otlp_json(message)), headers=JSON).status_code == 200
    assert only_span(client)["cost_usd"] == 1.0


@settings(max_examples=40)
@given(st.data())
def test_ties_and_several_roots_cannot_make_the_trace_depend_on_arrival_order(database, data):
    # Equal start times and several parentless spans: only the (started_at, id)
    # tiebreaks keep the derived trace the same whichever span arrives first.
    count = data.draw(st.integers(2, 6))
    spans = [
        pb_span(
            i + 1,
            name=f"span-{i}",
            parent=data.draw(st.sampled_from([None, None, 1, 2])) if i > 1 else None,
            start=T0 + data.draw(st.sampled_from([0, 0, 1])),
            **{"session.id": ("s", f"s{i}")},
        )
        for i in range(count)
    ]
    for i in range(count):
        spans[i].status.code = data.draw(st.sampled_from([0, 2]))
    reset(database)
    store(database, export(*spans))
    with database.read() as conn:
        expected = dump(conn)
    order = data.draw(st.permutations(range(count)))
    reset(database)
    for i in order:
        store(database, export(spans[i]))
    with database.read() as conn:
        assert dump(conn) == expected


def test_a_large_resource_is_stored_and_returned_once_not_once_per_span(client):
    resource = [(f"attr{i}", ("s", "x" * 30_000)) for i in range(8)]  # about 240 KB
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    resource_spans.resource.attributes.extend(key_values(resource))
    resource_spans.scope_spans.add().spans.extend(pb_span(i + 1, start=T0 + i) for i in range(200))
    body = request.SerializeToString()
    assert 200_000 < len(body) < 300_000

    assert post(client, body).status_code == 200

    with client.app.state.db.read() as conn:
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        size = pages * conn.execute("PRAGMA page_size").fetchone()[0]
        spans = conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
        otel_bytes = conn.execute("SELECT SUM(LENGTH(otel)) FROM spans").fetchone()[0]
    assert spans == 200
    assert size < 700_000, f"the resource was stored per span: {size} bytes"
    assert otel_bytes < 200 * 1_000
    response = client.get(f"/api/traces/{TRACE.hex()}")
    assert len(response.content) < 600_000
    trace = response.json()
    (resource_row,) = trace["resources"].values()
    assert len(resource_row["attributes"]) == 8
    assert {s["otel"]["resource"] for s in trace["spans"]} == set(trace["resources"])


def test_resources_are_per_trace_and_follow_the_spans_on_a_resend(client):
    post(client, export(pb_span(1), service="old"))
    post(client, export(pb_span(1), service="old"))
    trace = client.get(f"/api/traces/{TRACE.hex()}").json()
    assert len(trace["resources"]) == 1 and len(trace["scopes"]) == 1
    # The span is resent from a differently described resource: the old row goes.
    post(client, export(pb_span(1), service="new"))
    trace = client.get(f"/api/traces/{TRACE.hex()}").json()
    (resource,) = trace["resources"].values()
    assert resource["attributes"] == {"service.name": "new"}
    assert only_span(client)["service"] == "new"
    # The same resource in another trace is that trace's own row.
    other = bytes(range(101, 117))
    post(client, export(pb_span(1, trace=other), service="new"))
    again = client.get(f"/api/traces/{other.hex()}").json()
    assert set(again["resources"]) == set(trace["resources"])


def test_deleting_a_trace_deletes_its_resources(client):
    post(client, export(pb_span(1)))
    with client.app.state.db.write() as conn:
        conn.execute("DELETE FROM traces")
        assert conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM scopes").fetchone()[0] == 0


def test_a_span_name_is_cut_to_the_native_limit(client):
    post(client, export(pb_span(1, name="n" * 600), pb_span(2, name="é" * 512)))
    spans = spans_of(client)
    assert spans[(1).to_bytes(8, "big").hex()]["name"] == "n" * 512
    assert spans[(2).to_bytes(8, "big").hex()]["name"] == "é" * 512


def test_whole_number_token_counts_may_be_floats(client):
    post(client, export(llm(1, **{"gen_ai.usage.input_tokens": ("d", 12.0),
                                  "gen_ai.usage.output_tokens": ("d", 3.0)}),
                        llm(2, **{"gen_ai.usage.input_tokens": ("d", 12.5),
                                  "gen_ai.usage.output_tokens": ("d", -1.0)}),
                        llm(3, **{"gen_ai.usage.input_tokens": ("d", float("nan")),
                                  "gen_ai.usage.output_tokens": ("d", float("inf"))})))
    spans = spans_of(client)
    got = [(spans[i.to_bytes(8, "big").hex()]["input_tokens"],
            spans[i.to_bytes(8, "big").hex()]["output_tokens"]) for i in (1, 2, 3)]
    assert got == [(12, 3), (None, None), (None, None)]
    assert all(isinstance(spans[(1).to_bytes(8, "big").hex()][k], int)
               for k in ("input_tokens", "output_tokens"))


def test_an_export_of_only_rejected_spans_is_still_a_200_with_partial_success(client):
    bad = [pb_span(1, trace=b"\x00" * 16), pb_span(2, trace=b"\x01" * 5)]
    r = post(client, export(*bad))
    assert r.status_code == 200
    partial = ExportTraceServiceResponse.FromString(r.content).partial_success
    assert partial.rejected_spans == 2 and partial.error_message
    assert client.get("/api/traces").json()["traces"] == []


def test_x_gzip_is_gzip_and_raw_deflate_or_concatenated_members_are_refused(client):
    body = export(pb_span(1))
    ok = post(client, gzip.compress(body), headers={**PROTOBUF, "content-encoding": "x-gzip"})
    assert ok.status_code == 200
    raw = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    raw_deflate = raw.compress(body) + raw.flush()
    for encoding, data in (
        ("deflate", raw_deflate),
        ("gzip", gzip.compress(body) + gzip.compress(body)),
    ):
        r = post(client, data, headers={**PROTOBUF, "content-encoding": encoding})
        assert r.status_code == 400, encoding

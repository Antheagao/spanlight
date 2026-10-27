"""The real OpenTelemetry SDK exporter against a real server on a real port.

This is the test that proves the pieces fit as clients use them: the exporter's
own protobuf encoding, headers, gzip and retry behavior, through uvicorn.
"""

import dataclasses
import json
import sqlite3
import threading
import time
import urllib.request

import pytest
import uvicorn
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode

from spanlight.server import create_app

TOKEN = "e2e-" + "t" * 40


class Server:
    """spanlight under uvicorn on an ephemeral loopback port."""

    def __init__(self, app):
        self.config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "the server did not start"
            time.sleep(0.01)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(10)

    def get(self, path):
        with urllib.request.urlopen(self.url + path, timeout=10) as response:
            return json.load(response)


@pytest.fixture()
def server(tmp_path, settings):
    app = create_app(db_path=str(tmp_path / "e2e.db"), settings=settings)
    with Server(app) as running:
        yield running


def emit_ask_tree(tracer) -> None:
    """A doc-pilot-like trace: a request, an agent run, two model calls, a tool."""
    with tracer.start_as_current_span("POST /ask", kind=SpanKind.SERVER) as root:
        root.set_attribute("http.method", "POST")
        with tracer.start_as_current_span("invoke_agent ask") as agent:
            agent.set_attribute("gen_ai.operation.name", "invoke_agent")
            agent.set_attribute("gen_ai.conversation.id", "conv-1")
            agent.set_attribute("docpilot.cost_usd", 0.0042)
            chat_span = tracer.start_as_current_span("chat claude-haiku-4-5", kind=SpanKind.CLIENT)
            with chat_span as chat:
                for key, value in {
                    "gen_ai.operation.name": "chat",
                    "gen_ai.request.model": "claude-haiku-4-5",
                    "gen_ai.response.model": "claude-haiku-4-5-20251001",
                    "gen_ai.usage.input_tokens": 1200,
                    "gen_ai.usage.output_tokens": 80,
                    "gen_ai.response.finish_reasons": ["end_turn"],
                    "docpilot.cost_usd": 0.0042,
                }.items():
                    chat.set_attribute(key, value)
            with tracer.start_as_current_span("execute_tool search") as tool:
                tool.set_attribute("gen_ai.operation.name", "execute_tool")
                tool.set_status(Status(StatusCode.ERROR, "tool failed"))
                tool.record_exception(ValueError("no results"))


def exporter_for(server, **options):
    provider = TracerProvider(resource=Resource.create({"service.name": "e2e-svc"}))
    exporter = OTLPSpanExporter(endpoint=f"{server.url}/v1/traces", **options)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider


def assert_ask_tree_arrived(server):
    (summary,) = server.get("/api/traces")["traces"]
    trace = server.get(f"/api/traces/{summary['id']}")
    assert trace["name"] == "POST /ask" and trace["source"] == "otlp"
    assert trace["services"] == ["e2e-svc"] and trace["session"] == "conv-1"
    assert trace["status"] == "ok"
    spans = {s["name"]: s for s in trace["spans"]}
    assert set(spans) == {"POST /ask", "invoke_agent ask", "chat claude-haiku-4-5",
                          "execute_tool search"}
    chat = spans["chat claude-haiku-4-5"]
    assert (chat["kind"], chat["model"]) == ("llm", "claude-haiku-4-5-20251001")
    assert (chat["input_tokens"], chat["output_tokens"], chat["cost_usd"]) == (1200, 80, 0.0042)
    assert chat["attributes"]["gen_ai.response.finish_reasons"] == ["end_turn"]
    assert spans["invoke_agent ask"]["cost_usd"] is None  # the run total is not double counted
    tool = spans["execute_tool search"]
    assert (tool["kind"], tool["status"], tool["error"]) == ("tool", "error", "tool failed")
    assert tool["events"][0]["name"] == "exception"
    assert summary["cost_usd"] == pytest.approx(0.0042)
    assert spans["POST /ask"]["otel"]["span_kind"] == "SERVER"
    assert spans["POST /ask"]["parent_id"] is None
    assert spans["chat claude-haiku-4-5"]["parent_id"] == spans["invoke_agent ask"]["id"]


@pytest.mark.parametrize("compression", [None, Compression.Gzip, Compression.Deflate])
def test_the_real_exporter_delivers_a_trace(server, compression):
    provider = exporter_for(server, compression=compression)
    emit_ask_tree(provider.get_tracer("e2e"))
    assert provider.force_flush()
    provider.shutdown()
    assert_ask_tree_arrived(server)


def test_the_exporter_sends_the_bearer_token_when_the_server_requires_one(tmp_path, settings):
    locked = dataclasses.replace(settings, ingest_token=TOKEN)
    with Server(create_app(db_path=str(tmp_path / "t.db"), settings=locked)) as server:
        refused = exporter_for(server, timeout=2)
        emit_ask_tree(refused.get_tracer("e2e"))
        refused.shutdown()
        assert server.get("/api/traces")["traces"] == []

        provider = exporter_for(server, headers={"authorization": f"Bearer {TOKEN}"})
        emit_ask_tree(provider.get_tracer("e2e"))
        assert provider.force_flush()
        provider.shutdown()
        assert_ask_tree_arrived(server)


def test_the_exporter_retries_a_503_and_honors_retry_after(tmp_path, settings):
    path = tmp_path / "retry.db"
    app = create_app(db_path=str(path), settings=settings, busy_timeout_ms=50)
    with Server(app) as server:
        other = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        other.execute("BEGIN IMMEDIATE")
        threading.Timer(0.4, lambda: (other.execute("COMMIT"), other.close())).start()
        provider = exporter_for(server, timeout=15)
        started = time.monotonic()
        emit_ask_tree(provider.get_tracer("e2e"))
        provider.shutdown()
        elapsed = time.monotonic() - started
        # The first attempts met a busy database; the exporter waited and tried again.
        assert elapsed >= 0.9, "the exporter did not back off after the 503"
        assert_ask_tree_arrived(server)

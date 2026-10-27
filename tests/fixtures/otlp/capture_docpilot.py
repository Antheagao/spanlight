"""Regenerate `docpilot_capture.json`: what doc-pilot's real OTLP exporter sends.

Run it with doc-pilot's own virtualenv, so the spans come from doc-pilot's
own telemetry helpers and the SDK exporter, not from a copy of them:

    cd ~/doc-pilot/backend
    .venv/bin/python ~/spanlight/tests/fixtures/otlp/capture_docpilot.py api    OUT_DIR
    .venv/bin/python ~/spanlight/tests/fixtures/otlp/capture_docpilot.py worker OUT_DIR
    .venv/bin/python ~/spanlight/tests/fixtures/otlp/capture_docpilot.py merge  OUT_DIR OUT.json

Nothing is started and no model is called: the exporter posts to a throwaway
HTTP server in the same process, the "model" responses are stand-ins, and the
API run puts a one-route FastAPI app under FastAPIInstrumentor. The worker run
continues the API run's trace through a stored W3C traceparent, the way
doc-pilot's job queue does.
"""

import base64
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

mode, out_dir = sys.argv[1], Path(sys.argv[2])
out_dir.mkdir(parents=True, exist_ok=True)

if mode == "merge":
    requests = []
    for service in ("api", "worker"):
        for file in sorted(out_dir.glob(f"{service}-*.bin")):
            requests.append(
                {
                    "service": f"doc-pilot-{service}",
                    "content_type": "application/x-protobuf",
                    "body_base64": base64.b64encode(file.read_bytes()).decode(),
                }
            )
    Path(sys.argv[3]).write_text(json.dumps({"requests": requests}, indent=2) + "\n")
    raise SystemExit(0)

received = []


class Capture(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["content-length"]))
        (out_dir / f"{mode}-{len(received)}.bin").write_bytes(body)
        received.append(body)
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


server = HTTPServer(("127.0.0.1", 0), Capture)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://127.0.0.1:{server.server_port}"
os.environ["OTEL_SERVICE_NAME"] = f"doc-pilot-{mode}"

sys.path.insert(0, os.getcwd())  # doc-pilot's backend directory
from app import telemetry  # noqa: E402
from app.agent.loop import AGENT_NAME  # noqa: E402

assert telemetry.configure_tracing(f"doc-pilot-{mode}")
tracer = telemetry.tracer
Kind = telemetry.SpanKind


def response(model, input_tokens, output_tokens):
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return SimpleNamespace(id=f"msg_{model}", model=model, stop_reason="end_turn", usage=usage)


def run_api():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite://")
    # skip_dep_check, as doc-pilot's instrument_engine does
    SQLAlchemyInstrumentor().instrument(engine=engine, skip_dep_check=True)
    app = FastAPI()

    @app.post("/ask")
    def ask():
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        with tracer().start_as_current_span(
            f"invoke_agent {AGENT_NAME}",
            kind=Kind.INTERNAL,
            attributes={
                telemetry.GEN_AI_OPERATION_NAME: "invoke_agent",
                telemetry.GEN_AI_PROVIDER_NAME: "scripted",
                telemetry.GEN_AI_AGENT_NAME: AGENT_NAME,
                telemetry.GEN_AI_REQUEST_MODEL: "scripted",
            },
        ) as agent:
            agent.set_attribute(telemetry.GEN_AI_CONVERSATION_ID, "conv-7")
            (out_dir / "traceparent.txt").write_text(telemetry.current_traceparent())
            with telemetry.model_call_span(
                "scripted", max_tokens=1024, prompt_version="agent-v3"
            ) as span:
                telemetry.record_model_response(span, response("scripted", 812, 96), 0.0)
            with tracer().start_as_current_span(
                "execute_tool search_documents",
                attributes={
                    telemetry.GEN_AI_OPERATION_NAME: "execute_tool",
                    telemetry.GEN_AI_TOOL_NAME: "search_documents",
                    telemetry.GEN_AI_TOOL_CALL_ID: "toolu_01",
                },
            ), tracer().start_as_current_span(
                "retrieval document_chunks",
                attributes={
                    telemetry.GEN_AI_OPERATION_NAME: "retrieval",
                    telemetry.GEN_AI_DATA_SOURCE_ID: "document_chunks",
                    "docpilot.search.mode": "hybrid",
                    "docpilot.search.k": 8,
                    "docpilot.search.results": 5,
                },
            ):
                pass
            with tracer().start_as_current_span(
                "embeddings hashing",
                attributes={
                    telemetry.GEN_AI_OPERATION_NAME: "embeddings",
                    telemetry.GEN_AI_REQUEST_MODEL: "hashing",
                    "docpilot.chunk_count": 3,
                },
            ):
                pass
            with telemetry.model_call_span(
                "claude-haiku-4-5", max_tokens=1024, prompt_version="agent-v3"
            ) as span:
                telemetry.record_model_response(
                    span, response("claude-haiku-4-5-20251001", 1500, 240), 0.0027
                )
            agent.set_attribute("docpilot.agent.status", "answered")
            agent.set_attribute("docpilot.agent.steps", 2)
            agent.set_attribute(telemetry.DOCPILOT_COST_USD, 0.0027)
        return {"ok": True}

    FastAPIInstrumentor.instrument_app(app)
    with TestClient(app) as client:
        assert client.post("/ask").status_code == 200


def run_worker():
    traceparent = (out_dir / "traceparent.txt").read_text()
    with tracer().start_as_current_span(
        "job judge",
        context=telemetry.context_from_traceparent(traceparent),
        kind=Kind.CONSUMER,
        attributes={
            telemetry.DOCPILOT_JOB_ID: "11111111-2222-3333-4444-555555555555",
            telemetry.DOCPILOT_JOB_KIND: "judge",
            telemetry.DOCPILOT_JOB_ATTEMPT: 1,
            telemetry.DOCPILOT_ASK_RUN_ID: "66666666-7777-8888-9999-000000000000",
        },
    ) as job:
        with tracer().start_as_current_span("evaluate agent_answer", kind=Kind.INTERNAL) as grade:
            with telemetry.model_call_span(
                "scripted", max_tokens=512, prompt_version="judge-v2"
            ) as span:
                telemetry.record_model_response(span, response("scripted", 300, 40), 0.0)
            for name, passed in (("groundedness", True), ("answers_question", False)):
                grade.add_event(
                    "gen_ai.evaluation.result",
                    {
                        "gen_ai.evaluation.name": name,
                        "gen_ai.evaluation.score.value": 1.0 if passed else 0.0,
                        "gen_ai.evaluation.score.label": "pass" if passed else "fail",
                    },
                )
            grade.set_attribute("docpilot.judge.prompt_version", "judge-v2")
        job.set_attribute(telemetry.DOCPILOT_JOB_OUTCOME, "done")


{"api": run_api, "worker": run_worker}[mode]()
telemetry.shutdown_tracing()
print(f"{mode}: exported {len(received)} request(s)")

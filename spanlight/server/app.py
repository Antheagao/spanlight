"""The spanlight HTTP API.

A deliberately small surface: one ingest endpoint the SDK batches into, and
read endpoints the dashboard renders from. spanlight is a dev-tools daemon
for localhost or a private network: guard.py refuses foreign Hosts and
cross-site writes, and an optional bearer token protects writes.
"""

from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, errors
from .errors import api_error
from .guard import SECURITY_HEADERS, Guard
from .otlp import (
    OtlpDecodeError,
    decode_json,
    decode_protobuf,
    encode_export_response,
    map_request,
)
from .otlp.compression import (
    DecompressedTooLarge,
    MalformedCompression,
    UnsupportedContentEncoding,
    inflate,
)
from .settings import Settings

DEFAULT_DB = "spanlight.db"
DASHBOARD_DIST = Path(__file__).resolve().parent.parent.parent / "dashboard" / "dist"


class TraceIn(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=512)
    started_at: float
    ended_at: float | None = None
    status: Literal["ok", "error"] = "ok"
    session: str | None = None
    metadata: dict = Field(default_factory=dict)


class SpanIn(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)
    parent_id: str | None = None
    name: str = Field(min_length=1, max_length=512)
    kind: Literal["llm", "retrieval", "tool", "other"] = "other"
    started_at: float
    ended_at: float | None = None
    status: Literal["ok", "error"] = "ok"
    error: str | None = None
    model: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    input: str | None = None
    output: str | None = None
    attributes: dict = Field(default_factory=dict)


class IngestIn(BaseModel):
    traces: list[TraceIn] = Field(default_factory=list, max_length=1000)
    spans: list[SpanIn] = Field(default_factory=list, max_length=10000)


def create_app(
    db_path: str | None = None,
    *,
    settings: Settings | None = None,
    busy_timeout_ms: int = db.BUSY_TIMEOUT_MS,
) -> FastAPI:
    path = db_path or os.environ.get("SPANLIGHT_DB", DEFAULT_DB)
    settings = settings or Settings.from_env(os.environ)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = db.Database(path, busy_timeout_ms)
        yield
        app.state.db.close()

    # The interactive docs load scripts from a CDN, which the CSP blocks, so
    # they would render blank; and the schema has no other consumer.
    app = FastAPI(
        title="spanlight", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.settings = settings
    errors.install(app, SECURITY_HEADERS)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.dev_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Added last, so outermost: it sees every request first.
    app.add_middleware(Guard, settings=settings)

    def database() -> db.Database:
        return app.state.db

    @app.get("/healthz")
    def healthz() -> dict:
        with database().read() as conn:
            conn.execute("SELECT 1")
        return {"ok": True}

    @app.post("/api/ingest", status_code=202)
    def ingest(batch: IngestIn) -> dict:
        with database().write() as conn:
            db.ingest(
                conn,
                [t.model_dump() for t in batch.traces],
                [s.model_dump() for s in batch.spans],
            )
        return {"traces": len(batch.traces), "spans": len(batch.spans)}

    @app.get("/api/traces")
    def list_traces(
        limit: int = Query(default=50, ge=1, le=500),
        before: float | None = None,
        before_id: str | None = Query(default=None, max_length=128),
        q: str | None = Query(default=None, max_length=512),
    ) -> dict:
        if before_id is not None and before is None:
            raise api_error(400, "invalid_body", "before_id requires before", field="before_id")
        with database().read() as conn:
            traces = db.list_traces(conn, limit=limit, before=before, before_id=before_id, q=q)
        last = traces[-1] if len(traces) == limit else None
        return {
            "traces": traces,
            "next_before": last["started_at"] if last else None,
            "next_before_id": last["id"] if last else None,
        }

    @app.get("/api/traces/{trace_id}")
    def get_trace(trace_id: str) -> dict:
        with database().read() as conn:
            trace = db.get_trace(conn, trace_id)
        if trace is None:
            raise api_error(404, "trace_not_found", "trace not found")
        return trace

    @app.get("/api/stats/models")
    def stats_models(since: float | None = None) -> list[dict]:
        with database().read() as conn:
            return db.model_stats(conn, since=since)

    @app.get("/api/stats/timeseries")
    def stats_timeseries(
        since: float,
        bucket_seconds: int = Query(default=3600, ge=60, le=86400),
    ) -> list[dict]:
        with database().read() as conn:
            return db.timeseries(conn, since=since, bucket_seconds=bucket_seconds)

    def export_traces(body: bytes, content_type: str, encoding: str | None) -> tuple[bytes, str]:
        """Decode, map and store one OTLP export; returns the response body and
        the format it is in. Runs on a worker thread: decompression, decoding
        and the write are all blocking."""
        protobuf = content_type.partition(";")[0].strip().lower() == "application/x-protobuf"
        try:
            data = inflate(body, encoding, settings.max_request_bytes)
            request = decode_protobuf(data) if protobuf else decode_json(data)
        except UnsupportedContentEncoding as error:
            raise api_error(415, "unsupported_media_type", str(error)) from None
        except DecompressedTooLarge as error:
            raise api_error(413, "payload_too_large", str(error)) from None
        except (MalformedCompression, OtlpDecodeError) as error:
            raise api_error(400, "invalid_body", str(error)) from None
        batch = map_request(request, settings.cost_attributes)
        if batch.spans:
            with database().write() as conn:
                db.ingest_otlp(conn, batch.spans)
        fmt = "protobuf" if protobuf else "json"
        return encode_export_response(batch.rejected, batch.message, fmt), fmt

    exports_in_flight = threading.BoundedSemaphore(settings.max_exports_in_flight)

    @app.post("/v1/traces")
    async def otlp_traces(request: Request) -> Response:
        # Not waiting for a slot: a client told to retry later holds no memory
        # meanwhile, and OTLP exporters do retry a 503 with Retry-After.
        if not exports_in_flight.acquire(blocking=False):
            raise api_error(
                503,
                "overloaded",
                "too many exports in progress, retry",
                headers={"Retry-After": "1"},
            )
        try:
            # The guard has already read the body within the size cap.
            body, fmt = await run_in_threadpool(
                export_traces,
                await request.body(),
                request.headers.get("content-type", ""),
                request.headers.get("content-encoding"),
            )
        finally:
            exports_in_flight.release()
        media_type = "application/x-protobuf" if fmt == "protobuf" else "application/json"
        return Response(body, media_type=media_type)

    def traces_only() -> None:
        raise api_error(404, "not_found", "spanlight accepts traces only")

    for signal in ("metrics", "logs"):
        app.add_api_route(f"/v1/{signal}", traces_only, methods=["POST"], include_in_schema=False)

    # Unknown /api and /v1 paths answer in their prefix's error format whatever
    # the method, and are not handed to the dashboard mount below. This
    # catch-all also matches a known path with the wrong method, which
    # Starlette would otherwise turn into a 405 only when nothing later matches
    # fully, so it answers 405 itself.
    @app.api_route(
        "/v1/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        include_in_schema=False,
    )
    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        include_in_schema=False,
    )
    def unknown_api_route(request: Request) -> None:
        path = request.url.path
        allowed = sorted(
            {
                method
                for route in app.routes
                if isinstance(route, APIRoute)
                and route.endpoint is not unknown_api_route
                and route.path_regex.match(path)
                for method in route.methods
            }
        )
        if allowed:
            raise api_error(
                405,
                "method_not_allowed",
                "method not allowed",
                headers={"Allow": ", ".join(allowed)},
            )
        raise api_error(404, "not_found", "no such route")

    if DASHBOARD_DIST.is_dir():
        app.mount("/", StaticFiles(directory=DASHBOARD_DIST, html=True), name="dashboard")

    return app

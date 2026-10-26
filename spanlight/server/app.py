"""The spanlight HTTP API.

A deliberately small surface: one ingest endpoint the SDK batches into, and
read endpoints the dashboard renders from. No auth: spanlight is designed to
run on localhost or inside a private network, like a dev-tools daemon.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db

log = logging.getLogger(__name__)

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
    db_path: str | None = None, *, busy_timeout_ms: int = db.BUSY_TIMEOUT_MS
) -> FastAPI:
    path = db_path or os.environ.get("SPANLIGHT_DB", DEFAULT_DB)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = db.Database(path, busy_timeout_ms)
        yield
        app.state.db.close()

    app = FastAPI(title="spanlight", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def database() -> db.Database:
        return app.state.db

    @app.exception_handler(db.DatabaseBusy)
    def database_busy(request: Request, exc: db.DatabaseBusy) -> JSONResponse:
        # Another process held the write lock past the busy timeout. Clients
        # (including OTLP exporters) treat 503 + Retry-After as retryable.
        log.warning("%s %s: database busy", request.method, request.url.path)
        return JSONResponse(
            {"detail": "database busy, retry"}, status_code=503, headers={"Retry-After": "1"}
        )

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
            raise HTTPException(status_code=400, detail="before_id requires before")
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
            raise HTTPException(status_code=404, detail="trace not found")
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

    if DASHBOARD_DIST.is_dir():
        app.mount("/", StaticFiles(directory=DASHBOARD_DIST, html=True), name="dashboard")

    return app

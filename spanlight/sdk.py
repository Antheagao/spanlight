"""The spanlight tracing SDK.

Stdlib only, by design: tracing must be installable anywhere the app runs,
and an observability client must never take the app down. Every network
failure is swallowed (with one warning), the buffer drops rather than blocks
when full, and spans record exceptions without eating them.

Usage:

    from spanlight import Spanlight

    sl = Spanlight()  # http://127.0.0.1:4318
    with sl.trace("chat-request", user="demo") as t:
        with t.llm_span("answer", model="claude-haiku-4-5") as s:
            reply = call_model(...)
            s.record_usage(input_tokens=420, output_tokens=180,
                           input_text=prompt, output_text=reply)
"""

from __future__ import annotations

import atexit
import contextlib
import json
import threading
import time
import urllib.error
import urllib.request
import uuid
import warnings
from queue import Empty, Full, Queue
from typing import Any

# USD per million tokens (input, output). List prices as of October 2026;
# override per client with Spanlight(prices={...}). Unknown models get
# cost None rather than a made-up number.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-5.6-luna": (0.20, 1.20),
    "gemini-3.7-flash": (0.75, 3.75),
    "gemini-3.5-flash-lite": (0.30, 2.50),
}


def estimate_cost(
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    prices: dict[str, tuple[float, float]] | None = None,
) -> float | None:
    """Cost in USD, or None when the model or usage is unknown.

    Dated model ids match their base entry by prefix, longest prefix first
    (``claude-haiku-4-5-20251001`` -> ``claude-haiku-4-5``).
    """
    if model is None or input_tokens is None or output_tokens is None:
        return None
    table = {**DEFAULT_PRICES, **(prices or {})}
    entry = table.get(model)
    if entry is None:
        for key in sorted(table, key=len, reverse=True):
            if model.startswith(key):
                entry = table[key]
                break
    if entry is None:
        return None
    return (input_tokens * entry[0] + output_tokens * entry[1]) / 1_000_000


def _new_id() -> str:
    return uuid.uuid4().hex


class Span:
    """A timed unit of work inside a trace. Context manager; records an
    exception as status=error and re-raises it."""

    def __init__(
        self,
        client: Spanlight,
        trace: Trace,
        parent_id: str | None,
        name: str,
        kind: str,
        model: str | None = None,
        attributes: dict | None = None,
    ):
        self._client = client
        self._trace = trace
        self.id = _new_id()
        self.parent_id = parent_id
        self.name = name
        self.kind = kind
        self.model = model
        self.attributes = attributes or {}
        self.status = "ok"
        self.error: str | None = None
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.cost_usd: float | None = None
        self.input_text: str | None = None
        self.output_text: str | None = None
        self.started_at = time.time()
        self.ended_at: float | None = None

    def record_usage(
        self,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        input_text: str | None = None,
        output_text: str | None = None,
        model: str | None = None,
        cost_usd: float | None = None,
    ) -> None:
        """Attach token usage (and optionally the payloads) to an LLM span.
        Cost comes from the price table unless given explicitly."""
        if model is not None:
            self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.input_text = input_text
        self.output_text = output_text
        self.cost_usd = (
            cost_usd
            if cost_usd is not None
            else estimate_cost(self.model, input_tokens, output_tokens, self._client._prices)
        )

    def span(self, name: str, kind: str = "other", **attributes: Any) -> Span:
        return Span(self._client, self._trace, self.id, name, kind, attributes=attributes)

    def llm_span(self, name: str, model: str | None = None, **attributes: Any) -> Span:
        return Span(self._client, self._trace, self.id, name, "llm", model, attributes)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "trace_id": self._trace.id,
            "parent_id": self.parent_id,
            "name": self.name,
            "kind": self.kind,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "error": self.error,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "input": self.input_text,
            "output": self.output_text,
            "attributes": self.attributes,
        }

    def __enter__(self) -> Span:
        return self

    def __exit__(self, exc_type, exc, _tb) -> None:
        self.ended_at = time.time()
        if exc is not None:
            self.status = "error"
            self.error = repr(exc)
            self._trace.status = "error"
        self._client._enqueue("span", self.to_dict())
        return None  # never suppress the exception


class Trace:
    """One end-to-end request. Reported at open (so a crash still leaves a
    visible open trace) and upserted again at close."""

    def __init__(self, client: Spanlight, name: str, metadata: dict):
        self._client = client
        self.id = _new_id()
        self.name = name
        self.metadata = metadata
        self.status = "ok"
        self.started_at = time.time()
        self.ended_at: float | None = None

    def span(self, name: str, kind: str = "other", **attributes: Any) -> Span:
        return Span(self._client, self, None, name, kind, attributes=attributes)

    def llm_span(self, name: str, model: str | None = None, **attributes: Any) -> Span:
        return Span(self._client, self, None, name, "llm", model, attributes)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "session": self._client._session,
            "metadata": self.metadata,
        }

    def __enter__(self) -> Trace:
        self._client._enqueue("trace", self.to_dict())
        return self

    def __exit__(self, exc_type, exc, _tb) -> None:
        self.ended_at = time.time()
        if exc is not None:
            self.status = "error"
        self._client._enqueue("trace", self.to_dict())
        return None


class Spanlight:
    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:4318",
        *,
        session: str | None = None,
        prices: dict[str, tuple[float, float]] | None = None,
        flush_interval: float = 2.0,
        max_batch: int = 500,
        max_buffer: int = 10_000,
        timeout: float = 3.0,
        enabled: bool = True,
    ):
        self._endpoint = endpoint.rstrip("/")
        self._session = session
        self._prices = prices
        self._flush_interval = flush_interval
        self._max_batch = max_batch
        self._timeout = timeout
        self._enabled = enabled
        self._queue: Queue[tuple[str, dict]] = Queue(maxsize=max_buffer)
        self._send_lock = threading.Lock()
        self._warned = False
        self._closed = threading.Event()
        self._worker = threading.Thread(target=self._run, name="spanlight-flush", daemon=True)
        if enabled:
            self._worker.start()
            atexit.register(self.close)

    def trace(self, name: str, **metadata: Any) -> Trace:
        return Trace(self, name, metadata)

    def flush(self) -> None:
        """Drain the buffer and send it now."""
        self._flush_once()

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._flush_once()

    # -- internals ----------------------------------------------------------

    def _enqueue(self, kind: str, payload: dict) -> None:
        if not self._enabled:
            return
        with contextlib.suppress(Full):  # observability never blocks the app
            self._queue.put_nowait((kind, payload))

    def _run(self) -> None:
        while not self._closed.wait(self._flush_interval):
            self._flush_once()

    def _flush_once(self) -> None:
        with self._send_lock:
            while True:
                traces: list[dict] = []
                spans: list[dict] = []
                try:
                    while len(traces) + len(spans) < self._max_batch:
                        kind, payload = self._queue.get_nowait()
                        (traces if kind == "trace" else spans).append(payload)
                except Empty:
                    pass
                if not traces and not spans:
                    return
                self._send(traces, spans)

    def _send(self, traces: list[dict], spans: list[dict]) -> None:
        body = json.dumps({"traces": traces, "spans": spans}).encode()
        request = urllib.request.Request(
            f"{self._endpoint}/api/ingest",
            data=body,
            headers={"content-type": "application/json"},
        )
        try:
            urllib.request.urlopen(request, timeout=self._timeout).close()
        except (urllib.error.URLError, OSError) as exc:
            if not self._warned:
                self._warned = True
                warnings.warn(
                    f"spanlight: could not reach {self._endpoint} ({exc}); "
                    "traces are being dropped",
                    stacklevel=2,
                )

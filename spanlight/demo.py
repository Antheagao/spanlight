"""Seed a spanlight database with realistic demo traffic.

    python -m spanlight.demo [--db spanlight.db] [--days 7] [--traces 400] [--seed 7]

Writes straight into the database (no server needed), so the dashboard has
something to show before any real app is instrumented. Deterministic for a
given seed. All timestamps land in the past `--days`, denser during working
hours, with a small error rate.
"""

from __future__ import annotations

import argparse
import random
import time
import uuid

from .sdk import estimate_cost
from .server import db

MODELS = [
    ("claude-haiku-4-5", 0.45),
    ("gpt-5.6-luna", 0.25),
    ("claude-sonnet-5", 0.15),
    ("gemini-3.5-flash-lite", 0.15),
]

ERROR_MESSAGES = [
    "TimeoutError('model call exceeded 30s')",
    "RateLimitError('429 from provider')",
    "ValidationError('response failed schema check')",
]


def _model(rng: random.Random) -> str:
    return rng.choices([m for m, _ in MODELS], weights=[w for _, w in MODELS])[0]


def _llm(rng: random.Random, t: float, trace_id: str, parent: str | None, name: str,
         in_lo: int, in_hi: int, out_lo: int, out_hi: int) -> tuple[dict, float]:
    model = _model(rng)
    input_tokens = rng.randint(in_lo, in_hi)
    output_tokens = rng.randint(out_lo, out_hi)
    latency = min(rng.lognormvariate(-0.4, 0.6) + output_tokens / 900, 30.0)
    failed = rng.random() < 0.025
    span = {
        "id": uuid.uuid4().hex,
        "trace_id": trace_id,
        "parent_id": parent,
        "name": name,
        "kind": "llm",
        "started_at": t,
        "ended_at": t + latency,
        "status": "error" if failed else "ok",
        "error": rng.choice(ERROR_MESSAGES) if failed else None,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": estimate_cost(model, input_tokens, output_tokens),
        "input": f"[demo] prompt with {input_tokens} tokens",
        "output": None if failed else f"[demo] completion with {output_tokens} tokens",
        "attributes": {},
    }
    return span, t + latency


def _plain(rng: random.Random, t: float, trace_id: str, parent: str | None,
           name: str, kind: str, lo: float, hi: float, **attributes) -> tuple[dict, float]:
    latency = rng.uniform(lo, hi)
    span = {
        "id": uuid.uuid4().hex,
        "trace_id": trace_id,
        "parent_id": parent,
        "name": name,
        "kind": kind,
        "started_at": t,
        "ended_at": t + latency,
        "status": "ok",
        "error": None,
        "model": None,
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
        "input": None,
        "output": None,
        "attributes": attributes,
    }
    return span, t + latency


def make_trace(rng: random.Random, started_at: float) -> tuple[dict, list[dict]]:
    trace_id = uuid.uuid4().hex
    spans: list[dict] = []
    t = started_at
    shape = rng.choices(["chat", "summarize", "eval"], weights=[0.6, 0.25, 0.15])[0]

    if shape == "chat":
        name = "chat-request"
        if rng.random() < 0.5:
            span, t = _plain(rng, t, trace_id, None, "retrieve-context", "retrieval",
                             0.02, 0.3, index="docs", top_k=4)
            spans.append(span)
        span, t = _llm(rng, t, trace_id, None, "answer", 300, 2500, 80, 700)
        spans.append(span)
        if span["status"] == "ok" and rng.random() < 0.2:
            tool, t = _plain(rng, t, trace_id, None, "lookup-order", "tool",
                             0.05, 0.8, tool="orders_api")
            spans.append(tool)
            follow, t = _llm(rng, t, trace_id, None, "answer-with-tool-result",
                             400, 3000, 60, 400)
            spans.append(follow)
    elif shape == "summarize":
        name = "summarize-doc"
        span, t = _plain(rng, t, trace_id, None, "load-document", "other", 0.01, 0.2)
        spans.append(span)
        span, t = _llm(rng, t, trace_id, None, "summarize", 4000, 24000, 200, 900)
        spans.append(span)
    else:
        name = "eval-run"
        parent, t = _plain(rng, t, trace_id, None, "eval-batch", "other", 0.0, 0.05)
        spans.append(parent)
        for i in range(rng.randint(3, 6)):
            span, t = _llm(rng, t, trace_id, parent["id"], f"case-{i + 1}",
                           150, 900, 10, 120)
            spans.append(span)
        parent["ended_at"] = t

    status = "error" if any(s["status"] == "error" for s in spans) else "ok"
    trace = {
        "id": trace_id,
        "name": name,
        "started_at": started_at,
        "ended_at": t,
        "status": status,
        "session": "demo",
        "metadata": {"source": "spanlight.demo"},
    }
    return trace, spans


def working_hours_timestamp(rng: random.Random, now: float, days: int) -> float:
    """A past timestamp, biased toward 9:00-19:00 local time."""
    while True:
        t = now - rng.uniform(0, days * 86400)
        hour = time.localtime(t).tm_hour
        if 9 <= hour < 19 or rng.random() < 0.15:
            return t


def seed(path: str, days: int, traces: int, seed_value: int) -> tuple[int, int]:
    rng = random.Random(seed_value)
    now = time.time()
    conn = db.connect(path)
    all_traces, all_spans = [], []
    for _ in range(traces):
        trace, spans = make_trace(rng, working_hours_timestamp(rng, now, days))
        all_traces.append(trace)
        all_spans.extend(spans)
    db.ingest(conn, all_traces, all_spans)
    conn.close()
    return len(all_traces), len(all_spans)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="spanlight.db")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--traces", type=int, default=400)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    n_traces, n_spans = seed(args.db, args.days, args.traces, args.seed)
    print(f"seeded {n_traces} traces / {n_spans} spans into {args.db}")


if __name__ == "__main__":
    main()

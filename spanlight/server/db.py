"""SQLite storage for traces and spans.

One file, WAL mode, schema created on open. SQLite is deliberate: spanlight is
self-hosted observability for a single team, and a zero-setup embedded database
keeps the install story to `pip install` + `python -m spanlight.server`.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  started_at REAL NOT NULL,
  ended_at   REAL,
  status     TEXT NOT NULL DEFAULT 'ok',
  session    TEXT,
  metadata   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS traces_started_idx ON traces(started_at DESC);

CREATE TABLE IF NOT EXISTS spans (
  id            TEXT PRIMARY KEY,
  trace_id      TEXT NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
  parent_id     TEXT,
  name          TEXT NOT NULL,
  kind          TEXT NOT NULL DEFAULT 'other',
  started_at    REAL NOT NULL,
  ended_at      REAL,
  status        TEXT NOT NULL DEFAULT 'ok',
  error         TEXT,
  model         TEXT,
  input_tokens  INTEGER,
  output_tokens INTEGER,
  cost_usd      REAL,
  input         TEXT,
  output        TEXT,
  attributes    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS spans_trace_idx ON spans(trace_id);
CREATE INDEX IF NOT EXISTS spans_model_idx ON spans(model) WHERE model IS NOT NULL;
"""

TRACE_COLUMNS = ("id", "name", "started_at", "ended_at", "status", "session", "metadata")
SPAN_COLUMNS = (
    "id",
    "trace_id",
    "parent_id",
    "name",
    "kind",
    "started_at",
    "ended_at",
    "status",
    "error",
    "model",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "input",
    "output",
    "attributes",
)


def connect(path: str | Path) -> sqlite3.Connection:
    # One shared connection; FastAPI runs sync endpoints on a thread pool, so
    # cross-thread use is expected. CPython's sqlite3 is compiled serialized
    # (sqlite3.threadsafety == 3), which makes that safe.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def _upsert(conn: sqlite3.Connection, table: str, columns: tuple[str, ...], row: dict) -> None:
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c != "id")
    values = [
        json.dumps(row[c]) if c in ("metadata", "attributes") and row.get(c) is not None
        else row.get(c)
        for c in columns
    ]
    conn.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}",
        values,
    )


def ingest(conn: sqlite3.Connection, traces: list[dict], spans: list[dict]) -> None:
    """Upsert a batch atomically. The SDK reports a trace when it opens and
    again when it closes, so every write must be an upsert."""
    with conn:
        for trace in traces:
            _upsert(conn, "traces", TRACE_COLUMNS, trace)
        for span in spans:
            _upsert(conn, "spans", SPAN_COLUMNS, span)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for key in ("metadata", "attributes"):
        if key in d and isinstance(d[key], str):
            d[key] = json.loads(d[key])
    return d


def list_traces(
    conn: sqlite3.Connection,
    limit: int = 50,
    before: float | None = None,
    q: str | None = None,
) -> list[dict]:
    """Newest-first trace summaries with per-trace aggregates, paged by a
    started_at cursor (`before`)."""
    where, params = [], []
    if before is not None:
        where.append("t.started_at < ?")
        params.append(before)
    if q:
        where.append("t.name LIKE ?")
        params.append(f"%{q}%")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"""
        SELECT t.*,
               COUNT(s.id)                                   AS span_count,
               COALESCE(SUM(s.kind = 'llm'), 0)              AS llm_calls,
               COALESCE(SUM(s.input_tokens), 0)              AS input_tokens,
               COALESCE(SUM(s.output_tokens), 0)             AS output_tokens,
               COALESCE(SUM(s.cost_usd), 0.0)                AS cost_usd
        FROM traces t LEFT JOIN spans s ON s.trace_id = t.id
        {where_sql}
        GROUP BY t.id
        ORDER BY t.started_at DESC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_trace(conn: sqlite3.Connection, trace_id: str) -> dict | None:
    trace = conn.execute("SELECT * FROM traces WHERE id = ?", [trace_id]).fetchone()
    if trace is None:
        return None
    spans = conn.execute(
        "SELECT * FROM spans WHERE trace_id = ? ORDER BY started_at", [trace_id]
    ).fetchall()
    result = _row_to_dict(trace)
    result["spans"] = [_row_to_dict(s) for s in spans]
    return result


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """Nearest-rank percentile; callers pass a non-empty pre-sorted list."""
    index = min(len(sorted_values) - 1, max(0, round(fraction * (len(sorted_values) - 1))))
    return sorted_values[index]


def model_stats(conn: sqlite3.Connection, since: float | None = None) -> list[dict]:
    """Per-model aggregates over LLM spans. Percentiles are computed in Python:
    the fetch is bounded by `since` and a self-hosted deployment's span volume,
    and SQLite has no built-in percentile."""
    where, params = ["kind = 'llm'", "model IS NOT NULL", "ended_at IS NOT NULL"], []
    if since is not None:
        where.append("started_at >= ?")
        params.append(since)
    rows = conn.execute(
        f"""
        SELECT model, started_at, ended_at, status,
               COALESCE(input_tokens, 0) AS input_tokens,
               COALESCE(output_tokens, 0) AS output_tokens,
               COALESCE(cost_usd, 0.0) AS cost_usd
        FROM spans WHERE {' AND '.join(where)}
        """,
        params,
    ).fetchall()
    by_model: dict[str, dict] = {}
    for row in rows:
        stats = by_model.setdefault(
            row["model"],
            {
                "model": row["model"],
                "calls": 0,
                "errors": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
                "latencies": [],
            },
        )
        stats["calls"] += 1
        stats["errors"] += row["status"] == "error"
        stats["input_tokens"] += row["input_tokens"]
        stats["output_tokens"] += row["output_tokens"]
        stats["cost_usd"] += row["cost_usd"]
        stats["latencies"].append(row["ended_at"] - row["started_at"])
    results = []
    for stats in sorted(by_model.values(), key=lambda s: -s["cost_usd"]):
        latencies = sorted(stats.pop("latencies"))
        stats["latency_p50"] = _percentile(latencies, 0.50)
        stats["latency_p95"] = _percentile(latencies, 0.95)
        results.append(stats)
    return results


def timeseries(
    conn: sqlite3.Connection, since: float, bucket_seconds: int = 3600
) -> list[dict]:
    """Calls, cost, and errors over time for LLM spans, bucketed on started_at."""
    rows = conn.execute(
        """
        SELECT CAST(started_at / ? AS INTEGER) * ? AS bucket,
               COUNT(*)                            AS calls,
               COALESCE(SUM(cost_usd), 0.0)        AS cost_usd,
               COALESCE(SUM(status = 'error'), 0)  AS errors,
               COALESCE(SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)), 0)
                                                   AS tokens
        FROM spans
        WHERE kind = 'llm' AND started_at >= ?
        GROUP BY bucket ORDER BY bucket
        """,
        [bucket_seconds, bucket_seconds, since],
    ).fetchall()
    return [dict(r) for r in rows]

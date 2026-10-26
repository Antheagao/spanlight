"""SQLite storage for traces and spans.

One file, WAL mode, schema migrated on open (see migrations.py). SQLite is
deliberate: spanlight is self-hosted observability for a single team, and a
zero-setup embedded database keeps the install story to `pip install` +
`python -m spanlight.server`.

Feature functions take a connection and never commit, so a caller composes
them inside one `Database.write()` transaction.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .migrations import migrate

# How long a statement waits for another process's write lock, and a request
# for another thread's use of the connection.
BUSY_TIMEOUT_MS = 5000
# How long opening the database waits for another process that is migrating it.
MIGRATION_BUSY_TIMEOUT_MS = 120_000

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


def _is_busy(error: sqlite3.OperationalError) -> bool:
    # The low byte is the primary result code; the high bits are extended.
    code = error.sqlite_errorcode
    return code is not None and code & 0xFF == sqlite3.SQLITE_BUSY


def _enable_wal(conn: sqlite3.Connection, timeout_ms: int) -> None:
    """Switch to WAL, which persists in the file, so only the first open of a
    database pays for it. The switch needs exclusive access, and SQLite fails
    it at once instead of calling the busy handler when another connection is
    opening or migrating the same file, so retry on BUSY until the deadline.
    Any other failure, such as a read-only file, is raised immediately."""
    if conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
        return
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        try:
            mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        except sqlite3.OperationalError as error:
            if not _is_busy(error) or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)
            continue
        if mode != "wal":
            raise RuntimeError(f"could not switch the database to WAL (journal_mode is {mode!r})")
        return


def connect(path: str | Path, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """Open and migrate the database. The connection is in autocommit mode
    (`isolation_level=None`): transactions are explicit, via `Database`.

    Opening waits up to MIGRATION_BUSY_TIMEOUT_MS for another process that is
    migrating the same file, so a second server starting alongside the first
    comes up instead of failing; `busy_timeout_ms` applies once it is open."""
    if str(path) in ("", ":memory:"):
        raise ValueError(f"spanlight needs a database file, not {str(path)!r}")
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        opening_timeout_ms = max(busy_timeout_ms, MIGRATION_BUSY_TIMEOUT_MS)
        conn.execute(f"PRAGMA busy_timeout = {int(opening_timeout_ms)}")
        _enable_wal(conn, opening_timeout_ms)
        conn.execute("PRAGMA foreign_keys = ON")
        migrate(conn, path)
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    except BaseException:
        conn.close()
        raise
    return conn


class DatabaseBusy(Exception):
    """The database stayed busy past the busy timeout: another process held
    the write lock, or another thread held this process's lock. Retryable; the
    API maps it to 503 + Retry-After."""


@contextmanager
def _busy_as_exception() -> Iterator[None]:
    try:
        yield
    except sqlite3.OperationalError as error:
        if _is_busy(error):
            raise DatabaseBusy from error
        raise


class Database:
    """One shared connection behind one re-entrant lock.

    FastAPI runs sync routes on a thread pool, and a connection has a single
    transaction: without the lock, one thread's commit or rollback would cover
    another thread's half-written batch. The lock is re-entrant so a read can
    run inside a write; a write cannot run inside a write, because its
    COMMIT or ROLLBACK would end the outer transaction. Waiting for the lock
    is bounded by the busy timeout, like waiting for another process.
    """

    def __init__(self, path: str | Path, busy_timeout_ms: int = BUSY_TIMEOUT_MS):
        self._conn = connect(path, busy_timeout_ms)
        self._lock = threading.RLock()
        self._lock_timeout_s = busy_timeout_ms / 1000
        self._writing = False

    @contextmanager
    def _locked(self) -> Iterator[None]:
        if not self._lock.acquire(timeout=self._lock_timeout_s):
            raise DatabaseBusy("timed out waiting for another request's database access")
        try:
            yield
        finally:
            self._lock.release()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self._locked(), _busy_as_exception():
            yield self._conn

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """A `BEGIN IMMEDIATE` transaction: committed on success, rolled back
        on any exception. Taking the write lock up front means a busy database
        fails at BEGIN, before any work is done. Not re-entrant: pass the
        connection to the inner function instead of opening a second write()."""
        with self._locked(), _busy_as_exception():
            if self._writing:
                raise RuntimeError("write() cannot be nested; pass the connection down instead")
            self._conn.execute("BEGIN IMMEDIATE")
            self._writing = True
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
            finally:
                self._writing = False

    def close(self) -> None:
        with self._locked():
            self._conn.close()


# Mirror the column defaults, so a partial dict means the same as the API's
# Pydantic models filling them in. A column with neither a default here nor
# NULL allowed in the schema is required.
TRACE_DEFAULTS = {"status": "ok", "metadata": {}}
SPAN_DEFAULTS = {"kind": "other", "status": "ok", "attributes": {}}
TRACE_REQUIRED = ("id", "name", "started_at")
SPAN_REQUIRED = ("id", "trace_id", "name", "started_at")
JSON_COLUMNS = ("metadata", "attributes")


def _upsert(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    key: tuple[str, ...],
    required: tuple[str, ...],
    defaults: dict[str, Any],
    row: dict,
) -> None:
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c not in key)
    values = []
    for c in columns:
        value = row.get(c)
        if value is None:
            if c in required:
                raise ValueError(f"{table} row is missing required field {c!r}")
            value = defaults.get(c)
        values.append(json.dumps(value) if c in JSON_COLUMNS and value is not None else value)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT({', '.join(key)}) DO UPDATE SET {updates}",
        values,
    )


def ingest(conn: sqlite3.Connection, traces: list[dict], spans: list[dict]) -> None:
    """Upsert a batch. The SDK reports a trace when it opens and again when it
    closes, so every write must be an upsert. A span is keyed by
    (trace_id, id), so a colliding id in another trace is a different span and
    an upsert never moves a span between traces. Runs in the caller's
    transaction (`Database.write()`). A missing optional field takes its
    default; a missing required one raises ValueError."""
    for trace in traces:
        _upsert(conn, "traces", TRACE_COLUMNS, ("id",), TRACE_REQUIRED, TRACE_DEFAULTS, trace)
    for span in spans:
        _upsert(
            conn, "spans", SPAN_COLUMNS, ("trace_id", "id"), SPAN_REQUIRED, SPAN_DEFAULTS, span
        )


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for key in ("metadata", "attributes", "events", "links", "otel"):
        if key in d and isinstance(d[key], str):
            d[key] = json.loads(d[key])
    return d


# The page is picked from `traces` alone, in index order, and only then joined
# to its spans; aggregating first would read every span to return 50 traces.
LIST_TRACES_SQL = """
SELECT t.*,
       COUNT(s.id)                                   AS span_count,
       COALESCE(SUM(s.kind = 'llm'), 0)              AS llm_calls,
       COALESCE(SUM(s.input_tokens), 0)              AS input_tokens,
       COALESCE(SUM(s.output_tokens), 0)             AS output_tokens,
       COALESCE(SUM(s.cost_usd), 0.0)                AS cost_usd
FROM (
    SELECT * FROM traces
    {where}
    ORDER BY started_at DESC, id DESC
    LIMIT ?
) t
LEFT JOIN spans s ON s.trace_id = t.id
GROUP BY t.id
ORDER BY t.started_at DESC, t.id DESC
"""


def list_traces(
    conn: sqlite3.Connection,
    limit: int = 50,
    before: float | None = None,
    before_id: str | None = None,
    q: str | None = None,
) -> list[dict]:
    """Newest-first trace summaries with per-trace aggregates, paged by a
    (started_at, id) cursor. Traces can share a started_at, so a page boundary
    inside a tie needs `before_id` too; `before` alone skips the whole tie."""
    where, params = [], []
    if before is not None and before_id is not None:
        # A NULL id (possible only by editing the file by hand) never matches.
        where.append("(started_at, id) < (?, ?)")
        params.extend([before, before_id])
    elif before is not None:
        where.append("started_at < ?")
        params.append(before)
    if q:
        where.append("name LIKE ?")
        params.append(f"%{q}%")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(LIST_TRACES_SQL.format(where=where_sql), [*params, limit]).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_trace(conn: sqlite3.Connection, trace_id: str) -> dict | None:
    trace = conn.execute("SELECT * FROM traces WHERE id = ?", [trace_id]).fetchone()
    if trace is None:
        return None
    spans = conn.execute(
        "SELECT * FROM spans WHERE trace_id = ? ORDER BY started_at, id", [trace_id]
    ).fetchall()
    result = _row_to_dict(trace)
    result["spans"] = [_row_to_dict(s) for s in spans]
    return result


def _nearest_rank_index(count: int, fraction: float) -> int:
    """Index of the nearest-rank percentile in a sorted list of `count` items."""
    return min(count - 1, max(0, round(fraction * (count - 1))))


def model_stats(conn: sqlite3.Connection, since: float | None = None) -> list[dict]:
    """Per-model aggregates over LLM spans. Counts and sums are one grouped
    query. Percentiles are two indexed-order lookups per model (SQLite has no
    built-in percentile), so no span rows are loaded into Python."""
    where, params = ["kind = 'llm'", "model IS NOT NULL", "ended_at IS NOT NULL"], []
    if since is not None:
        where.append("started_at >= ?")
        params.append(since)
    where_sql = " AND ".join(where)
    rows = conn.execute(
        f"""
        SELECT model,
               COUNT(*)                                 AS calls,
               COALESCE(SUM(status = 'error'), 0)       AS errors,
               COALESCE(SUM(input_tokens), 0)           AS input_tokens,
               COALESCE(SUM(output_tokens), 0)          AS output_tokens,
               COALESCE(SUM(cost_usd), 0.0)             AS cost_usd
        FROM spans WHERE {where_sql}
        GROUP BY model
        ORDER BY cost_usd DESC, model
        """,
        params,
    ).fetchall()
    results = []
    for row in rows:
        stats = dict(row)
        for name, fraction in (("latency_p50", 0.50), ("latency_p95", 0.95)):
            stats[name] = conn.execute(
                f"SELECT ended_at - started_at FROM spans WHERE {where_sql} AND model = ?"
                " ORDER BY ended_at - started_at LIMIT 1 OFFSET ?",
                [*params, row["model"], _nearest_rank_index(row["calls"], fraction)],
            ).fetchone()[0]
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

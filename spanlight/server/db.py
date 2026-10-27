"""SQLite storage for traces and spans.

One file, WAL mode, schema migrated on open (see migrations.py). SQLite is
deliberate: spanlight is self-hosted observability for a single team, and a
zero-setup embedded database keeps the install story to `pip install` +
`python -m spanlight.server`.

Feature functions take a connection and never commit, so a caller composes
them inside one `Database.write()` transaction.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .migrations import migrate
from .otlp.values import dumps_json_safe

log = logging.getLogger(__name__)

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


def _create_private(path: str | Path) -> None:
    """Create the database file readable by its owner only: it holds prompts
    and completions, and SQLite would create it 0644. SQLite gives the -wal and
    -shm files the same mode. An existing file is never changed silently, but a
    group or world readable one is reported."""
    # Two processes starting at once may both get here; the loser finds the
    # file the winner made.
    with contextlib.suppress(FileExistsError):
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    mode = os.stat(path).st_mode
    if mode & 0o044:
        log.warning(
            "%s is readable by other users (mode %o); it holds prompts and completions, "
            "so consider chmod 600",
            path,
            mode & 0o777,
        )


def connect(path: str | Path, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """Open and migrate the database. The connection is in autocommit mode
    (`isolation_level=None`): transactions are explicit, via `Database`.

    Opening waits up to MIGRATION_BUSY_TIMEOUT_MS for another process that is
    migrating the same file, so a second server starting alongside the first
    comes up instead of failing; `busy_timeout_ms` applies once it is open."""
    if str(path) in ("", ":memory:"):
        raise ValueError(f"spanlight needs a database file, not {str(path)!r}")
    _create_private(path)
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
JSON_COLUMNS = ("metadata", "attributes", "events", "links", "otel")
# What the OTLP path adds: where the span ran and the parts of OTLP that have
# no column of their own.
OTLP_SPAN_COLUMNS = (*SPAN_COLUMNS, "service", "events", "links", "otel")
OTLP_SPAN_DEFAULTS = {**SPAN_DEFAULTS, "events": [], "links": [], "otel": {}}


def _upsert(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    key: tuple[str, ...],
    required: tuple[str, ...],
    defaults: dict[str, Any],
    row: dict,
    dumps: Callable[[Any], str] = json.dumps,
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
        values.append(dumps(value) if c in JSON_COLUMNS and value is not None else value)
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


def ingest_otlp(conn: sqlite3.Connection, spans: list[dict]) -> None:
    """Upsert OTLP spans (as `otlp.mapping` produces them) and derive their
    traces. Runs in the caller's transaction.

    OTLP sends spans, never traces, and a trace's spans arrive in any order and
    batch (children usually export before their root). So a trace row is never
    taken from a request: it is recomputed from the stored spans after every
    upsert. The result then depends only on which spans have arrived, not on
    their order, and resending a batch changes nothing.
    """
    first_span: dict[str, dict] = {}
    for span in spans:
        first_span.setdefault(span["trace_id"], span)
    # A stub only satisfies the foreign key; _derive_trace overwrites it. An
    # existing row, including an SDK trace, is left as it is.
    for trace_id, span in first_span.items():
        conn.execute(
            "INSERT OR IGNORE INTO traces (id, name, started_at, source) VALUES (?, ?, ?, 'otlp')",
            [trace_id, span["name"], span["started_at"]],
        )
    stored_contexts: set[tuple[str, str, str]] = set()
    for span in spans:
        _upsert(
            conn, "spans", OTLP_SPAN_COLUMNS, ("trace_id", "id"), SPAN_REQUIRED,
            OTLP_SPAN_DEFAULTS, span, dumps=dumps_json_safe,
        )
        for table, context in zip(("resources", "scopes"), span["contexts"].values(), strict=True):
            key = (table, span["trace_id"], context.digest)
            if key not in stored_contexts:
                stored_contexts.add(key)
                conn.execute(
                    f"INSERT OR IGNORE INTO {table} (trace_id, digest, data) VALUES (?, ?, ?)",
                    [span["trace_id"], context.digest, dumps_json_safe(context.data)],
                )
    for trace_id in first_span:
        _derive_trace(conn, trace_id)


# Attributes that name the conversation a trace belongs to, in order of preference.
SESSION_ATTRIBUTES = ("session.id", "gen_ai.conversation.id")


def _derive_trace(conn: sqlite3.Connection, trace_id: str) -> None:
    """Recompute an OTLP trace row from its stored spans.

    The window is the earliest start to the latest end. The name, status and
    root span id come from the root (the earliest span without a parent);
    until a root arrives, the name is the earliest span's and the status is
    error if any span failed. Every choice has a (started_at, id) tiebreak, so
    equal timestamps cannot make the result depend on arrival order.
    """
    window = conn.execute(
        "SELECT MIN(started_at), MAX(ended_at), COALESCE(MAX(status = 'error'), 0)"
        " FROM spans WHERE trace_id = ?",
        [trace_id],
    ).fetchone()
    root = conn.execute(
        "SELECT id, name, status FROM spans WHERE trace_id = ? AND parent_id IS NULL"
        " ORDER BY started_at, id LIMIT 1",
        [trace_id],
    ).fetchone()
    if root is not None:
        name, status, root_id = root["name"], root["status"], root["id"]
    else:
        earliest = conn.execute(
            "SELECT name FROM spans WHERE trace_id = ? ORDER BY started_at, id LIMIT 1",
            [trace_id],
        ).fetchone()
        name, status, root_id = earliest["name"], "error" if window[2] else "ok", None
    session = None
    for attribute in SESSION_ATTRIBUTES:
        found = conn.execute(
            "SELECT json_extract(attributes, ?) FROM spans WHERE trace_id = ?"
            " AND json_type(attributes, ?) = 'text' AND json_extract(attributes, ?) != ''"
            " ORDER BY started_at, id LIMIT 1",
            [f'$."{attribute}"', trace_id, f'$."{attribute}"', f'$."{attribute}"'],
        ).fetchone()
        if found is not None:
            session = found[0]
            break
    conn.execute(
        "UPDATE traces SET name = ?, started_at = ?, ended_at = ?, status = ?, session = ?,"
        " root_span_id = ? WHERE id = ? AND source = 'otlp'",
        [name, window[0], window[1], status, session, root_id, trace_id],
    )
    # A resent span may point at a different resource or scope than before; keep
    # only what the stored spans still refer to, so the rows depend on the
    # spans alone.
    for table, member in (("resources", "resource"), ("scopes", "scope")):
        conn.execute(
            f"DELETE FROM {table} WHERE trace_id = ? AND digest NOT IN"
            f" (SELECT json_extract(otel, '$.{member}') FROM spans WHERE trace_id = ?"
            f" AND json_extract(otel, '$.{member}') IS NOT NULL)",
            [trace_id, trace_id],
        )


def _services(conn: sqlite3.Connection, trace_ids: list[str]) -> dict[str, list[str]]:
    """The distinct service names of each trace's spans, sorted."""
    if not trace_ids:
        return {}
    marks = ", ".join("?" for _ in trace_ids)
    rows = conn.execute(
        f"SELECT trace_id, service FROM spans WHERE trace_id IN ({marks})"
        " AND service IS NOT NULL GROUP BY trace_id, service ORDER BY trace_id, service",
        trace_ids,
    ).fetchall()
    services: dict[str, list[str]] = {trace_id: [] for trace_id in trace_ids}
    for row in rows:
        services[row["trace_id"]].append(row["service"])
    return services


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
    traces = [_row_to_dict(r) for r in rows]
    services = _services(conn, [t["id"] for t in traces])
    for trace in traces:
        trace["services"] = services[trace["id"]]
    return traces


def get_trace(conn: sqlite3.Connection, trace_id: str) -> dict | None:
    trace = conn.execute("SELECT * FROM traces WHERE id = ?", [trace_id]).fetchone()
    if trace is None:
        return None
    spans = conn.execute(
        "SELECT * FROM spans WHERE trace_id = ? ORDER BY started_at, id", [trace_id]
    ).fetchall()
    result = _row_to_dict(trace)
    result["spans"] = [_row_to_dict(s) for s in spans]
    result["services"] = _services(conn, [trace_id])[trace_id]
    # Shared by the spans that name them in `otel.resource` and `otel.scope`.
    for key, table in (("resources", "resources"), ("scopes", "scopes")):
        result[key] = {
            row["digest"]: json.loads(row["data"])
            for row in conn.execute(
                f"SELECT digest, data FROM {table} WHERE trace_id = ? ORDER BY digest", [trace_id]
            )
        }
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

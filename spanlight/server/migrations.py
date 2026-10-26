"""Versioned schema migrations, tracked in SQLite's `PRAGMA user_version`.

`MIGRATIONS` is append-only: an applied migration is never edited, a change
is a new entry. Each one runs in its own `BEGIN IMMEDIATE` transaction, so
two processes opening the same file serialize on the write lock and the
second finds nothing left to do.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

# The schema as of e52b962, created with IF NOT EXISTS so a database from
# before versioning adopts it as version 1 without being touched.
_BASELINE = """
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

# A span id is only unique within its trace (OpenTelemetry), so the identity
# becomes (trace_id, id); see docs/adr/0001-trace-scoped-span-identity.md.
# SQLite cannot change a primary key in place, hence the rebuild. Nothing
# references spans, so foreign_keys can stay on while it is dropped.
# The old spans_trace_idx is not recreated: the primary key now leads with
# trace_id and serves the same lookups.
_TRACE_SCOPED_SPANS = """
CREATE TABLE spans_new (
  id            TEXT NOT NULL,
  trace_id      TEXT NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
  parent_id     TEXT,
  name          TEXT NOT NULL,
  kind          TEXT NOT NULL DEFAULT 'other'
                CHECK (kind IN ('llm', 'retrieval', 'tool', 'other')),
  started_at    REAL NOT NULL,
  ended_at      REAL,
  status        TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'error')),
  error         TEXT,
  model         TEXT,
  input_tokens  INTEGER,
  output_tokens INTEGER,
  cost_usd      REAL,
  input         TEXT,
  output        TEXT,
  attributes    TEXT NOT NULL DEFAULT '{}',
  -- NULL for SDK spans, which have no resource.
  service       TEXT,
  events        TEXT NOT NULL DEFAULT '[]',
  links         TEXT NOT NULL DEFAULT '[]',
  -- Span kind, scope, trace_state, flags, dropped counts, resource attributes.
  otel          TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (trace_id, id)
);
INSERT INTO spans_new (
  id, trace_id, parent_id, name, kind, started_at, ended_at, status, error, model,
  input_tokens, output_tokens, cost_usd, input, output, attributes
)
SELECT
  id, trace_id, parent_id, name, kind, started_at, ended_at, status, error, model,
  input_tokens, output_tokens, cost_usd, input, output, attributes
FROM spans;
DROP TABLE spans;
ALTER TABLE spans_new RENAME TO spans;
CREATE INDEX spans_model_idx ON spans(model) WHERE model IS NOT NULL;
-- Serves the windowed stats, which filter on kind and a started_at range.
CREATE INDEX spans_kind_started_idx ON spans(kind, started_at);

ALTER TABLE traces ADD COLUMN source TEXT NOT NULL DEFAULT 'sdk'
  CHECK (source IN ('sdk', 'otlp'));
-- NULL until the root span of an OTLP trace has arrived.
ALTER TABLE traces ADD COLUMN root_span_id TEXT;
"""

# The trace list orders and pages by (started_at, id), both descending. It
# replaces traces_started_idx, which is a prefix of it.
_TRACES_PAGING_INDEX = """
CREATE INDEX traces_started_id_idx ON traces(started_at DESC, id DESC);
DROP INDEX traces_started_idx;
"""

# (version, name, SQL). Versions are consecutive from 1.
MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "baseline", _BASELINE),
    (2, "trace_scoped_spans", _TRACE_SCOPED_SPANS),
    (3, "traces_paging_index", _TRACES_PAGING_INDEX),
]


def _statements(sql: str) -> Iterator[str]:
    """Split a script into statements. `executescript` is unusable here: it
    commits the transaction the migration runs in. A `;` inside a string,
    comment, or trigger body does not end a statement."""
    *pieces, rest = sql.split(";")
    buffer = ""
    for piece in pieces:
        buffer += piece + ";"
        if sqlite3.complete_statement(buffer):
            yield buffer
            buffer = ""
    leftover = "\n".join(
        line for line in (buffer + rest).splitlines() if not line.strip().startswith("--")
    ).strip()
    if leftover:
        raise ValueError(f"migration ends in an incomplete statement: {leftover!r}")


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _backup(path: str | Path, version: int) -> str:
    """Copy the database to `<path>.v<version>.bak`, where `version` is the
    schema version it is at, and return that path.

    A backup is never overwritten: if a later start finds the file, it is the
    older and more useful copy (an upgrade that fails halfway leaves a
    database that is already partly rebuilt). Called while the caller holds
    the write lock, so the copy is a consistent state. A separate connection
    reads it: sqlite3's backup hangs when the source is the connection
    holding the write transaction."""
    backup_path = f"{path}.v{version}.bak"
    if os.path.exists(backup_path):
        return backup_path
    partial = f"{backup_path}.partial"
    # A partial left by a crash is ours to discard. Creating it ourselves with
    # O_EXCL and mode 0600 keeps the copy as private as a database that may
    # hold prompts, and refuses to write through a planted symlink.
    with contextlib.suppress(FileNotFoundError):
        os.unlink(partial)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    os.close(os.open(partial, flags, 0o600))
    try:
        source = sqlite3.connect(path)
        destination = sqlite3.connect(partial)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        # Atomic, so a crash never leaves a half-written file under the final name.
        os.replace(partial, backup_path)
    except BaseException:
        for leftover in (partial, f"{partial}-journal"):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(leftover)
        raise
    return backup_path


_KINDS = "('llm', 'retrieval', 'tool', 'other')"
_STATUSES = "('ok', 'error')"


def _check_trace_scoped_spans(
    conn: sqlite3.Connection, path: str | Path, backup_path: str | None
) -> None:
    """Rows the rebuild's CHECKs and foreign key would reject. Counting them
    first turns a bare IntegrityError into something an operator can act on.
    SDK-written data never has any; they come from hand edits."""
    bad_kind = conn.execute(f"SELECT COUNT(*) FROM spans WHERE kind NOT IN {_KINDS}").fetchone()[0]
    bad_status = conn.execute(
        f"SELECT COUNT(*) FROM spans WHERE status NOT IN {_STATUSES}"
    ).fetchone()[0]
    orphans = conn.execute(
        "SELECT COUNT(*) FROM spans s"
        " WHERE NOT EXISTS (SELECT 1 FROM traces t WHERE t.id = s.trace_id)"
    ).fetchone()[0]
    if not (bad_kind or bad_status or orphans):
        return
    raise RuntimeError(
        f"cannot migrate {path} to schema version 2: {bad_kind} spans have a kind outside "
        f"{_KINDS}, {bad_status} have a status outside {_STATUSES}, and {orphans} belong to "
        f"no trace. The database is unchanged; a copy from before the upgrade is at "
        f"{backup_path}. Fix the rows (adjust the values to what you want), then start again:\n"
        f"  UPDATE spans SET kind = 'other' WHERE kind NOT IN {_KINDS};\n"
        f"  UPDATE spans SET status = 'error' WHERE status NOT IN {_STATUSES};\n"
        f"  DELETE FROM spans WHERE trace_id NOT IN (SELECT id FROM traces);"
    )


# Checks run inside a migration's transaction, before its SQL, keyed by version.
_PREFLIGHT: dict[int, Callable[[sqlite3.Connection, str | Path, str | None], None]] = {
    2: _check_trace_scoped_spans,
}


def migrate(
    conn: sqlite3.Connection,
    path: str | Path,
    migrations: Sequence[tuple[int, str, str]] = MIGRATIONS,
) -> None:
    """Bring the database to the newest version. `conn` must be in autocommit
    mode (`isolation_level=None`). A non-empty database that is behind is
    copied aside first, once per start (see `_backup`)."""
    latest = migrations[-1][0]
    current = _user_version(conn)
    if current > latest:
        raise RuntimeError(
            f"{path} is at schema version {current}, but this spanlight only "
            f"knows up to {latest}; upgrade spanlight"
        )
    if current == latest:
        return
    backed_up = False
    backup_path = None
    for version, name, sql in migrations:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-read under the lock: another process may have migrated
            # between the check above and BEGIN returning.
            current = _user_version(conn)
            if current >= version:
                conn.execute("ROLLBACK")
                continue
            if not backed_up and conn.execute("SELECT 1 FROM sqlite_master").fetchone():
                backup_path = _backup(path, current)
            backed_up = True
            if preflight := _PREFLIGHT.get(version):
                preflight(conn, path, backup_path)
            for statement in _statements(sql):
                conn.execute(statement)
            if violations := conn.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError(
                    f"database has {len(violations)} pre-existing foreign key violations "
                    f"(see PRAGMA foreign_key_check); migration {version:04d}_{name} was "
                    f"rolled back. A copy from before the upgrade is at {backup_path}"
                )
            conn.execute(f"PRAGMA user_version = {int(version)}")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

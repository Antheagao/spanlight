import os
import sqlite3
import stat
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from spanlight.server import create_app, db
from spanlight.server.migrations import MIGRATIONS, _statements, migrate

LATEST = MIGRATIONS[-1][0]
SCHEMA_V0 = (Path(__file__).parent / "fixtures" / "schema_v0.sql").read_text()

SPAN_V0_COLUMNS = (
    "id, trace_id, parent_id, name, kind, started_at, ended_at, status, error, model, "
    "input_tokens, output_tokens, cost_usd, input, output, attributes"
)


def make_legacy(path, traces=3, spans_per_trace=4):
    """A database as e52b962 left it: the old DDL, no user_version, some rows."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_V0)
    for t in range(traces):
        conn.execute(
            "INSERT INTO traces (id, name, started_at, ended_at, status, session, metadata)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [f"t{t}", f"req-{t}", 1000.0 + t, 1001.0 + t, "error" if t == 0 else "ok",
             "demo", '{"user": "demo"}'],
        )
        for s in range(spans_per_trace):
            conn.execute(
                f"INSERT INTO spans ({SPAN_V0_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [f"s{t}-{s}", f"t{t}", None if s == 0 else f"s{t}-0", f"span-{s}",
                 "llm" if s % 2 else "other", 1000.0 + t + s / 10, 1000.5 + t + s / 10,
                 "error" if s == 3 else "ok", "boom" if s == 3 else None,
                 "haiku" if s % 2 else None, 100 if s % 2 else None, 50 if s % 2 else None,
                 0.001 if s % 2 else None, "in", "out", '{"k": [1, 2]}'],
            )
    conn.commit()
    conn.close()


def trace(id):
    return {
        "id": id, "name": "req", "started_at": 1.0, "ended_at": None,
        "status": "ok", "session": None, "metadata": {},
    }


def span(id, trace_id, name):
    return {
        "id": id, "trace_id": trace_id, "parent_id": None, "name": name, "kind": "other",
        "started_at": 1.0, "ended_at": None, "status": "ok", "error": None, "model": None,
        "input_tokens": None, "output_tokens": None, "cost_usd": None, "input": None,
        "output": None, "attributes": {},
    }


def dump(conn, sql):
    return [tuple(r) for r in conn.execute(sql).fetchall()]


def version(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def primary_key(path):
    conn = sqlite3.connect(path)
    try:
        columns = conn.execute("PRAGMA table_info(spans)").fetchall()
        return [c[1] for c in sorted(columns, key=lambda c: c[5]) if c[5]]
    finally:
        conn.close()


def test_migration_versions_are_consecutive_from_one():
    assert [v for v, _, _ in MIGRATIONS] == list(range(1, len(MIGRATIONS) + 1))


def test_fresh_database_reaches_latest_with_trace_scoped_spans(tmp_path):
    path = tmp_path / "fresh.db"
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST

    pk = {r["name"]: r["pk"] for r in conn.execute("PRAGMA table_info(spans)")}
    assert (pk["trace_id"], pk["id"]) == (1, 2)
    assert sum(1 for v in pk.values() if v) == 2

    conn.execute("INSERT INTO traces (id, name, started_at) VALUES ('t', 'n', 1.0)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO spans (id, trace_id, name, kind, started_at)"
            " VALUES ('s', 't', 'n', 'bogus', 1.0)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO spans (id, trace_id, name, status, started_at)"
            " VALUES ('s', 't', 'n', 'bogus', 1.0)"
        )
    conn.close()
    assert not list(tmp_path.glob("*.bak")), "an empty database needs no backup"


def test_legacy_database_migrates_with_data_preserved_and_backed_up(tmp_path):
    path = tmp_path / "legacy.db"
    make_legacy(path)
    before = sqlite3.connect(path)
    old_traces = dump(before, "SELECT * FROM traces ORDER BY id")
    old_spans = dump(before, f"SELECT {SPAN_V0_COLUMNS} FROM spans ORDER BY trace_id, id")
    before.close()
    assert version(path) == 0

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST
    assert dump(conn, f"SELECT {SPAN_V0_COLUMNS} FROM spans ORDER BY trace_id, id") == old_spans
    new_traces = dump(
        conn, "SELECT id, name, started_at, ended_at, status, session, metadata"
        " FROM traces ORDER BY id")
    assert new_traces == old_traces
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    # New columns take their defaults on migrated rows.
    assert dump(conn, "SELECT DISTINCT source, root_span_id FROM traces") == [("sdk", None)]
    assert dump(conn, "SELECT DISTINCT service, events, links, otel FROM spans") == [
        (None, "[]", "[]", "{}")
    ]
    index_names = {r["name"] for r in conn.execute("PRAGMA index_list(spans)")}
    assert {"spans_model_idx", "spans_kind_started_idx"} <= index_names
    # The cascade survived the rebuild.
    conn.execute("DELETE FROM traces WHERE id = 't0'")
    assert conn.execute("SELECT COUNT(*) FROM spans WHERE trace_id = 't0'").fetchone()[0] == 0
    conn.close()

    backup = Path(f"{path}.v0.bak")
    assert sorted(p.name for p in tmp_path.glob("legacy.db*.bak")) == ["legacy.db.v0.bak"]
    assert not Path(f"{backup}.partial").exists()
    assert version(backup) == 0
    assert primary_key(backup) == ["id"]
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    restored = sqlite3.connect(backup)
    assert dump(restored, f"SELECT {SPAN_V0_COLUMNS} FROM spans ORDER BY trace_id, id") == old_spans
    restored.close()


def test_migrated_legacy_database_serves_through_the_api(tmp_path):
    path = tmp_path / "legacy.db"
    make_legacy(path)
    with TestClient(create_app(db_path=str(path))) as client:
        detail = client.get("/api/traces/t1").json()
        assert [s["id"] for s in detail["spans"]] == ["s1-0", "s1-1", "s1-2", "s1-3"]
        assert detail["spans"][0]["attributes"] == {"k": [1, 2]}
        assert detail["spans"][0]["events"] == []
        assert detail["spans"][0]["otel"] == {}
        assert detail["source"] == "sdk"
        assert client.get("/api/traces").json()["traces"][0]["span_count"] == 4


def test_reopening_is_a_no_op(tmp_path):
    path = tmp_path / "legacy.db"
    make_legacy(path)
    db.connect(path).close()
    backup = Path(f"{path}.v0.bak")
    backup_mtime = backup.stat().st_mtime_ns

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST
    assert conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0] == 12
    assert backup.stat().st_mtime_ns == backup_mtime
    conn.close()


def test_two_processes_opening_a_legacy_database_at_once(tmp_path):
    path = tmp_path / "legacy.db"
    make_legacy(path, traces=40, spans_per_trace=250)
    # Both children sleep until just before the same instant, then spin to it
    # (a sleeping child can overshoot, a spinning one burns a core), so their
    # migrations contend.
    script = textwrap.dedent(
        """
        import sys, time
        from spanlight.server import db
        start = float(sys.argv[2])
        time.sleep(max(0.0, start - 0.05 - time.time()))
        while time.time() < start:
            pass
        conn = db.connect(sys.argv[1])
        print(conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0])
        """
    )
    start = str(time.time() + 3)
    children = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(path), start],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(2)
    ]
    try:
        results = [(c.communicate(timeout=60), c.returncode) for c in children]
    finally:
        for c in children:
            if c.poll() is None:
                c.kill()
                c.wait()

    for (out, err), code in results:
        assert code == 0, err
        assert out.strip() == "10000"
    assert version(path) == LATEST
    # Each migration is its own transaction, so either process may have run any
    # of them. Whichever did first saw version 0, and that copy was never
    # overwritten, so it still holds the original schema and every row.
    original = Path(f"{path}.v0.bak")
    assert version(original) == 0
    assert primary_key(original) == ["id"]
    restored = sqlite3.connect(original)
    assert restored.execute("SELECT COUNT(*) FROM spans").fetchone()[0] == 10000
    restored.close()
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_a_second_connection_waits_for_the_first_and_finds_nothing_to_do(tmp_path):
    path = tmp_path / "legacy.db"
    make_legacy(path)
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    opened = []
    thread = threading.Thread(target=lambda: opened.append(db.connect(path)))
    thread.start()
    # B has either read version 0 and is blocked on the lock, or has not
    # started yet; the outcome below must hold in both cases.
    time.sleep(0.3)
    for _, _, sql in MIGRATIONS:
        for statement in _statements(sql):
            holder.execute(statement)
    holder.execute(f"PRAGMA user_version = {LATEST}")
    holder.execute("COMMIT")
    thread.join(timeout=30)

    assert len(opened) == 1
    assert opened[0].execute("PRAGMA user_version").fetchone()[0] == LATEST
    opened[0].close()
    holder.close()


def test_a_failing_migration_changes_nothing(tmp_path):
    path = tmp_path / "legacy.db"
    make_legacy(path)
    conn = db.connect(path)
    before = dump(conn, "SELECT * FROM spans ORDER BY trace_id, id")
    broken = [
        *MIGRATIONS,
        (LATEST + 1, "broken", "CREATE TABLE half_done (a); INSERT INTO nope VALUES (1);"),
    ]

    with pytest.raises(sqlite3.OperationalError, match="nope"):
        migrate(conn, path, broken)

    assert not conn.in_transaction
    assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST
    assert dump(conn, "SELECT * FROM spans ORDER BY trace_id, id") == before
    assert dump(conn, "SELECT name FROM sqlite_master WHERE name = 'half_done'") == []
    conn.close()


def test_a_foreign_key_violation_rolls_the_migration_back(tmp_path):
    path = tmp_path / "fresh.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO traces (id, name, started_at) VALUES ('t', 'n', 1.0)")
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO spans (id, trace_id, name, started_at) VALUES ('s', 'gone', 'n', 1)")
    conn.execute("PRAGMA foreign_keys = ON")
    orphaning = [*MIGRATIONS, (LATEST + 1, "noop", "SELECT 1;")]

    with pytest.raises(RuntimeError, match="pre-existing foreign key violations"):
        migrate(conn, path, orphaning)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST
    conn.close()


def test_a_database_from_a_newer_spanlight_is_refused(tmp_path):
    path = tmp_path / "newer.db"
    db.connect(path).close()
    raw = sqlite3.connect(path)
    raw.execute(f"PRAGMA user_version = {LATEST + 1}")
    raw.close()

    with pytest.raises(RuntimeError, match="upgrade spanlight"):
        db.connect(path)


def test_statements_split_on_complete_statements_only():
    sql = """
    -- a comment; with a semicolon
    CREATE TABLE a (x);
    CREATE TRIGGER t AFTER INSERT ON a BEGIN
      UPDATE a SET x = 1;
      UPDATE a SET x = 2;
    END;
    """
    parts = [p.strip() for p in _statements(sql)]
    assert len(parts) == 2
    assert parts[0].endswith("CREATE TABLE a (x);")
    assert parts[1].startswith("CREATE TRIGGER") and parts[1].endswith("END;")
    assert len(list(_statements("SELECT 1; SELECT 'a;b'; -- trailing comment"))) == 2
    with pytest.raises(ValueError, match="incomplete"):
        list(_statements("SELECT 1; SELECT"))


def test_same_span_id_in_two_traces_is_stored_twice_and_upsert_cannot_move_it(tmp_path):
    database = db.Database(tmp_path / "ids.db")
    traces = [trace("t1"), trace("t2")]
    spans = [span("s", "t1", "first"), span("s", "t2", "second")]
    with database.write() as conn:
        db.ingest(conn, traces, spans)

    again = span("s", "t1", "first-renamed")
    with database.write() as conn:
        db.ingest(conn, [], [again])

    with database.read() as conn:
        rows = dump(conn, "SELECT trace_id, id, name FROM spans ORDER BY trace_id")
    assert rows == [("t1", "s", "first-renamed"), ("t2", "s", "second")]
    database.close()


def test_a_failed_upgrade_never_replaces_the_original_backup(tmp_path):
    path = tmp_path / "legacy.db"
    make_legacy(path)
    broken = [*MIGRATIONS[:2], (3, "broken", "CREATE INDEX x ON nope(a);")]
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.OperationalError, match="nope"):
        migrate(conn, path, broken)
    conn.close()
    original = Path(f"{path}.v0.bak")
    original_bytes = original.read_bytes()
    assert version(path) == 2
    assert primary_key(path) == ["trace_id", "id"]
    assert primary_key(original) == ["id"]

    db.connect(path).close()

    assert version(path) == LATEST
    assert original.read_bytes() == original_bytes
    assert primary_key(original) == ["id"]
    # The second start backed up the state it found, under its own name.
    assert primary_key(Path(f"{path}.v2.bak")) == ["trace_id", "id"]


def test_the_backup_is_private_even_when_the_database_is_not(tmp_path):
    path = tmp_path / "legacy.db"
    make_legacy(path)
    os.chmod(path, 0o644)

    db.connect(path).close()

    assert stat.S_IMODE(Path(f"{path}.v0.bak").stat().st_mode) == 0o600


def test_a_failing_backup_leaves_nothing_behind_and_the_next_start_works(tmp_path):
    path = tmp_path / "legacy.db"
    make_legacy(path)

    # The copy is complete and sitting in the .partial file when this fails.
    with (
        mock.patch("spanlight.server.migrations.os.replace", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        db.connect(path)

    assert sorted(p.name for p in tmp_path.iterdir() if p.name != "legacy.db") == []
    assert version(path) == 0

    db.connect(path).close()
    assert version(Path(f"{path}.v0.bak")) == 0


def test_an_existing_backup_is_not_overwritten(tmp_path):
    path = tmp_path / "legacy.db"
    make_legacy(path)
    Path(f"{path}.v0.bak").write_bytes(b"an older copy the operator kept")

    db.connect(path).close()

    assert Path(f"{path}.v0.bak").read_bytes() == b"an older copy the operator kept"


def test_rows_the_rebuild_would_reject_fail_with_counts_and_the_fix(tmp_path):
    path = tmp_path / "legacy.db"
    make_legacy(path)
    raw = sqlite3.connect(path)
    raw.execute("UPDATE spans SET kind = 'LLM' WHERE id IN ('s1-1', 's1-3')")
    raw.execute("UPDATE spans SET status = 'timeout' WHERE id = 's2-2'")
    raw.commit()
    before = dump(raw, "SELECT * FROM spans ORDER BY trace_id, id")
    raw.close()

    with pytest.raises(RuntimeError) as failure:
        db.connect(path)

    message = str(failure.value)
    assert "2 spans have a kind outside" in message
    assert "1 have a status outside" in message
    assert "0 belong to no trace" in message
    assert f"{path}.v0.bak" in message
    assert "UPDATE spans SET kind" in message
    assert version(path) == 1, "the baseline committed, the rebuild did not"
    raw = sqlite3.connect(path)
    assert dump(raw, "SELECT * FROM spans ORDER BY trace_id, id") == before
    raw.close()

    # Following the message's advice gets the database through.
    raw = sqlite3.connect(path)
    kinds = "('llm', 'retrieval', 'tool', 'other')"
    raw.execute(f"UPDATE spans SET kind = 'other' WHERE kind NOT IN {kinds}")
    raw.execute("UPDATE spans SET status = 'error' WHERE status NOT IN ('ok', 'error')")
    raw.commit()
    raw.close()
    db.connect(path).close()
    assert version(path) == LATEST


def test_orphan_spans_fail_the_rebuild_with_a_count(tmp_path):
    path = tmp_path / "legacy.db"
    make_legacy(path)
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA foreign_keys = OFF")
    raw.execute("INSERT INTO spans (id, trace_id, name, started_at) VALUES ('o', 'gone', 'n', 1)")
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()

    with pytest.raises(RuntimeError, match="1 belong to no trace"):
        db.connect(path)

    assert version(path) == 1


def test_orphan_spans_in_a_legacy_database_are_reported_as_pre_existing(tmp_path):
    path = tmp_path / "legacy.db"
    make_legacy(path)
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA foreign_keys = OFF")
    raw.execute("INSERT INTO spans (id, trace_id, name, started_at) VALUES ('o', 'gone', 'n', 1)")
    raw.commit()
    raw.close()

    with pytest.raises(RuntimeError, match="1 pre-existing foreign key violations"):
        db.connect(path)

    assert version(path) == 0


def test_opening_waits_for_a_migration_in_progress_longer_than_the_busy_timeout(tmp_path):
    path = tmp_path / "legacy.db"
    make_legacy(path)
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    opened = []
    errors = []

    def open_it():
        try:
            opened.append(db.connect(path, busy_timeout_ms=50))
        except Exception as e:
            errors.append(e)

    thread = threading.Thread(target=open_it)
    thread.start()
    time.sleep(0.5)  # ten times the request busy timeout
    holder.execute("ROLLBACK")
    holder.close()
    thread.join(timeout=30)

    assert errors == []
    assert opened[0].execute("PRAGMA user_version").fetchone()[0] == LATEST
    opened[0].close()

import sqlite3
import stat
import threading
import time

import pytest
from fastapi.testclient import TestClient

from spanlight.server import create_app, db

# db.ingest takes the dicts the API produces, with every column present.
TRACE = {
    "id": "t1", "name": "req", "started_at": 1000.0, "ended_at": None,
    "status": "ok", "session": None, "metadata": {},
}


def trace(id):
    return {**TRACE, "id": id}


def span(id, trace_id):
    return {
        "id": id, "trace_id": trace_id, "parent_id": None, "name": "n", "kind": "other",
        "started_at": 1.0, "ended_at": None, "status": "ok", "error": None, "model": None,
        "input_tokens": None, "output_tokens": None, "cost_usd": None, "input": None,
        "output": None, "attributes": {},
    }


def ids(database):
    with database.read() as conn:
        return {r["id"] for r in conn.execute("SELECT id FROM traces")}


def test_a_paused_writer_blocks_the_next_one_until_it_exits(tmp_path):
    database = db.Database(tmp_path / "lock.db")
    inside, release, second_done = threading.Event(), threading.Event(), threading.Event()

    def first():
        with database.write() as conn:
            db.ingest(conn, [trace("a")], [])
            inside.set()
            assert release.wait(10)

    def second():
        with database.write() as conn:
            db.ingest(conn, [trace("b")], [])
        second_done.set()

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    threads[0].start()
    assert inside.wait(10)
    threads[1].start()
    assert not second_done.wait(0.3), "the second writer ran inside the first one's batch"
    release.set()
    for t in threads:
        t.join(10)

    assert second_done.is_set()
    assert ids(database) == {"a", "b"}
    database.close()


def test_a_failed_batch_rolls_back_only_itself(tmp_path):
    database = db.Database(tmp_path / "rollback.db")
    inside, release = threading.Event(), threading.Event()
    errors = []

    def failing():
        try:
            with database.write() as conn:
                db.ingest(conn, [trace("half-written")], [])
                inside.set()
                assert release.wait(10)
                raise ValueError("batch failed")
        except ValueError as e:
            errors.append(e)

    def neighbour():
        with database.write() as conn:
            db.ingest(conn, [trace("neighbour")], [])

    threads = [threading.Thread(target=failing), threading.Thread(target=neighbour)]
    threads[0].start()
    assert inside.wait(10)
    threads[1].start()
    release.set()
    for t in threads:
        t.join(10)

    assert len(errors) == 1
    assert ids(database) == {"neighbour"}
    database.close()


def test_a_batch_that_fails_midway_stores_nothing(tmp_path):
    database = db.Database(tmp_path / "atomic.db")
    orphan = span("s", "missing")

    with pytest.raises(sqlite3.IntegrityError), database.write() as conn:
        db.ingest(conn, [trace("kept?")], [orphan])

    assert ids(database) == set()
    database.close()


def test_a_read_can_run_inside_a_write(tmp_path):
    database = db.Database(tmp_path / "reentrant.db")
    with database.write() as conn:
        db.ingest(conn, [trace("a")], [])
        with database.read() as inner:
            assert inner.execute("SELECT COUNT(*) FROM traces").fetchone()[0] == 1
    database.close()


def test_a_busy_database_is_a_retryable_503(tmp_path, settings):
    path = tmp_path / "busy.db"
    app = create_app(db_path=str(path), settings=settings, busy_timeout_ms=50)
    payload = {"traces": [{k: v for k, v in TRACE.items() if v is not None}], "spans": []}
    with TestClient(app) as client:
        other_process = sqlite3.connect(path, isolation_level=None)
        other_process.execute("BEGIN IMMEDIATE")

        busy = client.post("/api/ingest", json=payload)
        # WAL readers are not blocked by the writer.
        read = client.get("/api/traces")

        other_process.execute("COMMIT")
        other_process.close()
        retried = client.post("/api/ingest", json=payload)

    assert busy.status_code == 503
    assert busy.headers["Retry-After"] == "1"
    assert read.status_code == 200
    assert retried.status_code == 202


def test_ingest_applies_column_defaults_to_partial_dicts(tmp_path):
    database = db.Database(tmp_path / "partial.db")
    with database.write() as conn:
        db.ingest(
            conn,
            [{"id": "t", "name": "req", "started_at": 1.0}],
            [{"id": "s", "trace_id": "t", "name": "call", "started_at": 1.0}],
        )
    with database.read() as conn:
        t = db.get_trace(conn, "t")
    assert (t["status"], t["metadata"], t["ended_at"], t["session"]) == ("ok", {}, None, None)
    assert (t["spans"][0]["kind"], t["spans"][0]["status"], t["spans"][0]["attributes"]) == (
        "other", "ok", {}
    )
    database.close()


@pytest.mark.parametrize(
    ("traces", "spans", "field"),
    [
        ([{"id": "t", "started_at": 1.0}], [], "name"),
        ([{"name": "n", "started_at": 1.0}], [], "id"),
        ([], [{"id": "s", "trace_id": "t", "name": "n"}], "started_at"),
        ([], [{"id": "s", "name": "n", "started_at": 1.0}], "trace_id"),
    ],
)
def test_ingest_rejects_a_missing_required_field_and_stores_nothing(
    tmp_path, traces, spans, field
):
    database = db.Database(tmp_path / "required.db")
    good = [trace("kept?")]

    with (
        pytest.raises(ValueError, match=f"missing required field '{field}'"),
        database.write() as conn,
    ):
        db.ingest(conn, [*good, *traces], spans)

    assert ids(database) == set()
    database.close()


def explain(conn, where, params):
    sql = db.LIST_TRACES_SQL.format(where=where)
    return conn.execute("EXPLAIN QUERY PLAN " + sql, [*params, 10]).fetchall()


@pytest.mark.parametrize(
    ("where", "params"),
    [
        ("", []),
        ("WHERE started_at < ?", [1.0]),
        ("WHERE (started_at, id) < (?, ?)", [1.0, "x"]),
        ("WHERE (started_at, id) < (?, ?) AND name LIKE ?", [1.0, "x", "%a%"]),
    ],
)
def test_the_real_trace_list_query_picks_its_page_from_the_paging_index(tmp_path, where, params):
    database = db.Database(tmp_path / "plan.db")
    with database.read() as conn:
        plan = explain(conn, where, params)
    database.close()

    traces_access = [r for r in plan if "traces_started_id_idx" in r["detail"]]
    assert traces_access, [r["detail"] for r in plan]
    # Everything the page subquery does is a child of the same co-routine; the
    # outer group/sort over at most `limit` traces is allowed to use a temp tree.
    subquery = traces_access[0]["parent"]
    assert [r["detail"] for r in plan if r["parent"] == subquery and "TEMP" in r["detail"]] == []


def test_nested_write_is_refused_without_touching_the_outer_transaction(tmp_path):
    database = db.Database(tmp_path / "nested.db")
    with database.write() as conn:
        db.ingest(conn, [trace("outer")], [])
        with pytest.raises(RuntimeError, match="cannot be nested"), database.write():
            pass
        db.ingest(conn, [trace("still-in-the-outer-transaction")], [])
    assert ids(database) == {"outer", "still-in-the-outer-transaction"}
    # The failed attempt did not leave the flag set.
    with database.write() as conn:
        db.ingest(conn, [trace("after")], [])
    database.close()


@pytest.mark.parametrize("path", [":memory:", ""])
def test_a_database_without_a_file_is_refused_up_front(path):
    with pytest.raises(ValueError, match="needs a database file"):
        db.connect(path)


def test_a_read_only_database_file_fails_fast(tmp_path):
    path = tmp_path / "readonly.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE traces (id TEXT)")
    raw.commit()
    raw.close()
    path.chmod(0o400)

    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        db.connect(path, busy_timeout_ms=10_000)

    assert time.monotonic() - started < 2


def test_waiting_for_the_in_process_lock_is_bounded_by_the_busy_timeout(tmp_path, settings):
    app = create_app(db_path=str(tmp_path / "wait.db"), settings=settings, busy_timeout_ms=100)
    payload = {"traces": [{"id": "t", "name": "n", "started_at": 1.0}], "spans": []}
    with TestClient(app) as client:
        holding, release = threading.Event(), threading.Event()

        def slow_reader():
            with app.state.db.read():
                holding.set()
                release.wait(10)

        thread = threading.Thread(target=slow_reader)
        thread.start()
        assert holding.wait(10)
        started = time.monotonic()
        ingest = client.post("/api/ingest", json=payload)
        health = client.get("/healthz")
        elapsed = time.monotonic() - started
        release.set()
        thread.join(10)
        recovered = client.post("/api/ingest", json=payload)

    assert (ingest.status_code, health.status_code) == (503, 503)
    assert ingest.headers["Retry-After"] == "1"
    assert elapsed < 3
    assert recovered.status_code == 202


def test_before_id_without_before_is_a_bad_request(client):
    r = client.get("/api/traces", params={"before_id": "x"})
    assert r.status_code == 400
    assert r.json()["error"] == {
        "code": "invalid_body",
        "message": "before_id requires before",
        "field": "before_id",
    }


@pytest.mark.parametrize("calls", [1, 2, 3, 4, 7, 10, 21, 100])
def test_model_stats_percentiles_are_nearest_rank_over_the_sorted_latencies(tmp_path, calls):
    database = db.Database(tmp_path / "stats.db")
    latencies = [((i * 37) % calls + 1) / 10 for i in range(calls)]
    llm = [
        {**span(f"s{i}", "t"), "kind": "llm", "model": "m", "started_at": 100.0 + i,
         "ended_at": 100.0 + i + latency, "input_tokens": 1, "output_tokens": 2, "cost_usd": 0.5}
        for i, latency in enumerate(latencies)
    ]
    with database.write() as conn:
        db.ingest(conn, [trace("t")], llm)
    with database.read() as conn:
        (stats,) = db.model_stats(conn)
    database.close()

    ordered = sorted(latencies)
    p50 = ordered[db._nearest_rank_index(calls, 0.50)]
    p95 = ordered[db._nearest_rank_index(calls, 0.95)]
    assert stats["calls"] == calls
    assert (stats["input_tokens"], stats["output_tokens"]) == (calls, 2 * calls)
    assert stats["cost_usd"] == pytest.approx(0.5 * calls)
    assert stats["latency_p50"] == pytest.approx(p50)
    assert stats["latency_p95"] == pytest.approx(p95)


def test_a_new_database_and_its_wal_files_are_private(tmp_path):
    path = tmp_path / "private.db"
    database = db.Database(path)
    with database.write() as conn:
        db.ingest(conn, [trace("a")], [])

    modes = {p.name: stat.S_IMODE(p.stat().st_mode) for p in tmp_path.glob("private.db*")}
    database.close()

    assert modes["private.db"] == 0o600
    assert set(modes) >= {"private.db-wal", "private.db-shm"}
    assert set(modes.values()) == {0o600}


def test_an_existing_database_keeps_its_mode_but_a_readable_one_is_reported(tmp_path, caplog):
    path = tmp_path / "shared.db"
    db.connect(path).close()
    path.chmod(0o644)
    caplog.clear()

    with caplog.at_level("WARNING", logger="spanlight.server.db"):
        db.connect(path).close()

    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert any("readable by other users" in r.message for r in caplog.records)


def test_a_private_existing_database_logs_nothing(tmp_path, caplog):
    path = tmp_path / "mine.db"
    db.connect(path).close()
    caplog.clear()
    with caplog.at_level("WARNING", logger="spanlight.server.db"):
        db.connect(path).close()
    assert caplog.records == []

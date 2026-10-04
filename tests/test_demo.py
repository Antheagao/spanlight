from spanlight.demo import seed
from spanlight.server import db


def test_seed_is_deterministic_and_well_formed(tmp_path):
    path = str(tmp_path / "demo.db")
    n_traces, n_spans = seed(path, days=3, traces=50, seed_value=7)
    assert n_traces == 50
    assert n_spans >= 50

    conn = db.connect(path)
    traces = db.list_traces(conn, limit=500)
    assert len(traces) == 50

    stats = db.model_stats(conn)
    assert stats, "demo data must include LLM spans"
    for s in stats:
        assert s["cost_usd"] > 0
        assert s["latency_p95"] >= s["latency_p50"] > 0

    detail = db.get_trace(conn, traces[0]["id"])
    assert detail["spans"], "every demo trace carries spans"
    for span in detail["spans"]:
        assert span["ended_at"] >= span["started_at"]
    conn.close()

    # Same seed, same database content counts.
    again = str(tmp_path / "demo2.db")
    assert seed(again, days=3, traces=50, seed_value=7) == (n_traces, n_spans)

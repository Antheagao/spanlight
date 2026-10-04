import pytest
from fastapi.testclient import TestClient

from spanlight.server import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def trace(id="t1", name="chat-request", started_at=1000.0, **kw):
    return {"id": id, "name": name, "started_at": started_at, **kw}


def span(id="s1", trace_id="t1", name="call", started_at=1000.0, **kw):
    return {"id": id, "trace_id": trace_id, "name": name, "started_at": started_at, **kw}


def llm_span(id, trace_id, started_at, latency, model="haiku", cost=0.001, **kw):
    return span(
        id=id,
        trace_id=trace_id,
        name=f"llm:{model}",
        kind="llm",
        started_at=started_at,
        ended_at=started_at + latency,
        model=model,
        input_tokens=100,
        output_tokens=50,
        cost_usd=cost,
        **kw,
    )


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_ingest_and_detail_roundtrip(client):
    r = client.post(
        "/api/ingest",
        json={
            "traces": [trace(metadata={"user": "demo"})],
            "spans": [
                llm_span("s1", "t1", 1000.0, 1.5, input="hi", output="hello"),
                span(id="s2", kind="tool", attributes={"tool": "search"}),
            ],
        },
    )
    assert r.status_code == 202
    assert r.json() == {"traces": 1, "spans": 2}

    detail = client.get("/api/traces/t1").json()
    assert detail["name"] == "chat-request"
    assert detail["metadata"] == {"user": "demo"}
    assert [s["id"] for s in detail["spans"]] == ["s1", "s2"]
    assert detail["spans"][0]["input"] == "hi"
    assert detail["spans"][1]["attributes"] == {"tool": "search"}


def test_ingest_is_an_upsert(client):
    client.post("/api/ingest", json={"traces": [trace()], "spans": []})
    client.post(
        "/api/ingest",
        json={"traces": [trace(ended_at=1002.0, status="error")], "spans": []},
    )
    detail = client.get("/api/traces/t1").json()
    assert detail["ended_at"] == 1002.0
    assert detail["status"] == "error"


def test_trace_not_found(client):
    assert client.get("/api/traces/nope").status_code == 404


def test_list_aggregates_and_pagination(client):
    traces = [trace(id=f"t{i}", started_at=1000.0 + i) for i in range(5)]
    spans = [llm_span(f"s{i}", f"t{i}", 1000.0 + i, 0.5) for i in range(5)]
    client.post("/api/ingest", json={"traces": traces, "spans": spans})

    page = client.get("/api/traces", params={"limit": 3}).json()
    assert [t["id"] for t in page["traces"]] == ["t4", "t3", "t2"]
    assert page["traces"][0]["llm_calls"] == 1
    assert page["traces"][0]["input_tokens"] == 100
    assert page["traces"][0]["cost_usd"] == pytest.approx(0.001)
    assert page["next_before"] == 1002.0

    rest = client.get(
        "/api/traces", params={"limit": 3, "before": page["next_before"]}
    ).json()
    assert [t["id"] for t in rest["traces"]] == ["t1", "t0"]
    assert rest["next_before"] is None


def test_list_search(client):
    client.post(
        "/api/ingest",
        json={"traces": [trace(id="a", name="chat"), trace(id="b", name="eval-run")], "spans": []},
    )
    found = client.get("/api/traces", params={"q": "eval"}).json()["traces"]
    assert [t["id"] for t in found] == ["b"]


def test_model_stats_percentiles_and_errors(client):
    latencies = [0.1, 0.2, 0.3, 0.4, 1.0]
    spans = [
        llm_span(f"s{i}", "t1", 1000.0 + i, latency)
        for i, latency in enumerate(latencies)
    ]
    spans[-1]["status"] = "error"
    spans.append(llm_span("cheap", "t1", 1000.0, 0.05, model="gpt-mini", cost=0.0001))
    client.post("/api/ingest", json={"traces": [trace()], "spans": spans})

    stats = {s["model"]: s for s in client.get("/api/stats/models").json()}
    haiku = stats["haiku"]
    assert haiku["calls"] == 5
    assert haiku["errors"] == 1
    assert haiku["cost_usd"] == pytest.approx(0.005)
    assert haiku["latency_p50"] == pytest.approx(0.3)
    assert haiku["latency_p95"] == pytest.approx(1.0)
    assert stats["gpt-mini"]["calls"] == 1


def test_timeseries_buckets(client):
    spans = [
        llm_span("s1", "t1", 0.0, 0.1),
        llm_span("s2", "t1", 10.0, 0.1),
        llm_span("s3", "t1", 70.0, 0.1, status="error"),
    ]
    client.post("/api/ingest", json={"traces": [trace()], "spans": spans})
    buckets = client.get(
        "/api/stats/timeseries", params={"since": 0, "bucket_seconds": 60}
    ).json()
    assert [(b["bucket"], b["calls"], b["errors"]) for b in buckets] == [
        (0, 2, 0),
        (60, 1, 1),
    ]
    assert buckets[0]["tokens"] == 300


def test_validation_rejects_bad_payloads(client):
    bad = span(input_tokens=-1)
    assert client.post("/api/ingest", json={"traces": [], "spans": [bad]}).status_code == 422
    assert client.post("/api/ingest", json={"spans": [{"id": "x"}]}).status_code == 422

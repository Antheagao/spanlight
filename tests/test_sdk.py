import pytest
from fastapi.testclient import TestClient

from spanlight.sdk import DEFAULT_PRICES, Spanlight, estimate_cost
from spanlight.server import create_app


@pytest.fixture()
def capture(monkeypatch):
    """A client whose sends are captured instead of hitting the network."""
    client = Spanlight(enabled=True, flush_interval=3600)
    batches: list[tuple[list[dict], list[dict]]] = []
    monkeypatch.setattr(client, "_send", lambda t, s: batches.append((t, s)))
    return client, batches


def flat(batches):
    traces = [t for ts, _ in batches for t in ts]
    spans = [s for _, ss in batches for s in ss]
    return traces, spans


def test_trace_reported_at_open_and_close(capture):
    client, batches = capture
    with client.trace("req", user="demo"):
        pass
    client.flush()
    traces, _ = flat(batches)
    assert [t["ended_at"] is None for t in traces] == [True, False]
    assert traces[0]["id"] == traces[1]["id"]
    assert traces[1]["metadata"] == {"user": "demo"}


def test_span_nesting_and_parent_ids(capture):
    client, batches = capture
    with (
        client.trace("req") as t,
        t.span("outer", kind="tool") as outer,
        outer.span("inner"),
    ):
        pass
    client.flush()
    _, spans = flat(batches)
    by_name = {s["name"]: s for s in spans}
    assert by_name["outer"]["parent_id"] is None
    assert by_name["inner"]["parent_id"] == by_name["outer"]["id"]
    assert by_name["inner"]["trace_id"] == by_name["outer"]["trace_id"]


def test_llm_span_records_usage_and_cost(capture):
    client, batches = capture
    with client.trace("req") as t, t.llm_span("answer", model="claude-haiku-4-5") as s:
        s.record_usage(input_tokens=1_000_000, output_tokens=0, input_text="hi")
    client.flush()
    _, spans = flat(batches)
    assert spans[0]["cost_usd"] == pytest.approx(1.00)
    assert spans[0]["input"] == "hi"
    assert spans[0]["kind"] == "llm"


def test_exception_marks_span_and_trace_error_and_reraises(capture):
    client, batches = capture
    with pytest.raises(ValueError), client.trace("req") as t, t.span("boom") as _s:
        raise ValueError("no")
    client.flush()
    traces, spans = flat(batches)
    assert spans[0]["status"] == "error"
    assert "ValueError" in spans[0]["error"]
    assert traces[-1]["status"] == "error"


def test_disabled_client_sends_nothing(capture):
    client, batches = capture
    client._enabled = False
    with client.trace("req"):
        pass
    client.flush()
    assert batches == []


def test_estimate_cost_prefix_match_and_unknowns():
    dated = estimate_cost("claude-haiku-4-5-20251001", 1_000_000, 1_000_000)
    base = estimate_cost("claude-haiku-4-5", 1_000_000, 1_000_000)
    assert dated == base == pytest.approx(6.00)
    assert estimate_cost("some-unknown-model", 10, 10) is None
    assert estimate_cost("claude-haiku-4-5", None, 10) is None
    assert estimate_cost("mine", 1_000_000, 0, prices={"mine": (3.0, 1.0)}) == pytest.approx(3.0)
    assert set(DEFAULT_PRICES) >= {"claude-haiku-4-5", "gpt-5.6-luna", "gemini-3.5-flash-lite"}


def test_sdk_payloads_are_accepted_by_the_server(tmp_path, monkeypatch):
    """End to end without the network: everything the SDK emits must ingest
    cleanly and read back through the server API."""
    sdk = Spanlight(enabled=True, flush_interval=3600)
    batches = []
    monkeypatch.setattr(sdk, "_send", lambda t, s: batches.append((t, s)))

    with sdk.trace("chat-request", user="demo") as t:
        with t.llm_span("answer", model="claude-haiku-4-5") as s:
            s.record_usage(input_tokens=420, output_tokens=180, output_text="hello")
        with t.span("lookup", kind="retrieval", index="docs"):
            pass
    sdk.flush()
    traces, spans = flat(batches)

    app = create_app(db_path=str(tmp_path / "e2e.db"))
    with TestClient(app) as http:
        r = http.post("/api/ingest", json={"traces": traces, "spans": spans})
        assert r.status_code == 202
        detail = http.get(f"/api/traces/{traces[0]['id']}").json()
        assert detail["ended_at"] is not None
        assert {s["kind"] for s in detail["spans"]} == {"llm", "retrieval"}
        listed = http.get("/api/traces").json()["traces"][0]
        assert listed["llm_calls"] == 1
        assert listed["cost_usd"] == pytest.approx(420 * 1.00 / 1e6 + 180 * 5.00 / 1e6)

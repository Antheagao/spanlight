import asyncio
import dataclasses
import json

import pytest
from fastapi.testclient import TestClient

from spanlight.server import app as app_module
from spanlight.server import create_app, db
from spanlight.server.guard import SECURITY_HEADERS, Guard
from spanlight.server.settings import Settings, SettingsError

TOKEN = "t" * 40
BODY = {"traces": [{"id": "t1", "name": "n", "started_at": 1.0}], "spans": []}


def with_settings(tmp_path, settings, **changes):
    app = create_app(
        db_path=str(tmp_path / "s.db"), settings=dataclasses.replace(settings, **changes)
    )
    return TestClient(app, raise_server_exceptions=False)


def post(client, **kwargs):
    return client.post("/api/ingest", **kwargs)


def error_code(response):
    return response.json()["error"]["code"]


# -- Host ------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["evil.example", "evil.example:4318", "localhost.evil.example", "localhost@evil.example",
     "127.0.0.2", "0.0.0.0:4318", "", "localhost:4318:x", "[::1]junk", "localhost:port"],
)
def test_a_foreign_host_is_refused_on_reads_and_writes(client, host):
    assert (client.get("/api/traces", headers={"host": host}).status_code) == 400
    r = post(client, json=BODY, headers={"host": host})
    assert (r.status_code, error_code(r)) == (400, "invalid_host")
    assert client.get("/api/stats/models", headers={"host": host}).status_code == 400


@pytest.mark.parametrize("host", ["localhost:5173", "LOCALHOST:4318", "127.0.0.1", "[::1]:4318"])
def test_loopback_hosts_are_allowed_whatever_the_port(client, host):
    assert client.get("/api/traces", headers={"host": host}).status_code == 200


def test_allowed_hosts_can_be_extended_from_the_environment(tmp_path):
    settings = Settings.from_env({"SPANLIGHT_ALLOWED_HOSTS": "host.docker.internal, Other.Name"})
    app = create_app(db_path=str(tmp_path / "h.db"), settings=settings)
    with TestClient(app) as client:
        docker = {"host": "host.docker.internal:4318"}
        assert client.get("/api/traces", headers=docker).status_code == 200
        assert client.get("/api/traces", headers={"host": "other.name"}).status_code == 200
        assert client.get("/api/traces", headers={"host": "evil.example"}).status_code == 400


def test_healthz_ignores_the_host_but_nothing_else_does(client):
    probe = {"host": "10.1.2.3:4318"}
    assert client.get("/healthz", headers=probe).json() == {"ok": True}
    assert client.post("/healthz", json={}, headers=probe).status_code == 400
    assert client.get("/healthz/", headers=probe).status_code == 400
    assert client.get("/api/traces", headers=probe).status_code == 400


# -- Origin ----------------------------------------------------------------


@pytest.mark.parametrize(
    "origin",
    ["https://evil.example", "null", "http://localhost:9999", "http://evil.example:4318",
     "http://localhost:4318@evil.example", "ftp://localhost:4318", "http://localhost:notaport",
     "http://localhost:4318", "http://localhost:5173", "http://testserver:4318"],
)
def test_a_foreign_origin_cannot_write(client, origin):
    # Host is `testserver`, so even loopback origins on other hosts are foreign.
    r = post(client, json=BODY, headers={"origin": origin})
    assert (r.status_code, error_code(r)) == (403, "forbidden_origin")
    assert client.get("/api/traces").json()["traces"] == []


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        ("testserver", "http://testserver"),
        ("localhost:4318", "http://localhost:4318"),
        ("127.0.0.1:4318", "http://127.0.0.1:4318"),
        ("LOCALHOST:4318", "http://localhost:4318"),
        ("[::1]:4318", "http://[::1]:4318"),
    ],
)
def test_an_origin_naming_the_requests_own_host_and_port_can_write(client, host, origin):
    r = post(client, json=BODY, headers={"host": host, "origin": origin})
    assert r.status_code == 202


def test_default_ports_work_without_being_the_bind_port(tmp_path, settings):
    with with_settings(tmp_path, settings, port=80) as client:
        r = post(client, json=BODY, headers={"host": "localhost", "origin": "http://localhost"})
        assert r.status_code == 202


def test_behind_tls_the_origin_matches_the_hosts_default_port(tmp_path, settings):
    hosts = (*settings.allowed_hosts, "spanlight.corp")
    corp = dataclasses.replace(settings, allowed_hosts=hosts)
    app = create_app(db_path=str(tmp_path / "tls.db"), settings=corp)
    with TestClient(app, base_url="https://spanlight.corp") as client:
        ok = post(client, json=BODY, headers={"origin": "https://spanlight.corp"})
        wrong_scheme = post(client, json=BODY, headers={"origin": "http://spanlight.corp"})
        wrong_port = post(client, json=BODY, headers={"origin": "https://spanlight.corp:8443"})
        other_site = post(client, json=BODY, headers={"origin": "https://evil.example"})
    assert ok.status_code == 202
    assert [r.status_code for r in (wrong_scheme, wrong_port, other_site)] == [403, 403, 403]


def test_no_origin_can_write(client):
    assert post(client, json=BODY).status_code == 202


def test_dev_origins_are_off_by_default_and_opt_in(tmp_path, settings):
    dev = {"origin": "http://localhost:5173"}
    preflight = {**dev, "access-control-request-method": "POST"}
    with with_settings(tmp_path, settings) as off:
        assert post(off, json=BODY, headers=dev).status_code == 403
        refused = off.options("/api/ingest", headers=preflight)
        assert "access-control-allow-origin" not in refused.headers
    with with_settings(tmp_path, settings, dev_origins=("http://localhost:5173",)) as on:
        assert post(on, json=BODY, headers=dev).status_code == 202
        allowed = on.options("/api/ingest", headers=preflight).headers
        assert allowed["access-control-allow-origin"] == "http://localhost:5173"
        other = {**preflight, "origin": "http://localhost:5174"}
        assert "access-control-allow-origin" not in on.options("/api/ingest", headers=other).headers
        assert post(on, json=BODY, headers={"origin": "http://localhost:5174"}).status_code == 403


def test_reads_do_not_check_the_origin(client):
    assert client.get("/api/traces", headers={"origin": "https://evil.example"}).status_code == 200


# -- Content type ----------------------------------------------------------


@pytest.mark.parametrize(
    "content_type",
    ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x",
     "application/jsonp", "text/json", None],
)
def test_a_write_without_an_allowed_content_type_is_refused_and_stores_nothing(
    client, content_type
):
    headers = {"content-type": content_type} if content_type else {}
    r = post(client, content=json.dumps(BODY), headers=headers)
    assert (r.status_code, error_code(r)) == (415, "unsupported_media_type")
    assert client.get("/api/traces").json()["traces"] == []


def test_a_bodyless_write_is_routed_instead_of_refused_for_its_content_type(client):
    assert client.delete("/api/traces/t1").status_code == 405
    r = client.delete("/api/nope")
    assert (r.status_code, error_code(r)) == (404, "not_found")
    r = client.patch("/healthz")
    assert (r.status_code, error_code(r)) == (405, "method_not_allowed")
    # With no body and no type, ingest reaches validation, not the type check.
    assert error_code(post(client)) == "invalid_body"


@pytest.mark.parametrize(
    "content_type", ["application/json", "application/json; charset=utf-8", "Application/JSON"]
)
def test_json_content_types_are_accepted(client, content_type):
    r = post(client, content=json.dumps(BODY), headers={"content-type": content_type})
    assert r.status_code == 202


# -- Token -----------------------------------------------------------------


def test_without_a_configured_token_nothing_is_required(client):
    assert post(client, json=BODY).status_code == 202


@pytest.fixture()
def locked(tmp_path, settings):
    with with_settings(tmp_path, settings, ingest_token=TOKEN) as client:
        yield client


def test_a_missing_or_wrong_token_is_401_with_a_challenge(locked):
    for headers in (
        {},
        {"authorization": f"Bearer {TOKEN}x"},
        {"authorization": f"Bearer {TOKEN[:-1]}"},
        {"authorization": f"Basic {TOKEN}"},
        {"authorization": TOKEN},
        {"authorization": "Bearer"},
        {"authorization": f"Bearer {TOKEN} extra"},
    ):
        r = post(locked, json=BODY, headers=headers)
        assert (r.status_code, error_code(r)) == (401, "unauthorized"), headers
        assert r.headers["www-authenticate"] == "Bearer"
        assert TOKEN not in r.text


@pytest.mark.parametrize(
    "authorization",
    [f"Bearer {TOKEN}", f"bearer {TOKEN}", f"BEARER   {TOKEN}  ", f"Bearer\t{TOKEN}"],
)
def test_the_right_token_is_accepted_whatever_the_scheme_case_or_spacing(locked, authorization):
    assert post(locked, json=BODY, headers={"authorization": authorization}).status_code == 202


def test_reads_need_no_token(locked):
    assert locked.get("/api/traces").status_code == 200
    assert locked.get("/healthz").status_code == 200


def test_the_token_is_checked_before_the_content_type(locked):
    r = post(locked, content="x", headers={"content-type": "text/plain"})
    assert r.status_code == 401


# -- Settings --------------------------------------------------------------


def test_defaults_are_loopback_with_no_token():
    s = Settings.from_env({})
    assert (s.host, s.port, s.ingest_token) == ("127.0.0.1", 4318, None)
    assert s.max_request_bytes == 16 * 1024 * 1024


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "127.0.0.2", "localhost", "LOCALHOST", "::1", "[::1]"]
)
def test_loopback_hosts_need_no_token(host):
    assert Settings.from_env({"SPANLIGHT_HOST": host}).host == host


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "::", "192.168.1.5", "spanlight.internal", "evil.localhost.x"]
)
def test_a_non_loopback_host_without_a_token_refuses_to_start(host):
    with pytest.raises(SettingsError, match="SPANLIGHT_INGEST_TOKEN is required"):
        Settings.from_env({"SPANLIGHT_HOST": host})
    with pytest.raises(SettingsError, match="SPANLIGHT_INGEST_TOKEN is required"):
        Settings.from_env({"SPANLIGHT_HOST": host, "SPANLIGHT_INGEST_TOKEN": ""})


def test_a_non_loopback_host_with_a_token_starts():
    s = Settings.from_env({"SPANLIGHT_HOST": "0.0.0.0", "SPANLIGHT_INGEST_TOKEN": TOKEN})
    assert s.ingest_token == TOKEN


@pytest.mark.parametrize(
    "token",
    ["short", "x" * 31, "x" * 16 + " " + "x" * 16, "x" * 32 + "\n", "x" * 31 + "\t",
     "x" * 31 + "\x00", "x" * 31 + "é", " " + "x" * 32],
)
def test_a_bad_token_fails_startup_naming_the_variable_and_never_echoing_it(token):
    with pytest.raises(SettingsError, match="SPANLIGHT_INGEST_TOKEN") as failure:
        Settings.from_env({"SPANLIGHT_INGEST_TOKEN": token})
    assert token.strip() not in str(failure.value) or token.strip() == ""


def test_a_32_character_token_is_accepted():
    assert Settings.from_env({"SPANLIGHT_INGEST_TOKEN": "x" * 32}).ingest_token == "x" * 32


@pytest.mark.parametrize(
    ("name", "value"),
    [("SPANLIGHT_PORT", "http"), ("SPANLIGHT_PORT", "0"), ("SPANLIGHT_PORT", "70000"),
     ("SPANLIGHT_PORT", "43.18"), ("SPANLIGHT_MAX_REQUEST_BYTES", "lots"),
     ("SPANLIGHT_MAX_REQUEST_BYTES", "0"), ("SPANLIGHT_HOST", "  ")],
)
def test_malformed_values_fail_startup_naming_the_variable(name, value):
    with pytest.raises(SettingsError, match=name):
        Settings.from_env({name: value})


# -- Body cap --------------------------------------------------------------


@pytest.fixture()
def small(tmp_path, settings):
    with with_settings(tmp_path, settings, max_request_bytes=1000) as client:
        yield client


def padded(size):
    """A valid ingest body of exactly `size` bytes."""
    trace = {"id": "t1", "name": "n", "started_at": 1.0, "metadata": {"p": ""}}
    shell = json.dumps({"traces": [trace]})
    body = shell.replace('"p": ""', '"p": "' + "x" * (size - len(shell)) + '"')
    assert len(body) == size
    return body.encode()


def test_a_body_over_the_cap_is_413_by_its_declared_length(small):
    r = post(small, content=b"x" * 1001, headers={"content-type": "application/json"})
    assert (r.status_code, error_code(r)) == (413, "payload_too_large")


def test_a_streamed_body_without_a_length_over_the_cap_is_413(small):
    def chunks():
        for _ in range(11):
            yield b"x" * 100

    r = post(small, content=chunks(), headers={"content-type": "application/json"})
    assert (r.status_code, error_code(r)) == (413, "payload_too_large")


def test_the_cap_is_checked_before_the_body_is_parsed(small):
    # 1001 bytes of invalid JSON: a parse error would be a 400.
    r = post(small, content=b"{" * 1001, headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_a_body_exactly_at_the_cap_is_accepted(small):
    r = post(small, content=padded(1000), headers={"content-type": "application/json"})
    assert r.status_code == 202
    r = post(small, content=padded(1001), headers={"content-type": "application/json"})
    assert r.status_code == 413


def run_guard(settings, headers, messages):
    """Drive the Guard with raw ASGI messages, as a server with a wrong or
    missing Content-Length would; returns (status, whether the app ran)."""
    ran = []
    sent = []

    async def inner(scope, receive, send):
        ran.append(True)
        while (await receive()).get("more_body"):
            pass
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    queue = [*messages, {"type": "http.disconnect"}]

    async def receive():
        return queue.pop(0)

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "method": "POST", "path": "/api/ingest",
        "headers": [(b"host", b"localhost"), (b"content-type", b"application/json"), *headers],
    }
    asyncio.run(Guard(inner, settings)(scope, receive, send))
    return sent[0]["status"], bool(ran)


def chunk(size, more=True):
    return {"type": "http.request", "body": b"x" * size, "more_body": more}


def test_a_lying_content_length_cannot_get_past_the_cap():
    settings = Settings(max_request_bytes=100)
    status, ran = run_guard(settings, [(b"content-length", b"10")], [chunk(60), chunk(60, False)])
    assert (status, ran) == (413, False)


def test_a_missing_content_length_cannot_get_past_the_cap():
    settings = Settings(max_request_bytes=100)
    status, ran = run_guard(settings, [], [chunk(60), chunk(30), chunk(11, False)])
    assert (status, ran) == (413, False)


def test_a_body_within_the_cap_reaches_the_app_in_one_piece():
    settings = Settings(max_request_bytes=100)
    status, ran = run_guard(settings, [], [chunk(60), chunk(40, False)])
    assert (status, ran) == (204, True)


def test_a_declared_length_over_the_cap_is_refused_without_reading():
    settings = Settings(max_request_bytes=100)
    status, ran = run_guard(settings, [(b"content-length", b"101")], [])
    assert (status, ran) == (413, False)


# -- Error envelope --------------------------------------------------------


def test_malformed_json_is_400_invalid_body_without_a_field(client):
    r = post(client, content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 400
    error = r.json()["error"]
    assert error["code"] == "invalid_body"
    assert "field" not in error


def test_a_validation_error_never_echoes_the_input(client):
    secret = "my-secret-prompt"
    r = post(client, json={"spans": [{"id": secret, "kind": secret}]})
    assert r.status_code == 400
    assert secret not in r.text


@pytest.mark.parametrize("method", ["get", "post", "put", "delete"])
def test_unknown_api_routes_are_an_envelope_404(client, method):
    r = getattr(client, method)("/api/nope/deeper", headers={"content-type": "application/json"})
    assert (r.status_code, error_code(r)) == (404, "not_found")


def test_a_wrong_method_is_an_envelope_405_with_allow(client):
    r = client.post("/api/traces", json={})
    assert (r.status_code, error_code(r)) == (405, "method_not_allowed")
    assert "GET" in r.headers["allow"]


def test_an_unexpected_failure_is_an_opaque_envelope_500(client, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(db, "list_traces", boom)
    client = TestClient(client.app, raise_server_exceptions=False)
    r = client.get("/api/traces")
    assert (r.status_code, error_code(r)) == (500, "internal")
    assert "secret internal detail" not in r.text
    assert "Traceback" not in r.text
    assert r.headers["x-content-type-options"] == "nosniff"


def test_a_busy_database_is_an_envelope_503(tmp_path, settings):
    import sqlite3

    path = tmp_path / "busy.db"
    app = create_app(db_path=str(path), settings=settings, busy_timeout_ms=50)
    with TestClient(app) as client:
        other = sqlite3.connect(path, isolation_level=None)
        other.execute("BEGIN IMMEDIATE")
        r = post(client, json=BODY)
        other.execute("COMMIT")
        other.close()
    assert (r.status_code, error_code(r)) == (503, "database_busy")
    assert r.headers["retry-after"] == "1"


# -- Dashboard mount and headers ------------------------------------------


@pytest.fixture()
def with_dashboard(tmp_path, settings, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>spanlight</title>")
    monkeypatch.setattr(app_module, "DASHBOARD_DIST", dist)
    app = create_app(db_path=str(tmp_path / "d.db"), settings=settings)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_security_headers_are_on_every_response(with_dashboard):
    client = with_dashboard
    responses = [
        client.get("/"),
        client.get("/api/traces"),
        client.get("/healthz"),
        client.get("/api/traces/nope"),
        client.get("/nope"),
        client.get("/api/traces", headers={"host": "evil.example"}),
        client.post("/api/ingest", content="x", headers={"content-type": "text/plain"}),
        client.post("/api/traces", json={}),
        client.options("/api/traces", headers={
            "origin": "http://localhost:5173", "access-control-request-method": "GET"}),
    ]
    for r in responses:
        for name, value in SECURITY_HEADERS.items():
            assert r.headers[name] == value, (r.request.url, name)
    assert "frame-ancestors 'none'" in responses[0].headers["content-security-policy"]


def test_the_dashboard_is_served_and_its_404_is_json_not_html(with_dashboard):
    assert "<title>spanlight</title>" in with_dashboard.get("/").text
    r = with_dashboard.get("/missing.js")
    assert (r.status_code, error_code(r)) == (404, "not_found")
    assert r.headers["content-type"].startswith("application/json")


def test_a_foreign_host_cannot_read_the_dashboard(with_dashboard):
    assert with_dashboard.get("/", headers={"host": "evil.example"}).status_code == 400


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_the_interactive_docs_and_schema_are_not_served(client, path):
    # The CSP would blank the docs pages, and nothing else consumes the schema.
    r = client.get(path)
    assert (r.status_code, error_code(r)) == (404, "not_found")


# -- Dev origins and concurrency settings ---------------------------------


def test_dev_origins_are_normalized_and_empty_by_default():
    assert Settings.from_env({}).dev_origins == ()
    listed = " http://LOCALHOST:5173/ , https://dev.example,, "
    got = Settings.from_env({"SPANLIGHT_DEV_ORIGINS": listed}).dev_origins
    assert got == ("http://localhost:5173", "https://dev.example")


@pytest.mark.parametrize(
    "value",
    ["localhost:5173", "ftp://localhost", "http://", "http://localhost:5173/app",
     "http://localhost:notaport", "http://localhost?x=1", "*"],
)
def test_a_malformed_dev_origin_fails_startup_naming_the_variable(value):
    with pytest.raises(SettingsError, match="SPANLIGHT_DEV_ORIGINS"):
        Settings.from_env({"SPANLIGHT_DEV_ORIGINS": value})


def test_concurrency_has_a_default_and_is_validated():
    assert Settings.from_env({}).max_concurrency == 64
    assert Settings.from_env({"SPANLIGHT_MAX_CONCURRENCY": "8"}).max_concurrency == 8
    for bad in ("0", "many", "-1"):
        with pytest.raises(SettingsError, match="SPANLIGHT_MAX_CONCURRENCY"):
            Settings.from_env({"SPANLIGHT_MAX_CONCURRENCY": bad})


def test_the_server_is_started_with_the_concurrency_limit(monkeypatch, tmp_path):
    from spanlight.server import __main__ as entry

    started = {}
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kwargs: started.update(kwargs))
    monkeypatch.setenv("SPANLIGHT_DB", str(tmp_path / "main.db"))
    monkeypatch.setenv("SPANLIGHT_PORT", "4999")
    monkeypatch.setenv("SPANLIGHT_MAX_CONCURRENCY", "12")
    entry.main()
    assert started == {"host": "127.0.0.1", "port": 4999, "limit_concurrency": 12}

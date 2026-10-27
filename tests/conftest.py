import pytest
from fastapi.testclient import TestClient

from spanlight.server import create_app
from spanlight.server.settings import LOOPBACK_ALLOWED_HOSTS, Settings


@pytest.fixture()
def settings():
    # TestClient sends `Host: testserver`.
    return Settings(allowed_hosts=(*LOOPBACK_ALLOWED_HOSTS, "testserver"))


@pytest.fixture()
def client(tmp_path, settings):
    app = create_app(db_path=str(tmp_path / "test.db"), settings=settings)
    with TestClient(app) as c:
        yield c

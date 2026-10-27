import os

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck
from hypothesis import settings as hypothesis_settings

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


# Property tests run a fixed set of examples by default, so CI is repeatable and
# fast. `HYPOTHESIS_PROFILE=deep pytest` fuzzes much harder with fresh seeds.
_common = {
    "deadline": None,
    "suppress_health_check": [HealthCheck.too_slow, HealthCheck.data_too_large],
}
hypothesis_settings.register_profile("ci", derandomize=True, max_examples=50, **_common)
hypothesis_settings.register_profile("deep", derandomize=False, max_examples=3000, **_common)
hypothesis_settings.register_profile("soak", derandomize=False, max_examples=6000, **_common)
hypothesis_settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))

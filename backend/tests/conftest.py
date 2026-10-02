import pytest

from adstudio.api.app import create_app
from adstudio.core.config import Config
from adstudio.core.container import build_container
from adstudio.core.fakes import FakeLLM, FakeOCR
from adstudio.core.security import COOKIE_NAME, SessionAuth


@pytest.fixture
def config(tmp_path):
    return Config.load(tmp_path / "data", workers=1, heartbeat_s=0.2, stale_after_s=0.5)


@pytest.fixture
def container(config):
    c = build_container(config, start_workers=False, auto_llm=False)
    yield c
    c.stop()


@pytest.fixture
def client(config):
    from fastapi.testclient import TestClient

    c = build_container(config, auth=SessionAuth(), ocr=FakeOCR(), llm=FakeLLM(), start_workers=False)
    app = create_app(c)
    with TestClient(app, base_url="http://127.0.0.1") as tc:
        tc.cookies.set(COOKIE_NAME, c.auth.session_token)
        tc.container = c
        yield tc

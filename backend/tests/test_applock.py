import pytest
from fastapi.testclient import TestClient

from adstudio.api.app import create_app
from adstudio.core import container as cmod
from adstudio.core.applock import AppLock
from adstudio.core.config import Config
from adstudio.core.security import COOKIE_NAME

PASS = "open sesame"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def lk(client):
    clock = Clock()
    client.container.applock = AppLock(client.container.config.data_root, clock)
    client.clock = clock
    return client


def unlock(c, p=PASS):
    return c.post("/api/v1/lock/unlock", json={"passphrase": p})


def test_disabled_by_default_and_setup_requires_a_decent_passphrase(lk):
    assert lk.get("/api/v1/lock/status").json() == {"enabled": False, "locked": False, "idle_lock_minutes": 0, "retry_after": 0}
    assert lk.post("/api/v1/lock/setup", json={"passphrase": "abc"}).json()["code"] == "weak_passphrase"
    assert lk.post("/api/v1/lock/setup", json={}).json()["code"] == "invalid_body"
    st = lk.post("/api/v1/lock/setup", json={"passphrase": PASS}).json()
    assert st["enabled"] and not st["locked"]  # setting it does not lock you out of the current session
    assert lk.post("/api/v1/lock/setup", json={"passphrase": PASS}).json()["code"] == "already_set"
    assert PASS not in lk.container.applock.file.read_text() and lk.container.applock.file.read_text().count("$argon2id$") == 1


def test_locked_app_rejects_everything_but_status_and_unlock(lk):
    lk.post("/api/v1/lock/setup", json={"passphrase": PASS})
    assert lk.get("/api/v1/health").status_code == 200
    assert lk.post("/api/v1/lock/lock").json()["locked"] is True
    for method, path in (("get", "/api/v1/health"), ("get", "/api/v1/documents"), ("get", "/api/v1/settings"),
                         ("post", "/api/v1/search"), ("get", "/api/v1/events"), ("post", "/api/v1/backup")):
        r = getattr(lk, method)(path)
        assert r.status_code == 423 and r.json()["code"] == "locked", path
    assert lk.get("/api/v1/lock/status").json()["locked"] is True
    assert unlock(lk, "wrong").status_code == 401
    assert unlock(lk).json()["locked"] is False
    assert lk.get("/api/v1/documents").status_code == 200


def test_unlock_attempts_are_rate_limited_with_backoff(lk):
    lk.post("/api/v1/lock/setup", json={"passphrase": PASS})
    lk.post("/api/v1/lock/lock")
    assert [unlock(lk, "x").status_code for _ in range(3)] == [401, 401, 401]
    blocked = unlock(lk, PASS)  # even the right passphrase is refused during back-off
    assert blocked.status_code == 429 and blocked.json()["code"] == "rate_limited"
    assert lk.get("/api/v1/lock/status").json()["retry_after"] > 0
    lk.clock.t += 5
    assert unlock(lk, "x").status_code == 401  # 4th failure: longer wait
    lk.clock.t += 400
    assert unlock(lk).status_code == 200  # back-off expired; success resets the counter
    lk.post("/api/v1/lock/lock")
    assert unlock(lk, "x").status_code == 401 and lk.container.applock.failures == 1


def test_idle_timeout_locks_and_activity_keeps_it_open(lk):
    lk.post("/api/v1/lock/setup", json={"passphrase": PASS})
    assert lk.patch("/api/v1/settings", json={"security.idle_lock_minutes": 5}).status_code == 200
    lk.clock.t += 4 * 60
    assert lk.get("/api/v1/documents").status_code == 200  # activity at minute 4 resets the timer
    lk.clock.t += 4 * 60
    assert lk.get("/api/v1/documents").status_code == 200
    for _ in range(3):  # polling the status / events does NOT count as activity
        lk.clock.t += 2 * 60
        assert lk.get("/api/v1/lock/status").status_code == 200
    r = lk.get("/api/v1/documents")
    assert r.status_code == 423  # 6 idle minutes since the last real request
    assert unlock(lk).status_code == 200 and lk.get("/api/v1/documents").status_code == 200
    lk.patch("/api/v1/settings", json={"security.idle_lock_minutes": 0})
    lk.clock.t += 10 * 3600
    assert lk.get("/api/v1/documents").status_code == 200  # 0 = never


def test_disable_needs_the_passphrase_and_lock_survives_restart(lk, tmp_path):
    lk.post("/api/v1/lock/setup", json={"passphrase": PASS})
    assert lk.post("/api/v1/lock/disable", json={"passphrase": "nope"}).status_code == 401
    assert lk.container.applock.enabled
    cfg = lk.container.config
    again = AppLock(cfg.data_root)  # a restart reads lock.json: an enabled lock starts locked
    assert again.enabled and again.locked
    again.unlock(PASS)
    assert not again.locked
    assert lk.post("/api/v1/lock/disable", json={"passphrase": PASS}).json()["enabled"] is False
    assert not cfg.data_root.joinpath("lock.json").exists() and not AppLock(cfg.data_root).locked

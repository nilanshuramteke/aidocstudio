from fastapi.testclient import TestClient

from adstudio.api.app import create_app
from adstudio.core.container import build_container
from adstudio.core.security import COOKIE_NAME


def test_health(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["db"]["ok"] is True and body["db"]["schema_version"] >= 1
    assert body["providers"]["llm"]["configured"] is True  # fixture injects FakeLLM
    assert r.headers["x-content-type-options"] == "nosniff"


def test_api_requires_session_cookie(client):
    client.cookies.clear()
    r = client.get("/api/v1/health")
    assert r.status_code == 401
    assert r.json()["code"] == "unauthenticated"


def test_bad_origin_and_host_rejected(client):
    assert client.get("/api/v1/health", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/v1/health", headers={"Origin": "null"}).status_code == 403
    assert client.get("/api/v1/health", headers={"Origin": "http://127.0.0.1:5173"}).status_code == 200
    r = client.get("/api/v1/health", headers={"Host": "evil.example"})  # DNS rebinding
    assert r.status_code == 403 and r.json()["code"] == "bad_host"


def test_one_time_token_sets_cookie_once(config):
    c = build_container(config, start_workers=False, auto_llm=False)
    token = c.auth.issue_one_time()
    with TestClient(create_app(c), base_url="http://127.0.0.1") as tc:
        r = tc.get(f"/?t={token}", follow_redirects=False)
        assert r.status_code == 303
        assert COOKIE_NAME in r.headers["set-cookie"] and "httponly" in r.headers["set-cookie"].lower()
        assert "samesite=strict" in r.headers["set-cookie"].lower()
        assert tc.get("/api/v1/health").status_code == 200  # cookie jar kept the session
        assert tc.get(f"/?t={token}", follow_redirects=False).status_code == 403  # token is single-use


def test_settings_roundtrip_and_validation(client):
    assert client.get("/api/v1/settings").json()["review.auto_accept"] == 0.9
    r = client.patch("/api/v1/settings", json={"review.auto_accept": 0.95})
    assert r.json()["review.auto_accept"] == 0.95
    assert client.patch("/api/v1/settings", json={"nope": 1}).status_code == 400
    assert client.patch("/api/v1/settings", json={"ui.theme": 3}).json()["code"] == "invalid_setting"
    with client.container.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action='settings.update'").fetchone()[0] == 1


def test_jobs_endpoints(client):
    q = client.container.queue
    jid = q.enqueue("debug:sleep", {"seconds": 0})
    assert client.get(f"/api/v1/jobs/{jid}").json()["status"] == "queued"
    assert client.get("/api/v1/jobs?status=queued").json()["items"][0]["id"] == jid
    assert client.post(f"/api/v1/jobs/{jid}/cancel").json()["status"] == "cancelled"
    assert client.post(f"/api/v1/jobs/{jid}/retry").json()["status"] == "queued"
    assert client.get("/api/v1/jobs/missing").status_code == 404

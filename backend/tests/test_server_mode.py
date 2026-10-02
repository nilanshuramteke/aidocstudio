"""Opt-in server mode (Docker / LAN): fail-closed binding, reusable access token, restricted path import."""
from adstudio import main as mainmod
from adstudio.core.security import SessionAuth


def test_access_token_is_reusable_and_exact():
    a = SessionAuth()
    a.access_token = "x" * 32
    assert a.consume_one_time("x" * 32) and a.consume_one_time("x" * 32)  # not consumed
    assert not a.consume_one_time("x" * 31 + "y") and not a.consume_one_time("") and not a.consume_one_time("é" * 32)


def test_one_time_tokens_stay_single_use_and_no_default_access_token():
    a = SessionAuth()
    assert not a.consume_one_time("")  # no access token configured on loopback
    t = a.issue_one_time()
    assert a.consume_one_time(t) and not a.consume_one_time(t)


def test_loopback_detection_and_extra_hosts():
    assert all(mainmod._is_loopback(h) for h in ("127.0.0.1", "localhost", "::1"))
    assert not mainmod._is_loopback("0.0.0.0") and not mainmod._is_loopback("192.168.1.5")
    assert mainmod._extra_hosts(" NAS.local, 192.168.1.20 ,,") == {"nas.local", "192.168.1.20"}


def test_non_loopback_bind_refuses_to_start_without_a_strong_token(tmp_path, monkeypatch, capsys):
    argv = ["--data-dir", str(tmp_path), "--host", "0.0.0.0", "--no-browser"]
    monkeypatch.delenv("ADSTUDIO_ACCESS_TOKEN", raising=False)
    assert mainmod.run(argv) == 1
    assert "ADSTUDIO_ACCESS_TOKEN" in capsys.readouterr().err
    monkeypatch.setenv("ADSTUDIO_ACCESS_TOKEN", "too-short")
    assert mainmod.run(argv) == 1
    assert "ADSTUDIO_ACCESS_TOKEN" in capsys.readouterr().err


def test_import_paths_limited_to_allowed_roots_in_server_mode(client, tmp_path):
    inbox, outside = tmp_path / "inbox", tmp_path / "outside"
    inbox.mkdir()
    outside.mkdir()
    (inbox / "ok.txt").write_bytes(b"inside the inbox")
    (outside / "secret.txt").write_bytes(b"outside the inbox")
    try:
        (inbox / "link.txt").symlink_to(outside / "secret.txt")
    except OSError:
        pass  # symlinks need privileges on some Windows setups
    client.app.state.import_roots = [inbox.resolve()]
    res = client.post("/api/v1/documents/import-paths", json={"paths": [str(inbox), str(outside / "secret.txt")]}).json()["results"]
    by_status = [r["status"] for r in res]
    assert by_status.count("created") == 1  # only ok.txt; the symlink and the outside file are refused
    assert by_status.count("rejected") == len(res) - 1 and len(res) >= 2
    assert all("server mode" in (r.get("reason") or "") for r in res if r["status"] == "rejected")

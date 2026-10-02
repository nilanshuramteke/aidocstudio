import json
import os

import keyring
import pytest
from fastapi.testclient import TestClient
from keyring.backend import KeyringBackend

from adstudio.api.app import create_app
from adstudio.core import container as cmod
from adstudio.core.config import Config
from adstudio.core.errors import AppError
from adstudio.core.fakes import FakeLLM, FakeOCR
from adstudio.core.security import COOKIE_NAME
from adstudio.main import run as cli
from adstudio.ops import backup as bk
from adstudio.storage import crypt

from .test_documents import run_workers, settle, upload
from .test_extraction import INVOICE_TEXT, pdf_with_lines
from .test_search import ConceptEmbedding, drain, search, titles

PASS = "correct horse battery staple"
SECRET_WORD = "zxqvortexmarker"


@pytest.fixture(autouse=True)
def fast_kdf(monkeypatch):
    monkeypatch.setattr(crypt, "KDF", dict(time_cost=1, memory_cost=1024, parallelism=1))  # keep tests quick


class MemoryKeyring(KeyringBackend):
    priority = 99

    def __init__(self):
        self.store = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        self.store.pop((service, username), None)


@pytest.fixture
def kr():
    old = keyring.get_keyring()
    mem = MemoryKeyring()
    keyring.set_keyring(mem)
    yield mem
    keyring.set_keyring(old)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(crypt.ENV_PASSPHRASE, raising=False)
    monkeypatch.delenv("ADSTUDIO_NEW_PASSPHRASE", raising=False)


def app(config, key=None, emb=None):
    c = cmod.build_container(config, ocr=FakeOCR(), llm=FakeLLM(), embedding=emb, start_workers=False, key=key)
    tc = TestClient(create_app(c), base_url="http://127.0.0.1")
    tc.__enter__()
    tc.cookies.set(COOKIE_NAME, c.auth.session_token)
    tc.container = c
    return tc


def populated(config):
    tc = app(config, emb=ConceptEmbedding())
    ids = [upload(tc, "inv.pdf", pdf_with_lines(INVOICE_TEXT))["document_id"],
           upload(tc, "note.txt", f"the truck and the car {SECRET_WORD} were parked".encode())["document_id"]]
    run_workers(tc)
    for i in ids:
        settle(tc, i)
    drain(tc)
    tc.post("/api/v1/documents/bulk", json={"ids": ids[:1], "action": "tag", "params": {"tags": ["finance"]}})
    tc.container.stop()
    tc.__exit__(None, None, None)
    return ids


def raw_db(config) -> bytes:
    return config.db_path.read_bytes()


# ── key handling ──────────────────────────────────────────────
def test_kdf_and_verifier(tmp_path):
    (tmp_path / "db").mkdir()
    crypt.crypt_file(tmp_path).write_text(json.dumps({"enabled": True, "salt": "00" * 16, "kdf_params": crypt.KDF,
                                                      "verifier": crypt._verifier(crypt.derive_key(PASS, bytes(16)))}))
    k = crypt.key_from_passphrase(tmp_path, PASS)
    assert len(k) == 32 and k == crypt.derive_key(PASS, bytes(16)) and k != crypt.derive_key(PASS + "x", bytes(16))
    with pytest.raises(AppError) as e:
        crypt.key_from_passphrase(tmp_path, "wrong")
    assert e.value.code == "bad_passphrase"


def test_keychain_cache_env_and_prompt_order(tmp_path, kr, monkeypatch):
    cfg = Config.load(tmp_path / "data", workers=1)
    populated(cfg)
    crypt.encrypt_in_place(cfg.data_root, PASS)
    with pytest.raises(AppError) as e:
        crypt.get_key(cfg.data_root)  # no env, nothing cached, not interactive
    assert e.value.code == "locked" and e.value.status == 423
    key = crypt.get_key(cfg.data_root, passphrase=PASS, remember=True)
    assert crypt.get_key(cfg.data_root) == key and list(kr.store)  # now served from the keychain
    crypt.forget_key(cfg.data_root)
    assert kr.store == {}
    monkeypatch.setenv(crypt.ENV_PASSPHRASE, PASS)
    assert crypt.get_key(cfg.data_root) == key
    monkeypatch.setenv(crypt.ENV_PASSPHRASE, "nope")
    with pytest.raises(AppError):
        crypt.get_key(cfg.data_root)
    monkeypatch.delenv(crypt.ENV_PASSPHRASE)
    monkeypatch.setattr(crypt.getpass, "getpass", lambda prompt="": PASS)
    assert crypt.get_key(cfg.data_root, interactive=True) == key


# ── encrypt / use / decrypt ───────────────────────────────────
def test_encrypt_in_place_keeps_everything_working_and_hides_content(tmp_path, kr):
    cfg = Config.load(tmp_path / "data", workers=1, heartbeat_s=0.2, stale_after_s=0.5)
    ids = populated(cfg)
    assert SECRET_WORD.encode() in raw_db(cfg) and raw_db(cfg)[:15] == b"SQLite format 3"  # plaintext before

    with pytest.raises(AppError) as weak:
        crypt.encrypt_in_place(cfg.data_root, "short")
    assert weak.value.code == "weak_passphrase"
    res = crypt.encrypt_in_place(cfg.data_root, PASS)
    assert res["encrypted"] and res["tables"]["documents"] == 2 and res["tables"]["chunks"] >= 2 and res["tables"]["chunk_vec"] >= 2
    assert crypt.is_encrypted(cfg.data_root) and os.path.exists(res["plaintext_copy"])
    raw = raw_db(cfg)
    assert raw[:15] != b"SQLite format 3" and SECRET_WORD.encode() not in raw and b"INV-20491" not in raw and b"documents" not in raw
    with pytest.raises(AppError) as locked:
        cmod.build_container(cfg, start_workers=False, auto_llm=False)  # no key anywhere
    assert locked.value.code == "locked"
    with pytest.raises(Exception):  # wrong key never opens it
        cmod.build_container(cfg, start_workers=False, auto_llm=False, key=bytes(32))

    key = crypt.get_key(cfg.data_root, passphrase=PASS)
    tc = app(cfg, key=key, emb=ConceptEmbedding())
    assert tc.get("/api/v1/health").json()["capabilities"]["encrypted"] is True and tc.get("/api/v1/health").json()["db"]["ok"]
    assert {d["title"] for d in tc.get("/api/v1/documents").json()["items"]} == {"inv", "note"}
    assert titles(search(tc, SECRET_WORD, mode="keyword")) == ["note"]  # FTS rebuilt from the copied chunks
    assert titles(search(tc, "automobile", mode="meaning"))[0] == "note"  # vectors survived the copy
    assert search(tc, "Acme Traders")["results"][0]["field_hits"][0]["key"] == "customer"  # field_fts too
    assert tc.get(f"/api/v1/documents/{ids[0]}").json()["tags"] == ["finance"] and tc.get(f"/api/v1/documents/{ids[0]}/file").status_code == 200
    new = upload(tc, "later.txt", b"added after encryption works fine")["document_id"]  # write path, queue, workers, triggers
    run_workers(tc)
    settle(tc, new)
    drain(tc)
    assert titles(search(tc, "added after encryption", mode="keyword")) == ["later"]
    assert tc.post("/api/v1/backup", json={}).json()["size"] > 0  # online backup of an encrypted DB
    tc.container.stop()
    tc.__exit__(None, None, None)
    assert SECRET_WORD.encode() not in raw_db(cfg)  # still opaque after use

    with pytest.raises(AppError) as again:
        crypt.encrypt_in_place(cfg.data_root, PASS)
    assert again.value.code == "already_encrypted"
    out = crypt.decrypt_in_place(cfg.data_root, PASS, keep_encrypted=False)
    assert out["encrypted"] is False and not crypt.is_encrypted(cfg.data_root)
    assert raw_db(cfg)[:15] == b"SQLite format 3"
    tc2 = app(cfg)
    assert {d["title"] for d in tc2.get("/api/v1/documents").json()["items"]} == {"inv", "note", "later"}
    assert titles(search(tc2, SECRET_WORD, mode="keyword")) == ["note"]
    tc2.container.stop()
    tc2.__exit__(None, None, None)
    with pytest.raises(AppError):
        crypt.decrypt_in_place(cfg.data_root, PASS)  # not encrypted any more


def test_decrypt_requires_the_right_passphrase(tmp_path):
    cfg = Config.load(tmp_path / "data", workers=1)
    populated(cfg)
    crypt.encrypt_in_place(cfg.data_root, PASS, keep_plain=False)
    with pytest.raises(AppError) as e:
        crypt.decrypt_in_place(cfg.data_root, "not the passphrase")
    assert e.value.code == "bad_passphrase" and crypt.is_encrypted(cfg.data_root)


# ── backup / restore of an encrypted library ──────────────────
def test_backup_and_restore_of_an_encrypted_library(tmp_path):
    cfg = Config.load(tmp_path / "data", workers=1, heartbeat_s=0.2, stale_after_s=0.5)
    populated(cfg)
    crypt.encrypt_in_place(cfg.data_root, PASS, keep_plain=False)
    key = crypt.get_key(cfg.data_root, passphrase=PASS)
    tc = app(cfg, key=key)
    name = tc.post("/api/v1/backup", json={"passphrase": "backup-pass-1"}).json()["name"]
    tc.container.stop()
    tc.__exit__(None, None, None)
    import zipfile
    # the (decrypted) backup zip carries the salt and a database that is NOT plaintext
    bk.decrypt_file(cfg.data_root / "backups" / name, tmp_path / "b.zip", "backup-pass-1")
    with zipfile.ZipFile(tmp_path / "b.zip") as z:
        assert "db/crypt.json" in z.namelist() and z.read("db/studio.sqlite")[:15] != b"SQLite format 3"

    st = bk.stage_restore(cfg.data_root, cfg.data_root / "backups" / name, "backup-pass-1")
    assert st["encrypted_database"] and st["db_verified"] is False  # cannot be opened without the DB passphrase
    with pytest.raises(AppError) as bad:
        bk.stage_restore(cfg.data_root, cfg.data_root / "backups" / name, "backup-pass-1", db_passphrase="wrong")
    assert bad.value.code == "bad_backup" and not (cfg.data_root / bk.PENDING).exists()
    good = bk.stage_restore(cfg.data_root, cfg.data_root / "backups" / name, "backup-pass-1", db_passphrase=PASS)
    assert good["db_verified"] is True
    assert bk.apply_pending_restore(cfg.data_root) is True
    assert crypt.is_encrypted(cfg.data_root)
    tc2 = app(cfg, key=crypt.get_key(cfg.data_root, passphrase=PASS))
    assert {d["title"] for d in tc2.get("/api/v1/documents").json()["items"]} == {"inv", "note"}
    tc2.container.stop()
    tc2.__exit__(None, None, None)


# ── CLI ───────────────────────────────────────────────────────
def test_cli_encrypt_unlock_forget_decrypt(tmp_path, kr, monkeypatch, capsys):
    cfg = Config.load(tmp_path / "data", workers=1)
    populated(cfg)
    d = str(cfg.data_root)
    monkeypatch.setenv("ADSTUDIO_NEW_PASSPHRASE", PASS)
    assert cli(["--data-dir", d, "encrypt", "--remember"]) == 0
    out = capsys.readouterr().out
    assert "PLAINTEXT copy" in out and "files/ are not encrypted" in out and crypt.is_encrypted(cfg.data_root) and kr.store
    assert cli(["--data-dir", d, "forget-key"]) == 0 and kr.store == {}
    monkeypatch.setattr(crypt.getpass, "getpass", lambda prompt="": "wrong")
    assert cli(["--data-dir", d, "unlock"]) == 1
    assert "Wrong passphrase" in capsys.readouterr().err
    monkeypatch.setattr(crypt.getpass, "getpass", lambda prompt="": PASS)
    assert cli(["--data-dir", d, "unlock", "--remember"]) == 0 and kr.store
    import getpass as gp
    monkeypatch.setattr(gp, "getpass", lambda prompt="": PASS)
    assert cli(["--data-dir", d, "decrypt", "--delete-encrypted"]) == 0 and not crypt.is_encrypted(cfg.data_root) and kr.store == {}

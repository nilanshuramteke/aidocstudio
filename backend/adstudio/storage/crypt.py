"""Optional at-rest encryption of the SQLite database (SQLCipher, AES-256).

The key is derived from a passphrase with Argon2id; only salt + KDF parameters + a verifier (HMAC of a constant) are stored in
<data>/db/crypt.json, never the key. The derived key can be cached in the OS keychain ("remember on this device") so normal
launches do not prompt. Originals under files/ are NOT encrypted by this layer (see ADR 001 #29).
"""
from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import os
import secrets
import shutil
from datetime import datetime
from pathlib import Path

from ..core.errors import AppError
from .db import Database, load_sqlite_vec, vec_available
from .errors import DatabaseError

CRYPT_FILE = "crypt.json"
ENV_PASSPHRASE = "ADSTUDIO_PASSPHRASE"
KEYRING_SERVICE = "adstudio"
KDF = dict(time_cost=3, memory_cost=65536, parallelism=4)  # Argon2id: ~0.2 s on a laptop, once per launch
MIN_PASSPHRASE = 8
_SKIP_PREFIXES = ("sqlite_",)


def available() -> bool:
    try:
        import argon2  # noqa: F401
        import sqlcipher3  # noqa: F401
        return True
    except ImportError:
        return False


def crypt_file(data_root: Path) -> Path:
    return Path(data_root) / "db" / CRYPT_FILE


def is_encrypted(data_root: Path) -> bool:
    f = crypt_file(data_root)
    if not f.exists():
        return False
    try:
        return bool(json.loads(f.read_text(encoding="utf-8")).get("enabled"))
    except (OSError, ValueError):
        return False


def _params(data_root: Path) -> dict:
    return json.loads(crypt_file(data_root).read_text(encoding="utf-8"))


def derive_key(passphrase: str, salt: bytes, **kdf) -> bytes:
    from argon2.low_level import Type, hash_secret_raw
    p = {**KDF, **kdf}
    return hash_secret_raw(passphrase.encode(), salt, time_cost=p["time_cost"], memory_cost=p["memory_cost"],
                           parallelism=p["parallelism"], hash_len=32, type=Type.ID)


def _verifier(key: bytes) -> str:
    return hmac.new(key, b"adstudio-key-check", hashlib.sha256).hexdigest()


# ── key sources ───────────────────────────────────────────────
def _keyring_user(data_root: Path) -> str:
    return str(Path(data_root).resolve())


def cached_key(data_root: Path) -> bytes | None:
    try:
        import keyring
        v = keyring.get_password(KEYRING_SERVICE, _keyring_user(data_root))
        return bytes.fromhex(v) if v else None
    except Exception:  # noqa: BLE001 - no usable keychain
        return None


def remember_key(data_root: Path, key: bytes) -> bool:
    try:
        import keyring
        keyring.set_password(KEYRING_SERVICE, _keyring_user(data_root), key.hex())
        return True
    except Exception:  # noqa: BLE001
        return False


def forget_key(data_root: Path) -> None:
    try:
        import keyring
        keyring.delete_password(KEYRING_SERVICE, _keyring_user(data_root))
    except Exception:  # noqa: BLE001
        pass


def key_from_passphrase(data_root: Path, passphrase: str) -> bytes:
    meta = _params(data_root)
    key = derive_key(passphrase, bytes.fromhex(meta["salt"]), **meta.get("kdf_params", {}))
    if not hmac.compare_digest(_verifier(key), meta["verifier"]):
        raise AppError("Wrong passphrase", code="bad_passphrase", status=401)
    return key


def get_key(data_root: Path, *, passphrase: str | None = None, interactive: bool = False, remember: bool = False) -> bytes:
    """Resolve the database key: explicit passphrase, ADSTUDIO_PASSPHRASE, OS keychain, then (if interactive) a prompt."""
    meta = _params(data_root)
    candidates = []
    if passphrase:
        candidates.append(("passphrase", passphrase))
    elif os.environ.get(ENV_PASSPHRASE):
        candidates.append(("env", os.environ[ENV_PASSPHRASE]))
    for _, p in candidates:
        key = key_from_passphrase(data_root, p)
        if remember:
            remember_key(data_root, key)
        return key
    key = cached_key(data_root)
    if key and hmac.compare_digest(_verifier(key), meta["verifier"]):
        return key
    if interactive:
        key = key_from_passphrase(data_root, getpass.getpass("Library passphrase: "))
        if remember:
            remember_key(data_root, key)
        return key
    raise AppError(f"The library is encrypted. Run `adstudio unlock --remember`, or set {ENV_PASSPHRASE}.", code="locked", status=423)


# ── in-place conversion ───────────────────────────────────────
def _open_for_copy(path: Path, key: bytes | None):
    import sqlcipher3.dbapi2 as drv
    conn = drv.connect(str(path), isolation_level=None)
    if key is not None:
        conn.execute(f"PRAGMA key = \"x'{key.hex()}'\"")
    conn.row_factory = drv.Row
    conn.execute("PRAGMA foreign_keys=OFF")  # copy in any order; checked afterwards
    if vec_available():
        load_sqlite_vec(conn)
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    return conn


def _data_tables(conn) -> list[str]:
    """Ordinary tables to copy. Virtual-table shadow tables are rebuilt by their owners, not copied."""
    virtual = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE sql LIKE 'CREATE VIRTUAL TABLE%'")}
    out = []
    for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        n = r["name"]
        if n.startswith(_SKIP_PREFIXES) or n in virtual or any(n.startswith(v + "_") for v in virtual):
            continue
        out.append(n)
    return out


def _convert(src_path: Path, src_key: bytes | None, dst_path: Path, dst_key: bytes | None) -> dict[str, int]:
    """Copy a database into a fresh one with a different key (None = plaintext). Schema comes from our own migrations,
    so FTS5 / triggers / sqlite-vec tables are created natively and repopulated rather than byte-copied."""
    version_db = Database(dst_path, key=dst_key, on_connect=[load_sqlite_vec] if vec_available() else [])
    try:
        version_db.migrate()
    finally:
        version_db.close()
    src, dst = _open_for_copy(src_path, src_key), _open_for_copy(dst_path, dst_key)
    counts: dict[str, int] = {}
    try:
        dst.execute("BEGIN")
        for t in _data_tables(src):
            cols = [r["name"] for r in src.execute(f'PRAGMA table_info("{t}")')]
            if not cols or not dst.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone():
                continue
            marks, names = ",".join("?" * len(cols)), ",".join(f'"{c}"' for c in cols)
            rows = src.execute(f'SELECT {names} FROM "{t}"').fetchall()
            dst.executemany(f'INSERT OR REPLACE INTO "{t}"({names}) VALUES({marks})', [tuple(r) for r in rows])
            counts[t] = len(rows)
        # standalone FTS table + the vector table are not covered by triggers / the loop above
        if src.execute("SELECT 1 FROM sqlite_master WHERE name='field_fts'").fetchone():
            rows = src.execute("SELECT document_id, key, value FROM field_fts").fetchall()
            dst.executemany("INSERT INTO field_fts(document_id,key,value) VALUES(?,?,?)", [tuple(r) for r in rows])
        if src.execute("SELECT 1 FROM sqlite_master WHERE name='chunk_vec'").fetchone():
            dim = src.execute("SELECT dim FROM embedding_models LIMIT 1").fetchone()["dim"]
            dst.execute("DROP TABLE IF EXISTS chunk_vec")
            dst.execute(f"CREATE VIRTUAL TABLE chunk_vec USING vec0(embedding float[{int(dim)}])")
            rows = src.execute("SELECT rowid, embedding FROM chunk_vec").fetchall()
            dst.executemany("INSERT INTO chunk_vec(rowid, embedding) VALUES(?,?)", [(r[0], r[1]) for r in rows])
            counts["chunk_vec"] = len(rows)
        dst.execute("COMMIT")
        # chunks_fts is external-content and filled by triggers during the copy; verify rather than assume
        if "chunks" in counts:
            dst.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        dst.execute(f"PRAGMA user_version = {int(src.execute('PRAGMA user_version').fetchone()[0])}")
        bad = dst.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise AppError(f"Foreign key check failed after conversion ({len(bad)} rows)", code="convert_failed")
        if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise AppError("Integrity check failed after conversion", code="convert_failed")
    finally:
        src.close()
        dst.close()
    return counts


def _db_path(data_root: Path) -> Path:
    return Path(data_root) / "db" / "studio.sqlite"


def _assert_idle(path: Path) -> None:
    import sqlite3
    try:
        c = sqlite3.connect(str(path), timeout=1)
        c.execute("PRAGMA locking_mode=EXCLUSIVE")
        c.execute("BEGIN EXCLUSIVE")
        c.execute("ROLLBACK")
        c.close()
    except sqlite3.OperationalError as e:
        raise AppError("Close AdStudio before changing encryption (the database is in use)", code="in_use", status=409) from e


def _swap_in(data_root: Path, new_db: Path, *, keep_old: bool, label: str) -> Path | None:
    db = _db_path(data_root)
    old = None
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    if keep_old:
        old = db.with_name(f"studio.sqlite.{label}-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
        shutil.move(str(db), old)
    else:
        db.unlink()
    shutil.move(str(new_db), db)
    return old


def encrypt_in_place(data_root: Path, passphrase: str, *, keep_plain: bool = True) -> dict:
    data_root = Path(data_root)
    if not available():
        raise AppError("Encryption support is not installed (pip install sqlcipher3-wheels argon2-cffi)", code="unavailable", status=501)
    if is_encrypted(data_root):
        raise AppError("The library is already encrypted", code="already_encrypted", status=409)
    if len(passphrase) < MIN_PASSPHRASE:
        raise AppError(f"Use a passphrase of at least {MIN_PASSPHRASE} characters", code="weak_passphrase", status=422)
    db = _db_path(data_root)
    if not db.exists():
        raise AppError("No library database found", code="not_found", status=404)
    _assert_idle(db)
    salt = secrets.token_bytes(16)
    key = derive_key(passphrase, salt)
    tmp = db.with_name("studio.sqlite.encrypting")
    tmp.unlink(missing_ok=True)
    plain = Database(db)  # bring the plaintext schema fully up to date first, then checkpoint the WAL into the main file
    try:
        plain.migrate()
        with plain._wlock:
            plain._writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        plain.close()
    try:
        counts = _convert(db, None, tmp, key)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    old = _swap_in(data_root, tmp, keep_old=keep_plain, label="plain")
    crypt_file(data_root).write_text(json.dumps({"enabled": True, "kdf": "argon2id", "salt": salt.hex(), "verifier": _verifier(key),
                                                 "kdf_params": KDF, "created_at": datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")
    return {"encrypted": True, "tables": counts, "plaintext_copy": str(old) if old else None}


def decrypt_in_place(data_root: Path, passphrase: str, *, keep_encrypted: bool = True) -> dict:
    data_root = Path(data_root)
    if not is_encrypted(data_root):
        raise AppError("The library is not encrypted", code="not_encrypted", status=409)
    key = key_from_passphrase(data_root, passphrase)
    db = _db_path(data_root)
    _assert_idle_encrypted(db, key)
    tmp = db.with_name("studio.sqlite.decrypting")
    tmp.unlink(missing_ok=True)
    try:
        counts = _convert(db, key, tmp, None)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    old = _swap_in(data_root, tmp, keep_old=keep_encrypted, label="encrypted")
    crypt_file(data_root).unlink(missing_ok=True)
    forget_key(data_root)
    return {"encrypted": False, "tables": counts, "encrypted_copy": str(old) if old else None}


def _assert_idle_encrypted(path: Path, key: bytes) -> None:
    import sqlcipher3.dbapi2 as drv
    try:
        c = drv.connect(str(path), timeout=1)
        c.execute(f"PRAGMA key = \"x'{key.hex()}'\"")
        c.execute("BEGIN EXCLUSIVE")
        c.execute("ROLLBACK")
        c.close()
    except drv.OperationalError as e:
        raise AppError("Close AdStudio before changing encryption (the database is in use)", code="in_use", status=409) from e
    except DatabaseError as e:
        raise AppError("Could not open the encrypted database with that key", code="bad_passphrase", status=401) from e

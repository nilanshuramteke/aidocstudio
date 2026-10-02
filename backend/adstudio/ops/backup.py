"""Backup / restore. A backup is a zip of {SQLite online backup, original files, config}; derived/ is regenerable and
excluded. Optional passphrase encryption (scrypt + chunked AES-256-GCM). Restore is staged and applied at next start,
because the live database cannot be swapped under open connections."""
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from .. import __version__
from ..core.errors import AppError
from ..core.timeutil import now_iso
from ..storage.db import Database

MAGIC = b"ADSBK1"
CHUNK = 1024 * 1024
SCRYPT = dict(n=2 ** 15, r=8, p=1, dklen=32, maxmem=128 * 1024 * 1024)
PENDING = "restore_pending"
EXCLUDE_TOP = {"derived", "tmp", "logs", "models", "backups", "exports", PENDING}


# ── encryption ────────────────────────────────────────────────
def _key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(passphrase.encode(), salt=salt, **SCRYPT)


def encrypt_file(src: Path, dst: Path, passphrase: str) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, prefix = os.urandom(16), os.urandom(8)
    aes = AESGCM(_key(passphrase, salt))
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        fo.write(MAGIC + salt + prefix)
        counter, chunk = 0, fi.read(CHUNK)
        while True:
            nxt = fi.read(CHUNK)
            last = not nxt
            nonce = prefix + counter.to_bytes(4, "big")
            ct = aes.encrypt(nonce, chunk, b"\x01" if last else b"\x00")  # AAD marks the final chunk: truncation is detected
            fo.write(len(ct).to_bytes(4, "big") + ct)
            if last:
                break
            counter, chunk = counter + 1, nxt


def decrypt_file(src: Path, dst: Path, passphrase: str) -> None:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    with open(src, "rb") as fi:
        head = fi.read(len(MAGIC) + 24)
        if not head.startswith(MAGIC) or len(head) < len(MAGIC) + 24:
            raise AppError("Not an encrypted backup", code="bad_backup")
        salt, prefix = head[len(MAGIC):len(MAGIC) + 16], head[len(MAGIC) + 16:]
        aes = AESGCM(_key(passphrase, salt))
        try:
            with open(dst, "wb") as fo:
                counter, saw_last = 0, False
                while True:
                    ln = fi.read(4)
                    if not ln:
                        break
                    ct = fi.read(int.from_bytes(ln, "big"))
                    peek = fi.read(1)
                    last = not peek
                    if peek:
                        fi.seek(-1, 1)
                    fo.write(aes.decrypt(prefix + counter.to_bytes(4, "big"), ct, b"\x01" if last else b"\x00"))
                    counter += 1
                    saw_last = last
                if not saw_last:
                    raise InvalidTag()
        except InvalidTag as e:
            dst.unlink(missing_ok=True)
            raise AppError("Wrong passphrase, or the backup is damaged or incomplete", code="bad_passphrase", status=422) from e


# ── create ────────────────────────────────────────────────────
def create_backup(data_root: Path, db: Database, *, passphrase: str | None = None, auto: bool = False) -> Path:
    data_root = Path(data_root)
    out_dir = data_root / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = out_dir / f"backup-{'auto-' if auto else ''}{stamp}"
    n = 1
    while base.with_suffix(".zip").exists() or base.with_suffix(".adsbk").exists():
        base = out_dir / f"backup-{'auto-' if auto else ''}{stamp}-{n}"
        n += 1
    tmp = Path(tempfile.mkdtemp(dir=data_root / "tmp" if (data_root / "tmp").exists() else None))
    try:
        snap = tmp / "studio.sqlite"
        db._backup_to(snap)  # SQLite online backup API: consistent while the app runs
        files = [p for p in (data_root / "files").rglob("*") if p.is_file()] if (data_root / "files").exists() else []
        zpath = tmp / "backup.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps({
                "format": 1, "app_version": __version__, "created_at": now_iso(), "schema_version": db.user_version(),
                "files": len(files)}))
            z.write(snap, "db/studio.sqlite")
            if (data_root / "db" / "crypt.json").exists():  # the salt/KDF params are needed to open an encrypted DB
                z.write(data_root / "db" / "crypt.json", "db/crypt.json")
            for p in files:
                z.write(p, "files/" + p.relative_to(data_root / "files").as_posix())
            if (data_root / "config.toml").exists():
                z.write(data_root / "config.toml", "config.toml")
        if passphrase:
            final = base.with_suffix(".adsbk")
            encrypt_file(zpath, final, passphrase)
        else:
            final = base.with_suffix(".zip")
            shutil.move(zpath, final)
        return final
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def list_backups(data_root: Path) -> list[dict]:
    d = Path(data_root) / "backups"
    out = []
    for p in sorted(d.glob("backup-*"), reverse=True) if d.exists() else []:
        if p.suffix in (".zip", ".adsbk"):
            out.append({"name": p.name, "size": p.stat().st_size, "encrypted": p.suffix == ".adsbk", "auto": "-auto-" in p.name,
                        "modified": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")})
    return out


def prune_auto_backups(data_root: Path, keep: int) -> int:
    autos = [b for b in list_backups(data_root) if b["auto"]]
    for b in autos[keep:]:
        (Path(data_root) / "backups" / b["name"]).unlink(missing_ok=True)
    return max(0, len(autos) - keep)


# ── restore ───────────────────────────────────────────────────
def stage_restore(data_root: Path, backup: Path, passphrase: str | None = None, db_passphrase: str | None = None) -> dict:
    """Validate a backup and stage it for the next start. Nothing live is touched."""
    data_root, backup = Path(data_root), Path(backup)
    work = Path(tempfile.mkdtemp(dir=data_root))
    try:
        zsrc = backup
        if backup.suffix == ".adsbk" or backup.read_bytes()[:len(MAGIC)] == MAGIC:
            if not passphrase:
                raise AppError("This backup is encrypted; a passphrase is required", code="passphrase_required", status=422)
            zsrc = work / "decrypted.zip"
            decrypt_file(backup, zsrc, passphrase)
        try:
            z = zipfile.ZipFile(zsrc)
        except zipfile.BadZipFile as e:
            raise AppError("Not a valid backup file", code="bad_backup") from e
        with z:
            names = z.namelist()
            for n in names:  # no absolute paths or traversal inside the archive
                if n.startswith(("/", "\\")) or ".." in Path(n).parts or ":" in n:
                    raise AppError("Backup contains unsafe paths", code="bad_backup")
            if "manifest.json" not in names or "db/studio.sqlite" not in names:
                raise AppError("Backup is missing its manifest or database", code="bad_backup")
            manifest = json.loads(z.read("manifest.json"))
            stage = data_root / PENDING
            shutil.rmtree(stage, ignore_errors=True)
            stage.mkdir(parents=True)
            try:
                z.extractall(stage)
            except Exception as e:  # noqa: BLE001 - truncated/corrupt members
                shutil.rmtree(stage, ignore_errors=True)
                raise AppError(f"Backup could not be extracted: {e}", code="bad_backup") from e
        encrypted_db = (stage / "db" / "crypt.json").exists()
        try:
            if encrypted_db:  # cannot be read without the DB passphrase: check it really is not a plaintext SQLite file
                head = (stage / "db" / "studio.sqlite").read_bytes()[:16]
                ok, tables = head != b"SQLite format 3\x00" and len(head) == 16, {"documents", "settings"}
                if db_passphrase:
                    from ..storage import crypt
                    import json as _json
                    import shutil as _sh
                    (stage / "db" / "check").mkdir(exist_ok=True)
                    _sh.copy(stage / "db" / "crypt.json", stage / "db" / "check" / "crypt.json")
                    meta = _json.loads((stage / "db" / "crypt.json").read_text())
                    key = crypt.derive_key(db_passphrase, bytes.fromhex(meta["salt"]), **meta.get("kdf_params", {}))
                    import sqlcipher3.dbapi2 as drv
                    conn = drv.connect(str(stage / "db" / "studio.sqlite"))
                    try:
                        conn.execute(f"PRAGMA key = \"x'{key.hex()}'\"")
                        ok = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                    finally:
                        conn.close()
                    _sh.rmtree(stage / "db" / "check", ignore_errors=True)
            else:
                conn = sqlite3.connect(stage / "db" / "studio.sqlite")
                try:
                    ok = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                finally:
                    conn.close()
        except (sqlite3.DatabaseError, Exception) as e:  # noqa: BLE001 - includes sqlcipher's DatabaseError (wrong DB passphrase)
            ok, tables = False, set()
        if not ok or not {"documents", "settings"} <= tables:
            shutil.rmtree(stage, ignore_errors=True)  # never leave a bad backup staged: it would be applied at next start
            raise AppError("The database inside the backup failed its integrity check", code="bad_backup", status=422)
        return {"staged": True, "restart_required": True, "encrypted_database": encrypted_db,
                "db_verified": (not encrypted_db) or bool(db_passphrase),
                "created_at": manifest.get("created_at"),
                "files": manifest.get("files"), "schema_version": manifest.get("schema_version")}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def apply_pending_restore(data_root: Path) -> bool:
    """Call at startup BEFORE the database is opened. Current data is kept under backups/pre-restore-*."""
    data_root = Path(data_root)
    stage = data_root / PENDING
    if not (stage / "db" / "studio.sqlite").exists():
        return False
    keep = data_root / "backups" / f"pre-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    keep.mkdir(parents=True, exist_ok=True)
    for name in ("studio.sqlite", "studio.sqlite-wal", "studio.sqlite-shm", "crypt.json"):
        src = data_root / "db" / name
        if src.exists():
            shutil.move(str(src), keep / name)
    if (data_root / "files").exists():
        shutil.move(str(data_root / "files"), keep / "files")
    (data_root / "db").mkdir(exist_ok=True)
    shutil.move(str(stage / "db" / "studio.sqlite"), data_root / "db" / "studio.sqlite")
    if (stage / "db" / "crypt.json").exists():  # restored library is encrypted with ITS passphrase
        shutil.move(str(stage / "db" / "crypt.json"), data_root / "db" / "crypt.json")
    if (stage / "files").exists():
        shutil.move(str(stage / "files"), data_root / "files")
    else:
        (data_root / "files").mkdir(exist_ok=True)
    shutil.rmtree(data_root / "derived", ignore_errors=True)  # regenerable; stale pages would not match restored docs
    shutil.rmtree(stage, ignore_errors=True)
    return True

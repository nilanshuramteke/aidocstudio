"""SQLite access: one serialized writer connection, thread-local readers, ordered migrations."""
import re
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
MIN_SQLITE = (3, 37, 0)  # STRICT tables; RETURNING needs 3.35


class CapabilityError(RuntimeError):
    pass


def probe_capabilities() -> dict:
    """Fail fast on unsupported SQLite; report optional features."""
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise CapabilityError(
            f"SQLite {sqlite3.sqlite_version} is too old; need >= {'.'.join(map(str, MIN_SQLITE))}"
        )
    conn = sqlite3.connect(":memory:")
    try:
        fts5 = True
        try:
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        except sqlite3.OperationalError:
            fts5 = False
        return {
            "sqlite": sqlite3.sqlite_version,
            "fts5": fts5,
            "load_extension": hasattr(conn, "enable_load_extension"),
        }
    finally:
        conn.close()


def load_sqlite_vec(conn: sqlite3.Connection) -> None:
    """on_connect hook: load the sqlite-vec extension (raises if unavailable)."""
    import sqlite_vec
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)


def vec_available() -> bool:
    try:
        c = sqlite3.connect(":memory:")
        try:
            load_sqlite_vec(c)
            return True
        finally:
            c.close()
    except Exception:  # noqa: BLE001
        return False


def _driver(key: bytes | None):
    """stdlib sqlite3 normally; sqlcipher3 (same API, AES-256 pages) when a key is supplied."""
    if key is None:
        return sqlite3
    import sqlcipher3.dbapi2 as sqlcipher
    return sqlcipher


def apply_key(conn, key: bytes) -> None:
    """Raw 256-bit key (derived with Argon2id upstream): SQLCipher skips its slow PBKDF2 for `x'hex'` keys."""
    conn.execute(f"PRAGMA key = \"x'{key.hex()}'\"")
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()  # raises DatabaseError on a wrong key


def _connect(path: Path, on_connect=(), key: bytes | None = None):
    drv = _driver(key)
    conn = drv.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
    if key is not None:
        apply_key(conn, key)
    conn.row_factory = drv.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    for hook in on_connect:
        hook(conn)
    return conn


class Database:
    def __init__(self, path: Path, on_connect=(), key: bytes | None = None):
        self.path = Path(path)
        self.on_connect = tuple(on_connect)
        self.key = key
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._writer = _connect(self.path, self.on_connect, self.key)
        self._wlock = threading.RLock()
        self._local = threading.local()
        self._readers: list[sqlite3.Connection] = []  # every thread's reader, so close() really releases the file
        self._readers_lock = threading.Lock()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Serialized write transaction (BEGIN IMMEDIATE ... COMMIT/ROLLBACK)."""
        with self._wlock:
            self._writer.execute("BEGIN IMMEDIATE")
            try:
                yield self._writer
            except BaseException:
                self._writer.execute("ROLLBACK")
                raise
            else:
                self._writer.execute("COMMIT")

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = _connect(self.path, self.on_connect, self.key)
            with self._readers_lock:
                self._readers.append(conn)
        yield conn

    def integrity_ok(self) -> bool:
        with self.read() as c:
            return c.execute("PRAGMA quick_check").fetchone()[0] == "ok"

    def close(self) -> None:
        with self._wlock:
            self._writer.close()
        with self._readers_lock:
            readers, self._readers = self._readers, []
        for conn in readers:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._local.conn = None

    # ── migrations ────────────────────────────────────────────
    def user_version(self) -> int:
        with self.read() as c:
            return c.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self, backup_dir: Path | None = None) -> int:
        """Apply pending NNN_name.sql files in order; auto-backup first if data exists."""
        files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
        current = self.user_version()
        pending = [f for f in files if int(re.match(r"(\d+)_", f.name).group(1)) > current]
        if not pending:
            return current
        if current > 0 and backup_dir is not None:
            backup_dir.mkdir(parents=True, exist_ok=True)
            self._backup_to(backup_dir / f"pre-migration-v{current}.sqlite")
        for f in pending:
            version = int(re.match(r"(\d+)_", f.name).group(1))
            with self._wlock:
                self._writer.executescript(f"BEGIN;\n{f.read_text(encoding='utf-8')}\nPRAGMA user_version={version};\nCOMMIT;")
            current = version
        return current

    def _backup_to(self, dest: Path) -> None:
        with self._wlock:
            drv = _driver(self.key)
            target = drv.connect(dest)
            if self.key is not None:  # an encrypted database produces an encrypted backup
                apply_key(target, self.key)
            try:
                self._writer.backup(target)
            finally:
                target.close()

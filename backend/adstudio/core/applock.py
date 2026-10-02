"""Optional app lock: an Argon2id-hashed passphrase gates the whole API; an idle timeout re-locks it.

Server-side state only (the browser cannot unlock itself by editing storage). The lock is separate from database encryption:
it protects a running app from someone else at the keyboard; encryption protects the files when the app is not running.
"""
import json
import threading
import time
from pathlib import Path
from typing import Callable

from .errors import AppError

MIN_PASSPHRASE = 6
MAX_BACKOFF_S = 300


class AppLock:
    def __init__(self, data_root: Path, clock: Callable[[], float] = time.monotonic):
        self.file = Path(data_root) / "lock.json"
        self.clock = clock
        self._mutex = threading.Lock()
        self._hash: str | None = None
        self.locked = False
        self.last_activity = clock()
        self.failures = 0
        self.blocked_until = 0.0
        if self.file.exists():
            try:
                self._hash = json.loads(self.file.read_text(encoding="utf-8")).get("hash")
            except (OSError, ValueError):
                self._hash = None
        self.locked = self.enabled  # an enabled lock starts locked

    @property
    def enabled(self) -> bool:
        return self._hash is not None

    @staticmethod
    def _hasher():
        from argon2 import PasswordHasher
        return PasswordHasher()

    def _verify(self, passphrase: str) -> bool:
        from argon2.exceptions import InvalidHashError, VerificationError
        try:
            return self._hasher().verify(self._hash, passphrase)
        except (VerificationError, InvalidHashError):
            return False

    def status(self, idle_minutes: float = 0) -> dict:
        return {"enabled": self.enabled, "locked": self.locked, "idle_lock_minutes": idle_minutes,
                "retry_after": max(0, round(self.blocked_until - self.clock()))}

    # activity / idle ------------------------------------------
    def touch(self) -> None:
        self.last_activity = self.clock()

    def check_idle(self, minutes: float) -> bool:
        """Lock if idle for longer than `minutes` (0 = never). Returns the resulting locked state."""
        if self.enabled and not self.locked and minutes > 0 and self.clock() - self.last_activity > minutes * 60:
            self.locked = True
        return self.locked

    # transitions ----------------------------------------------
    def setup(self, passphrase: str) -> None:
        if self.enabled:
            raise AppError("An app lock is already set; disable it first", code="already_set", status=409)
        if len(passphrase) < MIN_PASSPHRASE:
            raise AppError(f"Use at least {MIN_PASSPHRASE} characters", code="weak_passphrase", status=422)
        h = self._hasher().hash(passphrase)
        self.file.write_text(json.dumps({"hash": h}), encoding="utf-8")
        self._hash, self.locked = h, False
        self.touch()

    def unlock(self, passphrase: str) -> None:
        with self._mutex:
            now = self.clock()
            if now < self.blocked_until:
                raise AppError(f"Too many attempts. Try again in {round(self.blocked_until - now)} s", code="rate_limited", status=429)
            if not self.enabled or self._verify(passphrase):
                self.failures, self.locked = 0, False
                self.touch()
                return
            self.failures += 1
            if self.failures >= 3:  # 3 free tries, then exponential back-off
                self.blocked_until = now + min(MAX_BACKOFF_S, 2 ** (self.failures - 2))
            raise AppError("Wrong passphrase", code="bad_passphrase", status=401)

    def lock(self) -> None:
        if self.enabled:
            self.locked = True

    def disable(self, passphrase: str) -> None:
        if not self.enabled:
            return
        self.unlock(passphrase)  # same rate limiting and verification
        self.file.unlink(missing_ok=True)
        self._hash, self.locked = None, False

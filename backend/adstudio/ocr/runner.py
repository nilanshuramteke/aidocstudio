"""Runs OCR in long-lived worker processes (spawn-safe on Windows) with a per-page timeout.

Models load once per worker (cached in a module global), not per page. A timed-out or crashed worker
is killed and the pool recreated, so a poison page never wedges the app.
"""
import threading
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FutureTimeout
from concurrent.futures.process import BrokenProcessPool
import multiprocessing

from ..core.interfaces import OCRPage, OCRProvider

_worker_providers: dict[str, OCRProvider] = {}


def _ocr_task(engine: str, image: bytes, langs: list[str]) -> OCRPage:
    p = _worker_providers.get(engine)
    if p is None:
        from .registry import make_provider
        p = _worker_providers[engine] = make_provider(engine)
    return p.recognize(image, langs)


class OCRTimeout(RuntimeError):
    pass


class OCRRunner:
    def __init__(self, engine: str, *, workers: int = 2, timeout_s: float = 120.0, inline: OCRProvider | None = None):
        """`inline` runs a provider in-process (tests, fakes); otherwise a spawn process pool is used."""
        self.engine, self.workers, self.timeout_s, self.inline = engine, workers, timeout_s, inline
        self._pool: ProcessPoolExecutor | None = None
        self._lock = threading.Lock()

    def _get_pool(self) -> ProcessPoolExecutor:
        with self._lock:
            if self._pool is None:
                self._pool = ProcessPoolExecutor(self.workers, mp_context=multiprocessing.get_context("spawn"))
            return self._pool

    def _kill_pool(self, pool: ProcessPoolExecutor) -> None:
        with self._lock:
            if self._pool is pool:
                self._pool = None
        for proc in list(getattr(pool, "_processes", {}).values()):
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        pool.shutdown(wait=False, cancel_futures=True)

    def recognize(self, image: bytes, langs: list[str]) -> OCRPage:
        if self.inline is not None:
            return self.inline.recognize(image, langs)
        pool = self._get_pool()
        try:
            return pool.submit(_ocr_task, self.engine, image, langs).result(timeout=self.timeout_s)
        except FutureTimeout as e:
            self._kill_pool(pool)
            raise OCRTimeout(f"OCR exceeded {self.timeout_s:.0f}s on one page") from e
        except BrokenProcessPool:
            self._kill_pool(pool)
            raise

    def close(self) -> None:
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            self._kill_pool(pool)

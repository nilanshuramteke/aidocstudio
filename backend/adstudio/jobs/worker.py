"""Worker pool: claims jobs, runs registered handlers, heartbeats, recovers stale jobs."""
import logging
import threading
import time
import uuid
from typing import Callable

from ..core.errors import NonRetryable
from .queue import Job, JobQueue

log = logging.getLogger("adstudio.jobs")


class JobContext:
    def __init__(self, queue: JobQueue, job: Job):
        self._q, self.job = queue, job

    def progress(self, value: float) -> None:
        self._q.set_progress(self.job.id, value)

    def cancelled(self) -> bool:
        return self._q.is_cancelled(self.job.id)


Handler = Callable[[JobContext], None]


class WorkerPool:
    def __init__(self, queue: JobQueue, handlers: dict[str, Handler], *, workers: int = 2,
                 heartbeat_s: float = 10.0, stale_after_s: float = 60.0, poll_s: float = 1.0):
        self.q, self.handlers = queue, handlers
        self.n, self.heartbeat_s, self.stale_after_s, self.poll_s = workers, heartbeat_s, stale_after_s, poll_s
        self.worker_id = f"w-{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        self.q.requeue_stale(self.stale_after_s)  # startup recovery
        targets = [self._loop for _ in range(self.n)] + [self._maintenance]
        for i, t in enumerate(targets):
            th = threading.Thread(target=t, name=f"adstudio-job-{i}", daemon=True)
            th.start()
            self._threads.append(th)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self.q.wake.set()
        for th in self._threads:
            th.join(timeout)

    def _maintenance(self) -> None:
        last_recover = time.monotonic()
        while not self._stop.wait(self.heartbeat_s):
            try:
                self.q.heartbeat(self.worker_id)
                if time.monotonic() - last_recover >= 60:
                    self.q.requeue_stale(self.stale_after_s)
                    last_recover = time.monotonic()
            except Exception:
                log.exception("maintenance failed")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.q.claim(self.worker_id, list(self.handlers))
            except Exception:
                log.exception("claim failed")
                self._stop.wait(self.poll_s)
                continue
            if job is None:
                self.q.wake.wait(self.poll_s)
                self.q.wake.clear()
                continue
            self._run(job)

    def _run(self, job: Job) -> None:
        try:
            self.handlers[job.kind](JobContext(self.q, job))
        except NonRetryable as e:
            self.q.fail(job, self.worker_id, str(e), retryable=False)
        except Exception as e:  # noqa: BLE001 - any handler failure is a job failure
            log.exception("job %s (%s) failed", job.id, job.kind)
            self.q.fail(job, self.worker_id, f"{type(e).__name__}: {e}")
        else:
            self.q.complete(job.id, self.worker_id)

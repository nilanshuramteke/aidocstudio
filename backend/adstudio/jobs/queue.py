"""Durable job queue on SQLite. A stage = one job row; handlers must be idempotent."""
import json
import threading
from dataclasses import dataclass

from ..core.events import EventBus
from ..core.ids import new_id
from ..core.timeutil import iso_in, now_iso
from ..storage.db import Database

BACKOFF_S = (5, 30, 300)


@dataclass
class Job:
    id: str
    kind: str
    document_id: str | None
    payload: dict
    attempts: int
    max_attempts: int


def _row_to_job(r) -> Job:
    return Job(r["id"], r["kind"], r["document_id"], json.loads(r["payload_json"] or "{}"),
               r["attempts"], r["max_attempts"])


class JobQueue:
    def __init__(self, db: Database, bus: EventBus | None = None):
        self.db = db
        self.bus = bus
        self.wake = threading.Event()  # set on enqueue so workers don't wait for the poll

    def _emit(self, job_id: str, status: str, **extra) -> None:
        if self.bus:
            self.bus.publish("job.updated", {"id": job_id, "status": status, **extra})

    def enqueue(self, kind: str, payload: dict | None = None, *, document_id: str | None = None,
                priority: int = 5, max_attempts: int = 3, delay_s: float = 0) -> str:
        job_id = new_id()
        with self.db.write() as c:
            c.execute(
                "INSERT INTO jobs(id,kind,document_id,payload_json,priority,max_attempts,run_after,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (job_id, kind, document_id, json.dumps(payload or {}), priority, max_attempts,
                 iso_in(delay_s) if delay_s else None, now_iso()),
            )
        self._emit(job_id, "queued", kind=kind)
        self.wake.set()
        return job_id

    def claim(self, worker_id: str, kinds: list[str] | None = None) -> Job | None:
        now = now_iso()
        kind_sql, params = "", [worker_id, now, now, now]
        if kinds:
            kind_sql = f" AND kind IN ({','.join('?' * len(kinds))})"
            params += kinds
        with self.db.write() as c:
            row = c.execute(
                "UPDATE jobs SET status='running', locked_by=?, locked_at=?, heartbeat_at=?, attempts=attempts+1"
                " WHERE id=(SELECT id FROM jobs WHERE status='queued' AND (run_after IS NULL OR run_after<=?)"
                f"{kind_sql} ORDER BY priority, created_at LIMIT 1) RETURNING *",
                params,
            ).fetchone()
        if row is None:
            return None
        self._emit(row["id"], "running", kind=row["kind"])
        return _row_to_job(row)

    def heartbeat(self, worker_id: str) -> None:
        with self.db.write() as c:
            c.execute("UPDATE jobs SET heartbeat_at=? WHERE status='running' AND locked_by=?",
                      (now_iso(), worker_id))

    def set_progress(self, job_id: str, progress: float) -> None:
        with self.db.write() as c:
            c.execute("UPDATE jobs SET progress=? WHERE id=? AND status='running'", (progress, job_id))
        self._emit(job_id, "running", progress=progress)

    def complete(self, job_id: str, worker_id: str) -> bool:
        with self.db.write() as c:
            n = c.execute(
                "UPDATE jobs SET status='done', progress=1, finished_at=?, locked_by=NULL"
                " WHERE id=? AND status='running' AND locked_by=?", (now_iso(), job_id, worker_id)).rowcount
        if n:
            self._emit(job_id, "done")
        return bool(n)

    def fail(self, job: Job, worker_id: str, error: str, *, retryable: bool = True) -> str:
        """Returns the resulting status ('queued' for a retry, else 'failed')."""
        final = not retryable or job.attempts >= job.max_attempts
        with self.db.write() as c:
            if final:
                n = c.execute(
                    "UPDATE jobs SET status='failed', error=?, finished_at=?, locked_by=NULL"
                    " WHERE id=? AND status='running' AND locked_by=?",
                    (error, now_iso(), job.id, worker_id)).rowcount
            else:
                delay = BACKOFF_S[min(job.attempts - 1, len(BACKOFF_S) - 1)]
                n = c.execute(
                    "UPDATE jobs SET status='queued', error=?, run_after=?, locked_by=NULL"
                    " WHERE id=? AND status='running' AND locked_by=?",
                    (error, iso_in(delay), job.id, worker_id)).rowcount
        status = "failed" if final else "queued"
        if n:
            self._emit(job.id, status, error=error)
        return status

    def is_cancelled(self, job_id: str) -> bool:
        with self.db.read() as c:
            r = c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        return r is not None and r["status"] == "cancelled"

    def cancel(self, job_id: str) -> bool:
        with self.db.write() as c:
            n = c.execute("UPDATE jobs SET status='cancelled', finished_at=?, locked_by=NULL"
                          " WHERE id=? AND status IN ('queued','running')", (now_iso(), job_id)).rowcount
        if n:
            self._emit(job_id, "cancelled")
        return bool(n)

    def retry(self, job_id: str) -> bool:
        with self.db.write() as c:
            n = c.execute("UPDATE jobs SET status='queued', attempts=0, error=NULL, run_after=NULL,"
                          " finished_at=NULL WHERE id=? AND status IN ('failed','cancelled')",
                          (job_id,)).rowcount
        if n:
            self._emit(job_id, "queued")
            self.wake.set()
        return bool(n)

    def requeue_stale(self, stale_after_s: float) -> int:
        """Restart recovery: running jobs whose heartbeat stopped go back to the queue (or fail)."""
        cutoff = iso_in(-stale_after_s)
        with self.db.write() as c:
            requeued = c.execute(
                "UPDATE jobs SET status='queued', locked_by=NULL, error='worker lost; requeued'"
                " WHERE status='running' AND heartbeat_at<? AND attempts<max_attempts", (cutoff,)).rowcount
            failed = c.execute(
                "UPDATE jobs SET status='failed', locked_by=NULL, finished_at=?, error='worker lost; attempts exhausted'"
                " WHERE status='running' AND heartbeat_at<? AND attempts>=max_attempts",
                (now_iso(), cutoff)).rowcount
        if requeued:
            self.wake.set()
        return requeued + failed

    def get(self, job_id: str) -> dict | None:
        with self.db.read() as c:
            r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(r) if r else None

    def list(self, status: str | None = None, limit: int = 100) -> list[dict]:
        sql, params = "SELECT * FROM jobs", []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.db.read() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

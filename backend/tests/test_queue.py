import threading
import time

from adstudio.core.errors import NonRetryable
from adstudio.jobs.worker import WorkerPool
from adstudio.core.timeutil import iso_in


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_priority_and_claim_exclusivity(container):
    q = container.queue
    low = q.enqueue("k", priority=5)
    high = q.enqueue("k", priority=1)
    assert q.claim("w1").id == high
    assert q.claim("w2").id == low
    assert q.claim("w3") is None


def test_concurrent_claims_never_double_claim(container):
    q = container.queue
    ids = {q.enqueue("k") for _ in range(30)}
    claimed, lock = [], threading.Lock()

    def grab(name):
        while (j := q.claim(name)) is not None:
            with lock:
                claimed.append(j.id)

    ts = [threading.Thread(target=grab, args=(f"w{i}",)) for i in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(claimed) == sorted(ids)


def test_retry_with_backoff_then_fail(container):
    q = container.queue
    jid = q.enqueue("k", max_attempts=2)
    j = q.claim("w")
    assert q.fail(j, "w", "x") == "queued"
    assert q.claim("w") is None  # backoff: run_after in the future
    with container.db.write() as c:
        c.execute("UPDATE jobs SET run_after=NULL")
    j = q.claim("w")
    assert j.attempts == 2
    assert q.fail(j, "w", "x") == "failed"
    assert q.get(jid)["status"] == "failed"


def test_non_retryable_fails_immediately(container):
    q = container.queue
    q.enqueue("k")
    j = q.claim("w")
    assert q.fail(j, "w", "password protected", retryable=False) == "failed"


def test_stale_running_job_is_requeued(container):
    q = container.queue
    jid = q.enqueue("k")
    q.claim("dead-worker")
    assert q.requeue_stale(60) == 0  # heartbeat is fresh
    with container.db.write() as c:
        c.execute("UPDATE jobs SET heartbeat_at=?", (iso_in(-120),))
    assert q.requeue_stale(60) == 1
    assert q.get(jid)["status"] == "queued"


def test_stale_job_with_exhausted_attempts_fails(container):
    q = container.queue
    jid = q.enqueue("k", max_attempts=1)
    q.claim("dead")
    with container.db.write() as c:
        c.execute("UPDATE jobs SET heartbeat_at=?", (iso_in(-120),))
    q.requeue_stale(60)
    assert q.get(jid)["status"] == "failed"


def test_late_completion_after_recovery_is_ignored(container):
    q = container.queue
    jid = q.enqueue("k")
    q.claim("slow")
    with container.db.write() as c:
        c.execute("UPDATE jobs SET heartbeat_at=?", (iso_in(-120),))
    q.requeue_stale(60)
    assert q.complete(jid, "slow") is False  # lost its lock
    assert q.get(jid)["status"] == "queued"


def test_cancel_and_retry(container):
    q = container.queue
    jid = q.enqueue("k")
    assert q.cancel(jid)
    assert q.claim("w") is None
    assert q.retry(jid)
    assert q.claim("w").id == jid


def test_worker_pool_runs_and_handles_failures(container):
    q = container.queue
    ran = []

    def ok(ctx):
        ran.append(ctx.job.payload["n"])

    def bad(ctx):
        raise NonRetryable("corrupt")

    pool = WorkerPool(q, {"ok": ok, "bad": bad}, workers=2, heartbeat_s=0.2, poll_s=0.05)
    pool.start()
    try:
        a = q.enqueue("ok", {"n": 1})
        b = q.enqueue("bad")
        c = q.enqueue("ok", {"n": 2})
        assert wait_for(lambda: all(q.get(i)["status"] in ("done", "failed") for i in (a, b, c)))
        assert q.get(a)["status"] == "done" and q.get(c)["status"] == "done"
        assert q.get(b)["status"] == "failed" and "corrupt" in q.get(b)["error"]
        assert sorted(ran) == [1, 2]  # failed job didn't block the queue
    finally:
        pool.stop()

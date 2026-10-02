"""Phase 0 routes: /health, /settings, /jobs, /events. Thin: all logic lives in services."""
import asyncio
import json

from fastapi import APIRouter, Body, Request
from fastapi.responses import StreamingResponse

from .. import __version__
from ..core.errors import NotFound
from ..core.interfaces import ProviderHealth

router = APIRouter()


def _c(request: Request):
    return request.app.state.c


def _provider_status(p) -> dict:
    if p is None:
        return {"configured": False, "ok": False, "detail": "not configured"}
    try:
        h: ProviderHealth = p.health()
        return {"configured": True, "ok": h.ok, "detail": h.detail}
    except Exception as e:  # noqa: BLE001
        return {"configured": True, "ok": False, "detail": f"{type(e).__name__}: {e}"}


@router.get("/health")
def health(request: Request):
    c = _c(request)
    counts = {}
    with c.db.read() as conn:
        for r in conn.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status"):
            counts[r["status"]] = r["n"]
    return {
        "version": __version__,
        "db": {"ok": c.db.integrity_ok(), "schema_version": c.db.user_version()},
        "capabilities": c.capabilities,
        "providers": {"ocr": _provider_status(c.ocr), "llm": _provider_status(c.llm),
                      "embedding": _provider_status(c.embedding)},
        "jobs": counts,
    }


@router.get("/settings")
def get_settings(request: Request):
    return _c(request).settings.all()


@router.patch("/settings")
def patch_settings(request: Request, changes: dict = Body(...)):
    return _c(request).settings.update(changes)


@router.get("/jobs")
def list_jobs(request: Request, status: str | None = None, limit: int = 100):
    return {"items": _c(request).queue.list(status, min(limit, 500))}


@router.get("/jobs/{job_id}")
def get_job(request: Request, job_id: str):
    job = _c(request).queue.get(job_id)
    if not job:
        raise NotFound(f"Job {job_id} not found")
    return job


@router.post("/jobs/{job_id}/cancel")
def cancel_job(request: Request, job_id: str):
    q = _c(request).queue
    if not q.get(job_id):
        raise NotFound(f"Job {job_id} not found")
    q.cancel(job_id)
    return q.get(job_id)


@router.post("/jobs/{job_id}/retry")
def retry_job(request: Request, job_id: str):
    q = _c(request).queue
    if not q.get(job_id):
        raise NotFound(f"Job {job_id} not found")
    q.retry(job_id)
    return q.get(job_id)


@router.get("/events")
async def events(request: Request):
    c = _c(request)
    bus = c.bus
    q = bus.subscribe()

    async def stream():
        try:
            yield ": connected\n\n"
            while not await request.is_disconnected():
                if c.applock.enabled and c.applock.locked:  # locking ends open streams too
                    yield "event: locked\ndata: {}\n\n"
                    break
                try:
                    event, data = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

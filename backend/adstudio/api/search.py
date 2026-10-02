"""Search routes: query, suggest, saved searches, history, reindex."""
from fastapi import APIRouter, Body, Request

from ..core.errors import AppError

router = APIRouter(prefix="/search")


def _c(request: Request):
    return request.app.state.c


@router.post("")
def search(request: Request, body: dict = Body(...)):
    q = body.get("q", "")
    if not isinstance(q, str):
        raise AppError("`q` must be a string", code="invalid_body")
    return _c(request).search.search(q, body.get("filters"), mode=body.get("mode", "best"),
                                     limit=min(int(body.get("limit", 20)), 100), parse=bool(body.get("parse", True)))


@router.get("/suggest")
def suggest(request: Request, q: str = ""):
    return {"items": _c(request).search.suggest(q)}


@router.get("/saved")
def saved(request: Request):
    return {"items": _c(request).search.saved()}


@router.post("/saved")
def save(request: Request, body: dict = Body(...)):
    return _c(request).search.save(body.get("name", ""), body.get("query", {}))


@router.delete("/saved/{sid}")
def delete_saved(request: Request, sid: str):
    _c(request).search.delete_saved(sid)
    return {"id": sid, "deleted": True}


@router.get("/history")
def history(request: Request):
    return {"items": _c(request).search.history()}


@router.delete("/history")
def clear_history(request: Request):
    _c(request).search.clear_history()
    return {"cleared": True}


@router.post("/reindex")
def reindex(request: Request):
    """Rebuild chunks + FTS (+ embeddings if a model is configured) for every document."""
    c = _c(request)
    with c.db.read() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM documents WHERE deleted_at IS NULL AND state NOT IN ('imported','failed')")]
    for doc_id in ids:
        c.queue.enqueue("stage:index", document_id=doc_id, priority=8)
    return {"queued": len(ids)}

"""Review routes: field edit/validate/undo/rerun, review queue, completion, bulk actions, correction metrics."""
from fastapi import APIRouter, Body, Request

from ..core.errors import AppError

router = APIRouter()


def _r(request: Request):
    return request.app.state.c.review


@router.patch("/documents/{doc_id}/fields/{key}")
def patch_field(request: Request, doc_id: str, key: str, body: dict = Body(...)):
    return _r(request).update_field(doc_id, key, value=body.get("value"), status=body.get("status"),
                                    force=bool(body.get("force")))


@router.post("/documents/{doc_id}/fields/{key}/validate")
def check_field(request: Request, doc_id: str, key: str, body: dict = Body(...)):
    if not isinstance(body.get("value"), str):
        raise AppError("`value` must be a string", code="invalid_body")
    return _r(request).check_value(doc_id, key, body["value"])


@router.post("/documents/{doc_id}/fields/{key}/rerun")
def rerun_field(request: Request, doc_id: str, key: str, body: dict | None = Body(None)):
    body = body or {}
    return _r(request).rerun(doc_id, key, bbox=body.get("bbox"), page_no=body.get("page_no"))


@router.post("/documents/{doc_id}/fields/undo")
def undo(request: Request, doc_id: str):
    return _r(request).undo(doc_id)


@router.get("/review/queue")
def queue(request: Request, type: str | None = None, min_conf: float | None = None):
    return _r(request).queue(type, min_conf)


@router.get("/review/next")
def next_item(request: Request, after: str | None = None):
    return _r(request).next_item(after)


@router.post("/review/{doc_id}/complete")
def complete(request: Request, doc_id: str):
    return _r(request).complete(doc_id)


@router.get("/review/metrics")
def metrics(request: Request):
    return _r(request).metrics()

"""Entities, relationships and related documents."""
from fastapi import APIRouter, Body, Request

from ..core.errors import AppError

router = APIRouter()


def _k(request: Request):
    return request.app.state.c.knowledge


@router.get("/entities")
def entities(request: Request, q: str = "", kind: str | None = None, limit: int = 25):
    return {"items": _k(request).search(q, kind, limit)}


@router.get("/entities/{eid}")
def entity(request: Request, eid: str):
    return _k(request).get(eid)


@router.get("/entities/{eid}/graph")
def entity_graph(request: Request, eid: str, depth: int = 1):
    return _k(request).graph(eid, depth)


@router.patch("/entities/{eid}")
def rename_entity(request: Request, eid: str, body: dict = Body(...)):
    return _k(request).rename(eid, body.get("name", ""))


@router.post("/entities/{eid}/merge")
def merge_entity(request: Request, eid: str, body: dict = Body(...)):
    into = body.get("into")
    if not isinstance(into, str):
        raise AppError("`into` (target entity id) is required", code="invalid_body")
    return _k(request).merge(eid, into)


@router.get("/documents/{doc_id}/related")
def related(request: Request, doc_id: str):
    c = request.app.state.c
    c.docs.get(doc_id)
    return c.knowledge.related_documents(doc_id)

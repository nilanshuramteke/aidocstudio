"""Tags, collections and bulk actions."""
from fastapi import APIRouter, Body, Request

from ..core.errors import AppError

router = APIRouter()


def _c(request: Request):
    return request.app.state.c


def _ids(body: dict, key: str = "ids") -> list[str]:
    ids = body.get(key)
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise AppError(f"`{key}` must be a list of ids", code="invalid_body")
    return ids


@router.get("/tags")
def tags(request: Request):
    return {"items": _c(request).org.list_tags()}


@router.post("/tags")
def create_tag(request: Request, body: dict = Body(...)):
    return _c(request).org.create_tag(body.get("name", ""), body.get("color"))


@router.patch("/tags/{tag_id}")
def update_tag(request: Request, tag_id: str, body: dict = Body(...)):
    return _c(request).org.update_tag(tag_id, body)


@router.delete("/tags/{tag_id}")
def delete_tag(request: Request, tag_id: str):
    _c(request).org.delete_tag(tag_id)
    return {"id": tag_id, "deleted": True}


@router.post("/documents/{doc_id}/tags")
def tag_document(request: Request, doc_id: str, body: dict = Body(...)):
    c = _c(request)
    c.docs.get(doc_id)
    c.org.tag_documents([doc_id], add=body.get("add", []), remove=body.get("remove", []))
    return {"id": doc_id, "tags": c.org.tags_of(doc_id)}


@router.get("/collections")
def collections(request: Request):
    return {"items": _c(request).org.list_collections()}


@router.post("/collections")
def create_collection(request: Request, body: dict = Body(...)):
    return _c(request).org.create_collection(body.get("name", ""), body.get("kind", "manual"), body.get("query"))


@router.delete("/collections/{cid}")
def delete_collection(request: Request, cid: str):
    _c(request).org.delete_collection(cid)
    return {"id": cid, "deleted": True}


@router.get("/collections/{cid}/documents")
def collection_documents(request: Request, cid: str):
    c = _c(request)
    ids = c.org.document_ids(cid)
    return c.docs.list(ids=ids, limit=200)


@router.post("/collections/{cid}/documents")
def add_documents(request: Request, cid: str, body: dict = Body(...)):
    return {"added": _c(request).org.add_to_collection(cid, _ids(body, "document_ids"))}


@router.delete("/collections/{cid}/documents")
def remove_documents(request: Request, cid: str, body: dict = Body(...)):
    return {"removed": _c(request).org.remove_from_collection(cid, _ids(body, "document_ids"))}


@router.post("/documents/bulk")
def bulk(request: Request, body: dict = Body(...)):
    return _c(request).bulk.run(_ids(body), body.get("action", ""), body.get("params"))

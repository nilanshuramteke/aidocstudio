"""Document types CRUD + extracted fields read API."""
import json

from fastapi import APIRouter, Body, Request

from ..core.errors import AppError
from ..core.interfaces import DocText, PageText
from ..extraction.hybrid import HybridExtractor

router = APIRouter()


def _c(request: Request):
    return request.app.state.c


def _type_json(t) -> dict:
    return {"id": t.id, "name": t.name, "description": t.description, "keywords": t.keywords, "fields": t.fields,
            "builtin": t.id.startswith("builtin:")}


@router.get("/document-types")
def list_types(request: Request):
    return {"items": [_type_json(t) for t in _c(request).types.list()]}


@router.post("/document-types")
def create_type(request: Request, body: dict = Body(...)):
    t = _c(request).types.create(body.get("name", ""), body.get("description", ""), body.get("keywords", []),
                                 body.get("fields", []))
    return _type_json(t)


@router.patch("/document-types/{type_id}")
def update_type(request: Request, type_id: str, body: dict = Body(...)):
    return _type_json(_c(request).types.update(type_id, body))


@router.delete("/document-types/{type_id}")
def delete_type(request: Request, type_id: str):
    _c(request).types.delete(type_id)
    return {"id": type_id, "deleted": True}


@router.post("/document-types/{type_id}/test")
def test_type(request: Request, type_id: str, body: dict = Body(...)):
    """Dry-run extraction of this template against pasted text (rules + LLM if available). Nothing is saved."""
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise AppError("`text` is required", code="invalid_body")
    c = _c(request)
    dt = c.types.get(type_id)
    doc = DocText("test.txt", [PageText(1, text, None)])
    results = HybridExtractor(c.llm, getattr(c.llm, "model", "") if c.llm else "").extract(doc, dt)
    return {"fields": [{"key": r.key, "raw_value": r.raw_value, "value": r.value, "extractor": r.extractor,
                        "grounded": r.grounded, "flag": r.flag} for r in results]}


@router.get("/documents/{doc_id}/fields")
def document_fields(request: Request, doc_id: str):
    c = _c(request)
    d = c.docs.get(doc_id)
    with c.db.read() as conn:
        fields = [dict(r) for r in conn.execute("SELECT * FROM extracted_fields WHERE document_id=? ORDER BY key", (doc_id,))]
        checks = [dict(r) for r in conn.execute(
            "SELECT rule_id, field_key, passed, message FROM validation_results WHERE document_id=?", (doc_id,))]
        dtype = conn.execute("SELECT name FROM document_types WHERE id=?", (d["doc_type_id"],)).fetchone() \
            if d["doc_type_id"] else None
    order = {f["key"]: i for i, f in enumerate(c.types.get(d["doc_type_id"]).fields)} if d["doc_type_id"] else {}
    for f in fields:
        f["bbox"] = json.loads(f.pop("bbox_json")) if f.get("bbox_json") else None
        f["validation"] = [x for x in checks if x["field_key"] == f["key"] and not x["passed"]]
    fields.sort(key=lambda f: order.get(f["key"], 999))
    return {"document_id": doc_id, "doc_type": dtype["name"] if dtype else None, "doc_type_conf": d["doc_type_conf"],
            "degraded": (d["state_detail"] or {}).get("degraded", []), "fields": fields}

"""Document routes. Thin: logic lives in documents.service / documents.pipeline."""
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Body, File, Request, UploadFile
from fastapi.responses import FileResponse, Response

from ..core.errors import AppError, NotFound
from ..documents.pipeline import render_page
from ..documents.service import ImportResult

router = APIRouter(prefix="/documents")


def _c(request: Request):
    return request.app.state.c


@router.post("/import")
def import_files(request: Request, files: list[UploadFile] = File(...)):
    svc = _c(request).docs
    results = [svc.import_stream(f.filename or "unnamed", f.file) for f in files]
    return {"results": [r.as_dict() for r in results]}


@router.post("/import-paths")
def import_paths(request: Request, body: dict = Body(...)):
    """Server-side import of local files/folders (the service only listens on 127.0.0.1)."""
    paths = body.get("paths")
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise AppError("`paths` must be a list of strings", code="invalid_body")
    svc = _c(request).docs
    results: list[ImportResult] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            targets = sorted(x for x in p.rglob("*") if x.is_file())
        elif p.is_file():
            targets = [p]
        else:
            results.append(ImportResult(raw, "rejected", reason="path not found"))
            continue
        for t in targets:
            with open(t, "rb") as f:
                results.append(svc.import_stream(t.name, f, source="upload"))
    return {"results": [r.as_dict() for r in results]}


@router.get("")
def list_documents(request: Request, q: str | None = None, state: str | None = None,
                   review: str | None = None, cursor: str | None = None, limit: int = 50,
                   tag: str | None = None, collection: str | None = None):
    c = _c(request)
    ids = c.org.document_ids(collection) if collection else None
    return c.docs.list(q=q, state=state, review=review, cursor=cursor, limit=limit, tag=tag, ids=ids)


@router.get("/{doc_id}")
def get_document(request: Request, doc_id: str):
    return _c(request).docs.get(doc_id)


@router.delete("/{doc_id}")
def delete_document(request: Request, doc_id: str):
    _c(request).docs.soft_delete(doc_id)
    return {"id": doc_id, "deleted": True}


@router.post("/{doc_id}/restore")
def restore_document(request: Request, doc_id: str):
    svc = _c(request).docs
    svc.restore(doc_id)
    return svc.get(doc_id)


@router.post("/{doc_id}/reprocess")
def reprocess_document(request: Request, doc_id: str, body: dict | None = Body(None)):
    stage = (body or {}).get("from_stage", "paginate")
    return {"job_id": _c(request).docs.reprocess(doc_id, stage)}


@router.get("/{doc_id}/file")
def get_file(request: Request, doc_id: str):
    path, mime, name = _c(request).docs.original_path(doc_id)
    if not path.exists():
        raise NotFound("Original file is missing from the library")
    # Range requests are handled by FileResponse. Never serve uploaded content as HTML/script.
    return FileResponse(path, media_type=mime, headers={
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(name)}",
        "Content-Security-Policy": "sandbox; default-src 'none'",
    })


@router.get("/{doc_id}/pages/{page_no}/image")
def page_image(request: Request, doc_id: str, page_no: int, w: int = 900):
    data = render_page(_c(request).docs, doc_id, page_no, w)
    if data is None:
        raise NotFound("No image for this page", code="no_image")
    return Response(data, media_type="image/webp", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/{doc_id}/pages/{page_no}/text")
def page_text(request: Request, doc_id: str, page_no: int):
    d = _c(request).docs.get(doc_id)
    for p in d["pages"]:
        if p["page_no"] == page_no:
            with _c(request).db.read() as c:
                r = c.execute("SELECT text, text_source FROM document_pages WHERE document_id=? AND page_no=?",
                              (doc_id, page_no)).fetchone()
            return {"page_no": page_no, "text": r["text"], "text_source": r["text_source"]}
    raise NotFound("Page not found")


@router.get("/{doc_id}/pages/{page_no}/words")
def page_words(request: Request, doc_id: str, page_no: int):
    """Word boxes normalized to 0..1 of the page, for highlighting. Empty for native-text pages."""
    import json
    c = _c(request)
    c.docs.get(doc_id)
    with c.db.read() as conn:
        r = conn.execute(
            "SELECT o.words_json, o.provider, p.text_source FROM document_pages p"
            " LEFT JOIN ocr_results o ON o.page_id=p.id WHERE p.document_id=? AND p.page_no=?",
            (doc_id, page_no)).fetchone()
    if r is None:
        raise NotFound("Page not found")
    return {"page_no": page_no, "provider": r["provider"], "text_source": r["text_source"],
            "words": json.loads(r["words_json"]) if r["words_json"] else []}

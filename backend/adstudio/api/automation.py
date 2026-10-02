"""Workflow rules and watch folders."""
from pathlib import Path

from fastapi import APIRouter, Body, Request

from ..core.errors import AppError
from .documents import _under

router = APIRouter()


def _c(request: Request):
    return request.app.state.c


@router.get("/workflows")
def workflows(request: Request):
    return {"items": _c(request).workflows.list()}


@router.post("/workflows")
def create_workflow(request: Request, body: dict = Body(...)):
    return _c(request).workflows.create(body.get("name", ""), body.get("definition"), bool(body.get("enabled", True)))


@router.get("/workflows/{wid}")
def get_workflow(request: Request, wid: str):
    return _c(request).workflows.get(wid)


@router.patch("/workflows/{wid}")
def update_workflow(request: Request, wid: str, body: dict = Body(...)):
    return _c(request).workflows.update(wid, body)


@router.delete("/workflows/{wid}")
def delete_workflow(request: Request, wid: str):
    _c(request).workflows.delete(wid)
    return {"id": wid, "deleted": True}


@router.post("/workflows/{wid}/run")
def run_workflow(request: Request, wid: str, body: dict = Body(...)):
    doc = body.get("document_id")
    if not isinstance(doc, str):
        raise AppError("`document_id` is required", code="invalid_body")
    c = _c(request)
    c.docs.get(doc)
    return c.workflows.run(wid, doc, dry_run=bool(body.get("dry_run", True)))  # dry run unless explicitly disabled


@router.get("/workflows/{wid}/runs")
def workflow_runs(request: Request, wid: str):
    return {"items": _c(request).workflows.runs(wid)}


@router.get("/watch-folders")
def watch_folders(request: Request):
    return {"items": _c(request).watch.list()}


@router.post("/watch-folders")
def create_watch_folder(request: Request, body: dict = Body(...)):
    roots = getattr(request.app.state, "import_roots", None)  # set in server mode: only these folders may be watched
    path = body.get("path", "")
    if roots is not None and not (isinstance(path, str) and _under(Path(path), roots)):
        raise AppError("That folder is not allowed in server mode", code="path_not_allowed", status=403)
    return _c(request).watch.create(body.get("path", ""), bool(body.get("recursive", True)), body.get("after_import", "leave"),
                                    bool(body.get("enabled", True)))


@router.patch("/watch-folders/{fid}")
def update_watch_folder(request: Request, fid: str, body: dict = Body(...)):
    return _c(request).watch.update(fid, body)


@router.delete("/watch-folders/{fid}")
def delete_watch_folder(request: Request, fid: str):
    _c(request).watch.delete(fid)
    return {"id": fid, "deleted": True}


@router.post("/watch-folders/{fid}/scan")
def scan_watch_folder(request: Request, fid: str):
    return _c(request).watch.scan(fid)

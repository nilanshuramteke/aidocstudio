"""Export, backup/restore, audit log."""
import json
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Body, File, Form, Request, UploadFile
from fastapi.responses import FileResponse

from ..core.errors import AppError, NotFound
from ..ops.backup import create_backup, list_backups, prune_auto_backups, stage_restore

router = APIRouter()
_MIME = {"csv": "text/csv", "json": "application/json",
         "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


def _c(request: Request):
    return request.app.state.c


@router.post("/export")
def export(request: Request, body: dict = Body(...)):
    c = _c(request)
    if isinstance(body.get("document_ids"), list):
        ids = body["document_ids"]
    elif body.get("collection_id"):
        ids = c.org.document_ids(body["collection_id"])
    elif isinstance(body.get("query"), dict):
        q = body["query"]
        ids = [r["document"]["id"] for r in c.search.search(q.get("q", ""), q.get("filters"), limit=5000)["results"]]
    else:
        raise AppError("Provide document_ids, collection_id or query", code="invalid_body")
    res = c.exporter.export(ids, body.get("format", "csv"), body.get("fields"))
    return FileResponse(res["path"], media_type=_MIME[res["format"]], filename=res["name"],
                        headers={"X-Export-Rows": str(res["rows"])})


@router.get("/exports/{name}")
def download_export(request: Request, name: str):
    p = _c(request).exporter.path_of(name)
    return FileResponse(p, filename=p.name)


@router.post("/backup")
def backup(request: Request, body: dict | None = Body(None)):
    c = _c(request)
    path = create_backup(c.config.data_root, c.db, passphrase=(body or {}).get("passphrase") or None)
    return {"name": path.name, "size": path.stat().st_size, "encrypted": path.suffix == ".adsbk"}


@router.get("/backups")
def backups(request: Request):
    return {"items": list_backups(_c(request).config.data_root)}


@router.get("/backups/{name}")
def download_backup(request: Request, name: str):
    p = _c(request).config.data_root / "backups" / Path(name).name
    if not p.is_file() or not p.name.startswith("backup-"):
        raise NotFound("Backup not found")
    return FileResponse(p, filename=p.name)


@router.post("/restore")
def restore(request: Request, body: dict = Body(...)):
    """Stage a restore from a backup already in <data>/backups. It is applied on the next start."""
    root = _c(request).config.data_root
    p = root / "backups" / Path(str(body.get("name", ""))).name
    if not p.is_file():
        raise NotFound("Backup not found")
    return stage_restore(root, p, body.get("passphrase") or None, body.get("db_passphrase") or None)


@router.post("/restore/upload")
def restore_upload(request: Request, file: UploadFile = File(...), passphrase: str = Form("")):
    root = _c(request).config.data_root
    with tempfile.NamedTemporaryFile(delete=False, dir=root / "tmp") as tmp:
        shutil.copyfileobj(file.file, tmp)
    try:
        return stage_restore(root, Path(tmp.name), passphrase or None)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


@router.get("/audit")
def audit(request: Request, limit: int = 100):
    c = _c(request)
    with c.db.read() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, at, action, target_type, target_id, before_json, after_json FROM audit_log ORDER BY id DESC LIMIT ?",
            (min(limit, 1000),))]
    return {"items": rows}

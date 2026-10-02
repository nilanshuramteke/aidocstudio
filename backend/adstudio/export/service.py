"""Export documents + extracted fields to CSV / JSON / XLSX files in <data>/exports."""
import csv
import json
import re
from datetime import datetime
from pathlib import Path

from ..core.errors import AppError
from ..storage.db import Database

BASE_COLUMNS = ["id", "title", "original_name", "type", "state", "review_status", "created_at", "tags"]
_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")
FORMATS = ("csv", "json", "xlsx")


def safe_cell(v) -> str:
    """Neutralize spreadsheet formula injection (a cell that starts with = + - @ is executed by Excel)."""
    s = "" if v is None else str(v)
    if s and s[0] in "=+-@\t\r" and not _NUMERIC.match(s):
        return "'" + s
    return s


class ExportService:
    def __init__(self, db: Database, exports_dir: Path):
        self.db, self.dir = db, Path(exports_dir)

    def _load(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        out = []
        with self.db.read() as c:
            q = (f"SELECT d.*, t.name AS type_name FROM documents d LEFT JOIN document_types t ON t.id=d.doc_type_id"
                 f" WHERE d.deleted_at IS NULL AND d.id IN ({','.join('?' * len(ids))}) ORDER BY d.created_at, d.id")
            for d in c.execute(q, ids).fetchall():
                fields = {r["key"]: dict(r) for r in c.execute(
                    "SELECT key, value, raw_value, confidence, status, page_no FROM extracted_fields WHERE document_id=?"
                    " AND status!='rejected'", (d["id"],))}
                tags = [r["name"] for r in c.execute(
                    "SELECT t.name FROM document_tags dt JOIN tags t ON t.id=dt.tag_id WHERE dt.document_id=? ORDER BY t.name", (d["id"],))]
                out.append({"doc": dict(d), "fields": fields, "tags": tags})
        return out

    def export(self, ids: list[str], fmt: str = "csv", fields: list[str] | None = None, name: str | None = None) -> dict:
        if fmt not in FORMATS:
            raise AppError(f"format must be one of {', '.join(FORMATS)}", code="invalid_format")
        rows = self._load(ids)
        if not rows:
            raise AppError("No documents to export", code="nothing_to_export", status=422)
        keys = fields if fields else sorted({k for r in rows for k in r["fields"]})
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = re.sub(r"[^\w\-]+", "_", name or "export")[:40]
        path = self.dir / f"{base}-{stamp}.{fmt}"
        n = 1
        while path.exists():
            path = self.dir / f"{base}-{stamp}-{n}.{fmt}"
            n += 1
        table = [BASE_COLUMNS + keys] + [
            [r["doc"]["id"], r["doc"]["title"], r["doc"]["original_name"], r["doc"]["type_name"] or "", r["doc"]["state"],
             r["doc"]["review_status"], r["doc"]["created_at"], ", ".join(r["tags"])]
            + [(r["fields"].get(k) or {}).get("value") or "" for k in keys] for r in rows]
        if fmt == "csv":
            with open(path, "w", newline="", encoding="utf-8-sig") as f:  # BOM so Excel reads UTF-8
                csv.writer(f).writerows([[safe_cell(c) for c in row] for row in table])
        elif fmt == "xlsx":
            import openpyxl
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Documents"
            for row in table:
                ws.append([safe_cell(c) for c in row])
            wb.save(path)
        else:
            payload = [{"id": r["doc"]["id"], "title": r["doc"]["title"], "original_name": r["doc"]["original_name"],
                        "type": r["doc"]["type_name"], "state": r["doc"]["state"], "review_status": r["doc"]["review_status"],
                        "created_at": r["doc"]["created_at"], "tags": r["tags"],
                        "fields": {k: {"value": v["value"], "confidence": v["confidence"], "status": v["status"],
                                       "page": v["page_no"]} for k, v in r["fields"].items() if not fields or k in fields}}
                       for r in rows]
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"name": path.name, "path": str(path), "rows": len(rows), "format": fmt}

    def path_of(self, name: str) -> Path:
        p = (self.dir / Path(name).name).resolve()  # basename only: no traversal out of the exports folder
        if not p.is_file() or p.parent != self.dir.resolve():
            raise AppError("Export not found", code="not_found", status=404)
        return p

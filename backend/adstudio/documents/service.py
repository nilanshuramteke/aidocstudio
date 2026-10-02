"""Document domain service: import, listing, soft delete, state changes."""
import json
import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from ..core.errors import AppError, NotFound
from ..core.events import EventBus
from ..core.ids import new_id
from ..core.timeutil import now_iso
from ..ingestion.sniff import KINDS, sniff
from ..jobs.queue import JobQueue
from ..storage.db import Database
from ..storage.errors import IntegrityError
from ..storage.filestore import FileStore


log = logging.getLogger("adstudio.documents")


@dataclass
class Limits:
    max_file_bytes: int = 500 * 1024 * 1024
    zip_max_entries: int = 500
    zip_max_total_bytes: int = 2 * 1024 * 1024 * 1024
    zip_max_ratio: float = 200.0  # uncompressed / compressed, per entry
    zip_max_depth: int = 3  # archives inside archives, counted from the uploaded one


class ZipBudget:
    """Entry/byte allowance shared by a whole archive tree, so nesting cannot multiply the limits."""

    def __init__(self, lim: "Limits"):
        self.entries_left, self.bytes_left = lim.zip_max_entries, lim.zip_max_total_bytes

    def take(self, entries: int, nbytes: int) -> str | None:
        if entries > self.entries_left:
            return "archive tree has too many files"
        if nbytes > self.bytes_left:
            return "archive tree is too large when extracted"
        self.entries_left -= entries
        self.bytes_left -= nbytes
        return None


@dataclass
class ImportResult:
    name: str
    status: str  # created | duplicate | rejected
    document_id: str | None = None
    reason: str | None = None
    children: list["ImportResult"] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = {"name": self.name, "status": self.status, "document_id": self.document_id, "reason": self.reason}
        if self.children:
            d["children"] = [c.as_dict() for c in self.children]
        return d


class DocumentService:
    def __init__(self, db: Database, files: FileStore, queue: JobQueue, bus: EventBus,
                 limits: Limits | None = None):
        self.db, self.files, self.queue, self.bus = db, files, queue, bus
        self.limits = limits or Limits()
        self.event_hooks: list = []  # callables (event, doc_id) run on pipeline milestones (workflows listen here)
        self.after_review: list[str] = []  # job kinds enqueued after a human edit (in addition to re-validation)
        self.after_validate: list[str] = []  # job kinds enqueued when a document finishes validation
        self.after_text: list[str] = []  # job kinds enqueued once a document reaches text_ready

    # ── import ────────────────────────────────────────────────
    def import_stream(self, name: str, src: BinaryIO, *, source: str = "upload", depth: int = 0,
                      zip_name: str | None = None, parent_id: str | None = None,
                      budget: "ZipBudget | None" = None) -> ImportResult:
        name = _safe_name(name)
        try:
            tmp, sha, size = self.files.spool(src, self.limits.max_file_bytes)
        except ValueError as e:
            return ImportResult(name, "rejected", reason=str(e))
        if size == 0:
            tmp.unlink(missing_ok=True)
            return ImportResult(name, "rejected", reason="empty file")
        with open(tmp, "rb") as f:
            head = f.read(8192)
        kind = sniff(head, full_path=tmp)
        if kind is None:
            tmp.unlink(missing_ok=True)
            return ImportResult(name, "rejected", reason="unsupported file type")
        if kind == "zip":
            return self._import_zip(name, tmp, depth, source, budget or ZipBudget(self.limits))

        existing = self._live_by_sha(sha)
        if existing:
            tmp.unlink(missing_ok=True)
            return ImportResult(name, "duplicate", document_id=existing)

        mime, ext = KINDS[kind]
        rel = self.files.commit(tmp, sha, ext)
        doc_id, now = new_id(), now_iso()
        try:
            with self.db.write() as c:
                c.execute(
                    "INSERT INTO documents(id,title,original_name,mime,size_bytes,sha256,source,parent_id,state,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (doc_id, Path(name).stem or name, name, mime, size, sha, source, parent_id, "imported", now, now))
                c.execute("INSERT INTO document_versions(document_id,version,file_path,sha256,created_at) VALUES(?,?,?,?,?)",
                          (doc_id, 1, rel, sha, now))
                if zip_name:
                    c.execute("INSERT INTO document_metadata(document_id,key,value) VALUES(?,?,?)",
                              (doc_id, "zip_name", zip_name))
        except IntegrityError:  # lost a race with a concurrent identical import
            return ImportResult(name, "duplicate", document_id=self._live_by_sha(sha))
        self.queue.enqueue("stage:paginate", document_id=doc_id, priority=5)
        self.bus.publish("document.state", {"id": doc_id, "state": "imported"})
        self.emit("document.imported", doc_id)
        return ImportResult(name, "created", document_id=doc_id)

    def _import_zip(self, name: str, tmp: Path, depth: int, source: str, budget: "ZipBudget") -> ImportResult:
        res = ImportResult(name, "created")
        if depth >= self.limits.zip_max_depth:
            tmp.unlink(missing_ok=True)
            return ImportResult(name, "rejected", reason=f"archives nested deeper than {self.limits.zip_max_depth} are not expanded")
        lim = self.limits
        try:
            with zipfile.ZipFile(tmp) as z:
                infos = [i for i in z.infolist() if not i.is_dir()]
                if len(infos) > lim.zip_max_entries:
                    return ImportResult(name, "rejected", reason=f"archive has more than {lim.zip_max_entries} files")
                if sum(i.file_size for i in infos) > lim.zip_max_total_bytes:
                    return ImportResult(name, "rejected", reason="archive is too large when extracted")
                over = budget.take(len(infos), sum(i.file_size for i in infos))
                if over:
                    return ImportResult(name, "rejected", reason=over)
                for i in infos:
                    base = _safe_name(i.filename)
                    if i.flag_bits & 0x1:
                        res.children.append(ImportResult(base, "rejected", reason="encrypted entry"))
                    elif i.compress_size and i.file_size / i.compress_size > lim.zip_max_ratio:
                        res.children.append(ImportResult(base, "rejected", reason="suspicious compression ratio"))
                    else:
                        with z.open(i) as member:  # spool() enforces the real size, not the header's claim
                            res.children.append(self.import_stream(base, member, source="zip", depth=depth + 1,
                                                                   zip_name=name, budget=budget))
        except zipfile.BadZipFile:
            return ImportResult(name, "rejected", reason="corrupt archive")
        finally:
            tmp.unlink(missing_ok=True)
        return res

    def _live_by_sha(self, sha: str) -> str | None:
        with self.db.read() as c:
            r = c.execute("SELECT id FROM documents WHERE sha256=? AND deleted_at IS NULL", (sha,)).fetchone()
        return r["id"] if r else None

    # ── queries ───────────────────────────────────────────────
    def get(self, doc_id: str, *, include_deleted: bool = False) -> dict:
        with self.db.read() as c:
            r = c.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
            if not r or (r["deleted_at"] and not include_deleted):
                raise NotFound(f"Document {doc_id} not found")
            d = dict(r)
            d["state_detail"] = json.loads(d["state_detail"]) if d["state_detail"] else None
            d["pages"] = [dict(p) for p in c.execute(
                "SELECT page_no,width,height,text_source,ocr_conf FROM document_pages WHERE document_id=? ORDER BY page_no",
                (doc_id,))]
            d["metadata"] = {m["key"]: m["value"] for m in c.execute(
                "SELECT key,value FROM document_metadata WHERE document_id=?", (doc_id,))}
            d["tags"] = [t["name"] for t in c.execute(
                "SELECT t.name FROM document_tags dt JOIN tags t ON t.id=dt.tag_id WHERE dt.document_id=? ORDER BY t.name", (doc_id,))]
        return d

    def list(self, *, q: str | None = None, state: str | None = None, review: str | None = None,
             cursor: str | None = None, limit: int = 50, tag: str | None = None, ids: list[str] | None = None) -> dict:
        where, params = ["deleted_at IS NULL"], []
        if tag:
            where.append("EXISTS (SELECT 1 FROM document_tags dt JOIN tags t ON t.id=dt.tag_id WHERE dt.document_id=documents.id AND t.name=?)")
            params.append(tag)
        if ids is not None:
            where.append(f"id IN ({','.join('?' * len(ids))})" if ids else "0")
            params += ids
        if q:
            where.append("(title LIKE ? ESCAPE '\\' OR original_name LIKE ? ESCAPE '\\')")
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params += [like, like]
        if state:
            where.append("state=?"); params.append(state)
        if review:
            where.append("review_status=?"); params.append(review)
        if cursor:  # ids are ULIDs: newest first, so the cursor is "ids smaller than"
            where.append("id<?"); params.append(cursor)
        limit = max(1, min(limit, 200))
        sql = ("SELECT id,title,original_name,mime,size_bytes,state,review_status,doc_type_id,doc_type_conf,page_count,"
               f"created_at FROM documents WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?")
        with self.db.read() as c:
            rows = [dict(r) for r in c.execute(sql, params + [limit + 1])]
        nxt = rows[limit - 1]["id"] if len(rows) > limit else None
        return {"items": rows[:limit], "next_cursor": nxt}

    def original_path(self, doc_id: str) -> tuple[Path, str, str]:
        d = self.get(doc_id)
        with self.db.read() as c:
            v = c.execute("SELECT file_path FROM document_versions WHERE document_id=? AND version=?",
                          (doc_id, d["current_version"])).fetchone()
        return self.files.abs(v["file_path"]), d["mime"], d["original_name"]

    # ── mutations ─────────────────────────────────────────────
    def soft_delete(self, doc_id: str) -> None:
        self.get(doc_id)
        with self.db.write() as c:
            c.execute("UPDATE documents SET deleted_at=?, updated_at=? WHERE id=?", (now_iso(), now_iso(), doc_id))
            c.execute("INSERT INTO audit_log(at,action,target_type,target_id) VALUES(?,?,?,?)",
                      (now_iso(), "document.delete", "document", doc_id))
        self.emit("document.deleted", doc_id)

    def restore(self, doc_id: str) -> None:
        d = self.get(doc_id, include_deleted=True)
        if not d["deleted_at"]:
            return
        if self._live_by_sha(d["sha256"]):
            raise AppError("An identical document already exists", code="duplicate", status=409)
        with self.db.write() as c:
            c.execute("UPDATE documents SET deleted_at=NULL, updated_at=? WHERE id=?", (now_iso(), doc_id))
            c.execute("INSERT INTO audit_log(at,action,target_type,target_id) VALUES(?,?,?,?)",
                      (now_iso(), "document.restore", "document", doc_id))
        self.emit("document.restored", doc_id)

    _STATE_EVENTS = {"classified": "document.classified", "extracted": "document.extracted", "ready": "document.ready",
                     "needs_review": "document.needs_review"}

    def emit(self, event: str, doc_id: str) -> None:
        for hook in self.event_hooks:
            try:
                hook(event, doc_id)
            except Exception:  # noqa: BLE001 - listeners must never break the pipeline
                log.exception("event hook failed for %s", event)

    def set_state(self, doc_id: str, state: str, detail: dict | None = None) -> None:
        with self.db.write() as c:
            c.execute("UPDATE documents SET state=?, state_detail=?, updated_at=? WHERE id=?",
                      (state, json.dumps(detail) if detail else None, now_iso(), doc_id))
        self.bus.publish("document.state", {"id": doc_id, "state": state})
        if state in self._STATE_EVENTS:
            self.emit(self._STATE_EVENTS[state], doc_id)

    def reprocess(self, doc_id: str, from_stage: str = "paginate") -> str:
        """Re-run from a stage. 'text' forces fresh OCR; 'paginate' rebuilds pages first (OCR cache is reused)."""
        self.get(doc_id)
        if from_stage == "text":
            self.set_state(doc_id, "paged")
            return self.queue.enqueue("stage:text", {"force": True}, document_id=doc_id, priority=1)
        if from_stage in ("classify", "extract", "validate"):
            self.set_state(doc_id, "text_ready" if from_stage == "classify" else "classified" if from_stage == "extract"
                           else "extracted")
            return self.queue.enqueue(f"stage:{from_stage}", document_id=doc_id, priority=1)
        if from_stage != "paginate":
            raise AppError(f"Unknown stage '{from_stage}'", code="invalid_stage")
        self.set_state(doc_id, "imported")
        return self.queue.enqueue("stage:paginate", document_id=doc_id, priority=1)


def _safe_name(raw: str) -> str:
    """Basename only; archive/upload names never reach the filesystem, this is for display + sanity."""
    base = PurePosixPath(raw.replace("\\", "/")).name.strip().strip(".")
    return (base or "unnamed")[:255]

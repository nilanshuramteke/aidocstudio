"""Human review: edit/accept/reject fields, undo (audit-backed), region re-extract, queue, metrics, bulk actions."""
import json
from dataclasses import replace

from ..core.errors import AppError, NotFound
from ..core.ids import new_id
from ..core.interfaces import DocText, DocType, LLMProvider, OCRWord, PageText
from ..core.timeutil import now_iso
from ..extraction.confidence import Thresholds, base_confidence
from ..extraction.grounding import key as gkey
from ..extraction.hybrid import HybridExtractor
from ..extraction.normalize import normalize
from ..ocr.runner import OCRRunner
from ..search.service import SearchService
from ..storage.db import Database
from ..storage.settings import SettingsStore
from ..validation.builtin import validate_field
from .extract_stage import USER_STATUSES, load_doc_text, review_counts, run_validation
from .pipeline import crop_png
from .service import DocumentService
from .types import DocumentTypes

SNAP_COLS = ("raw_value", "value", "confidence", "base_confidence", "page_no", "bbox_json", "status", "flag_reason", "extractor")
REGION_EXTRACTOR_CONF = 0.90


class ReviewService:
    def __init__(self, svc: DocumentService, types: DocumentTypes, settings: SettingsStore, search: SearchService,
                 runner: OCRRunner | None, llm: LLMProvider | None):
        self.svc, self.types, self.settings, self.search, self.runner, self.llm = svc, types, settings, search, runner, llm
        self.db: Database = svc.db

    # ── helpers ───────────────────────────────────────────────
    def _th(self) -> Thresholds:
        s = self.settings.all()
        return Thresholds(s["review.auto_accept"], s["review.soft_review"])

    def _field(self, doc_id: str, key: str) -> dict:
        self.svc.get(doc_id)
        with self.db.read() as c:
            r = c.execute("SELECT * FROM extracted_fields WHERE document_id=? AND key=?", (doc_id, key)).fetchone()
        if not r:
            raise NotFound(f"Field '{key}' not found on this document")
        return dict(r)

    def _fdef(self, doc_id: str, key: str) -> dict:
        d = self.svc.get(doc_id)
        dt = self.types.get(d["doc_type_id"]) if d["doc_type_id"] else self.types.by_name("Other")
        return next((f for f in dt.fields if f["key"] == key), {"key": key, "kind": "text"})

    @staticmethod
    def _snap(row: dict) -> dict:
        return {k: row[k] for k in SNAP_COLS} | {"key": row["key"]}

    def _write(self, doc_id: str, key: str, new: dict, *, before: dict, extra: dict | None = None) -> None:
        """Persist new column values + one audit entry (the undo record) in a single transaction."""
        now = now_iso()
        cols = [c for c in SNAP_COLS if c in new]
        with self.db.write() as c:
            c.execute(f"UPDATE extracted_fields SET {', '.join(f'{k}=?' for k in cols)}, updated_at=? WHERE document_id=? AND key=?",
                      [new[k] for k in cols] + [now, doc_id, key])
            after = self._snap(dict(c.execute("SELECT * FROM extracted_fields WHERE document_id=? AND key=?", (doc_id, key)).fetchone()))
            c.execute("INSERT INTO audit_log(at,action,target_type,target_id,before_json,after_json) VALUES(?,?,?,?,?,?)",
                      (now, "field.update", "field", doc_id, json.dumps(before), json.dumps(after | (extra or {}))))

    def _after_change(self, doc_id: str) -> None:
        run_validation(self.svc, self.types, self.settings, doc_id)  # cross-field rules react to the edit
        self.search.index_fields(doc_id)  # corrected values become searchable immediately
        for kind in self.svc.after_review:
            self.svc.queue.enqueue(kind, document_id=doc_id, priority=3)

    def get_field(self, doc_id: str, key: str) -> dict:
        row = self._field(doc_id, key)
        with self.db.read() as c:
            row["validation"] = [dict(r) for r in c.execute(
                "SELECT rule_id, message FROM validation_results WHERE document_id=? AND field_key=? AND passed=0", (doc_id, key))]
        row["bbox"] = json.loads(row.pop("bbox_json")) if row.get("bbox_json") else None
        return row

    # ── edit / accept / reject ────────────────────────────────
    def check_value(self, doc_id: str, key: str, value: str) -> dict:
        """Live validation while typing: normalized value + failing checks. Saves nothing."""
        self._field(doc_id, key)
        fd = self._fdef(doc_id, key)
        norm = normalize(fd.get("kind", "text"), value)
        if norm is None:
            return {"ok": False, "value": None, "messages": [f"Not a valid {fd.get('kind', 'value')}"]}
        msgs = [m for _, ok, m in validate_field(fd, norm) if not ok]
        return {"ok": not msgs, "value": norm, "messages": msgs}

    def update_field(self, doc_id: str, key: str, *, value: str | None = None, status: str | None = None,
                     force: bool = False) -> dict:
        row = self._field(doc_id, key)
        fd = self._fdef(doc_id, key)
        before = self._snap(row)
        correction_id = None
        if value is not None:
            chk = self.check_value(doc_id, key, value)
            if chk["value"] is None:
                raise AppError(chk["messages"][0], code="invalid_value", status=422)
            if not chk["ok"] and not force:
                raise AppError("; ".join(chk["messages"]), code="validation_failed", status=422)
            if chk["value"] == row["value"] and row["status"] in USER_STATUSES:
                return self.get_field(doc_id, key)  # no-op
            new = {"value": chk["value"], "status": "corrected", "confidence": 1.0, "flag_reason": None}
            if chk["value"] != row["value"]:
                correction_id = new_id()
                with self.db.write() as c:
                    c.execute("INSERT INTO corrections(id,document_id,doc_type_id,field_key,before_value,after_value,context,created_at)"
                              " VALUES(?,?,?,?,?,?,?,?)",
                              (correction_id, doc_id, self.svc.get(doc_id)["doc_type_id"], key,
                               row["value"] if row["value"] is not None else row["raw_value"], chk["value"],
                               self._context_line(doc_id, value, chk["value"]), now_iso()))
        elif status == "accepted":
            if row["value"] is None and row["raw_value"] is None:
                raise AppError("There is no value to accept. Enter one or reject the field.", code="nothing_to_accept", status=409)
            new = {"status": "accepted", "flag_reason": None}
        elif status == "rejected":
            new = {"status": "rejected", "flag_reason": None}
        else:
            raise AppError("Provide `value`, or status accepted|rejected", code="invalid_body")
        self._write(doc_id, key, new, before=before, extra={"correction_id": correction_id})
        self._after_change(doc_id)
        return self.get_field(doc_id, key)

    def _context_line(self, doc_id: str, typed: str, normalized: str) -> str | None:
        """The document line that contains the corrected value: what the model should have read it from."""
        wanted = {gkey(typed), gkey(normalized)} - {""}
        for p in load_doc_text(self.svc, doc_id).pages:
            for line in p.text.splitlines():
                if any(w in gkey(line) for w in wanted) and len(line.strip()) <= 160:
                    return line.strip()
        return None

    # ── undo ──────────────────────────────────────────────────
    def undo(self, doc_id: str) -> dict:
        self.svc.get(doc_id)
        with self.db.read() as c:
            rows = c.execute("SELECT id, action, before_json, after_json FROM audit_log WHERE target_id=?"
                             " AND action IN ('field.update','field.undo') ORDER BY id DESC", (doc_id,)).fetchall()
        undone: set[int] = set()
        target = None
        for r in rows:
            if r["action"] == "field.undo":
                undone.add(json.loads(r["after_json"])["undone"])
            elif r["id"] not in undone:
                target = r
                break
        if target is None:
            raise AppError("Nothing to undo", code="nothing_to_undo", status=409)
        before, after = json.loads(target["before_json"]), json.loads(target["after_json"])
        key = before["key"]
        with self.db.write() as c:
            cols = [k for k in SNAP_COLS]
            c.execute(f"UPDATE extracted_fields SET {', '.join(f'{k}=?' for k in cols)}, updated_at=? WHERE document_id=? AND key=?",
                      [before[k] for k in cols] + [now_iso(), doc_id, key])
            if after.get("correction_id"):
                c.execute("DELETE FROM corrections WHERE id=?", (after["correction_id"],))
            c.execute("INSERT INTO audit_log(at,action,target_type,target_id,after_json) VALUES(?,?,?,?,?)",
                      (now_iso(), "field.undo", "field", doc_id, json.dumps({"undone": target["id"], "key": key})))
        self._after_change(doc_id)
        return self.get_field(doc_id, key)

    # ── re-extract ────────────────────────────────────────────
    def rerun(self, doc_id: str, key: str, *, bbox: list[float] | None = None, page_no: int | None = None) -> dict:
        row = self._field(doc_id, key)
        fd = self._fdef(doc_id, key)
        before = self._snap(row)
        if bbox is not None:
            if page_no is None or len(bbox) != 4 or not all(isinstance(v, (int, float)) for v in bbox):
                raise AppError("Region needs page_no and bbox [x, y, w, h] (0..1)", code="invalid_body")
            x, y, w, h = bbox
            if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1 and x + w <= 1.0001 and y + h <= 1.0001):
                raise AppError("Region is outside the page", code="invalid_region")
            text, conf, extractor = self._read_region(doc_id, page_no, bbox)
            if not text.strip():
                raise AppError("No text found in that region", code="empty_region", status=422)
            value = normalize(fd.get("kind", "text"), text)
            base = base_confidence(REGION_EXTRACTOR_CONF, conf, True)
            new = {"raw_value": text.strip(), "value": value, "confidence": base, "base_confidence": base,
                   "page_no": page_no, "bbox_json": json.dumps([round(v, 5) for v in bbox]), "status": "auto",
                   "flag_reason": None, "extractor": extractor}
        else:
            one = DocType("tmp", "tmp", fields=[fd])
            doc = load_doc_text(self.svc, doc_id)
            model = getattr(self.llm, "model", "") if self.llm else ""
            r = HybridExtractor(self.llm, model).extract(doc, one)[0]
            base = base_confidence(r.extractor_conf, r.ocr_conf, r.grounded) if (r.value or r.raw_value) else 0.0
            new = {"raw_value": r.raw_value, "value": r.value, "confidence": base, "base_confidence": base,
                   "page_no": r.page_no, "bbox_json": json.dumps(r.bbox) if r.bbox else None, "status": "auto",
                   "flag_reason": r.flag, "extractor": r.extractor}
        self._write(doc_id, key, new, before=before)
        self._after_change(doc_id)
        return self.get_field(doc_id, key)

    def _read_region(self, doc_id: str, page_no: int, bbox: list[float]) -> tuple[str, float | None, str]:
        with self.db.read() as c:
            p = c.execute("SELECT p.id, p.text_source, o.provider, o.words_json FROM document_pages p"
                          " LEFT JOIN ocr_results o ON o.page_id=p.id WHERE p.document_id=? AND p.page_no=?",
                          (doc_id, page_no)).fetchone()
        if not p:
            raise NotFound("Page not found")
        if p["text_source"] == "native" or p["words_json"] is None and p["text_source"] is None:
            raise AppError("This document has no page image to select a region on", code="no_region_support")
        x, y, w, h = bbox
        if p["provider"] == "pymupdf-textlayer":  # exact words from the PDF itself: no OCR needed
            words = [OCRWord(o["t"], o["x"], o["y"], o["w"], o["h"], o["c"], o.get("line", 0)) for o in json.loads(p["words_json"])]
            inside = [o for o in words if x <= o.x + o.w / 2 <= x + w and y <= o.y + o.h / 2 <= y + h]
            inside.sort(key=lambda o: (round(o.y / max(o.h, 1e-6)), o.x))
            return " ".join(o.text for o in inside), 1.0, "region:textlayer"
        if self.runner is None:
            raise AppError("No OCR engine available to read that region", code="ocr_unavailable", status=503)
        langs = self.settings.all()["ocr.languages"]
        res = self.runner.recognize(crop_png(self.svc, doc_id, page_no, bbox), langs)
        return " ".join(res.text.split()), (res.mean_conf if res.words else None), f"region:{self.runner.engine}"

    # ── queue ─────────────────────────────────────────────────
    def queue(self, type_name: str | None = None, min_conf: float | None = None) -> dict:
        th = self._th()
        sql = ("SELECT d.id, d.title, d.created_at, t.name AS doc_type FROM documents d"
               " LEFT JOIN document_types t ON t.id=d.doc_type_id WHERE d.deleted_at IS NULL AND d.review_status='needs_review'")
        params: list = []
        if type_name:
            sql += " AND lower(t.name)=?"
            params.append(type_name.lower())
        items = []
        with self.db.read() as c:
            for d in c.execute(sql, params).fetchall():
                counts = review_counts(c, d["id"], th)
                if min_conf is not None and not c.execute(
                        "SELECT 1 FROM extracted_fields WHERE document_id=? AND status='needs_review' AND confidence>=?",
                        (d["id"], min_conf)).fetchone():
                    continue
                items.append({"document_id": d["id"], "title": d["title"], "doc_type": d["doc_type"],
                              "created_at": d["created_at"], "blocking": counts["blocking"], "soft": counts["soft"],
                              "fields_left": counts["pending"]})
        items.sort(key=lambda i: (i["blocking"] == 0, i["created_at"]))  # blocking first, then oldest
        return {"items": items, "documents": len(items), "fields_left": sum(i["fields_left"] for i in items)}

    def next_item(self, after: str | None = None) -> dict:
        q = self.queue()["items"]
        pick = next((i for i in q if i["document_id"] != after), q[0] if q else None)
        if not pick:
            return {"document_id": None, "fields": [], "queue": 0}
        th = self._th()
        with self.db.read() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT key, value, raw_value, confidence, flag_reason, page_no, bbox_json FROM extracted_fields"
                " WHERE document_id=? AND status='needs_review'", (pick["document_id"],))]
        for r in rows:
            r["bbox"] = json.loads(r.pop("bbox_json")) if r["bbox_json"] else None
            r["blocking"] = r["flag_reason"] in ("missing_required", "validation_failed", "disagreement") or (r["confidence"] or 0) < th.soft_review
        rows.sort(key=lambda r: (not r["blocking"], r["confidence"] or 0))
        return {"document_id": pick["document_id"], "title": pick["title"], "fields": rows, "queue": len(q)}

    def complete(self, doc_id: str) -> dict:
        self.svc.get(doc_id)
        th = self._th()
        with self.db.read() as c:
            counts = review_counts(c, doc_id, th)
            pending = [dict(r) for r in c.execute(
                "SELECT key, flag_reason, confidence FROM extracted_fields WHERE document_id=? AND status='needs_review'", (doc_id,))]
        blocking = [p["key"] for p in pending if p["flag_reason"] in ("missing_required", "validation_failed", "disagreement")
                    or (p["confidence"] or 0) < th.soft_review]
        if blocking:
            raise AppError(f"Resolve these fields first: {', '.join(blocking)}", code="blocking_fields", status=409)
        for p in pending:  # remaining soft (low-confidence but valid) fields are accepted as-is
            self.update_field(doc_id, p["key"], status="accepted")
        run_validation(self.svc, self.types, self.settings, doc_id)
        return self.svc.get(doc_id)

    # ── bulk ──────────────────────────────────────────────────
    def accept_all_confident(self, doc_id: str) -> int:
        th = self._th()
        with self.db.read() as c:
            keys = [r["key"] for r in c.execute(
                "SELECT key FROM extracted_fields WHERE document_id=? AND status='needs_review' AND flag_reason='low_confidence'"
                " AND confidence>=?", (doc_id, th.soft_review))]
        for k in keys:
            self.update_field(doc_id, k, status="accepted")
        return len(keys)

    def bulk(self, ids: list[str], action: str) -> list[dict]:
        if action not in ("accept_all_confident", "delete", "reprocess"):
            raise AppError("action must be accept_all_confident, delete or reprocess", code="invalid_action")
        out = []
        for doc_id in ids:
            try:
                if action == "accept_all_confident":
                    out.append({"id": doc_id, "ok": True, "accepted": self.accept_all_confident(doc_id)})
                elif action == "delete":
                    self.svc.soft_delete(doc_id)
                    out.append({"id": doc_id, "ok": True})
                else:
                    out.append({"id": doc_id, "ok": True, "job_id": self.svc.reprocess(doc_id)})
            except AppError as e:
                out.append({"id": doc_id, "ok": False, "error": e.detail})
        return out

    # ── metrics ───────────────────────────────────────────────
    def metrics(self) -> dict:
        with self.db.read() as c:
            rows = c.execute("SELECT id, action, before_json, after_json FROM audit_log WHERE action IN ('field.update','field.undo')"
                             " ORDER BY id").fetchall()
            auto_now = c.execute("SELECT COUNT(*) FROM extracted_fields WHERE status='auto' AND value IS NOT NULL").fetchone()[0]
            reviewed_docs = c.execute("SELECT COUNT(*) FROM documents WHERE review_status='reviewed' AND deleted_at IS NULL").fetchone()[0]
        undone = {json.loads(r["after_json"])["undone"] for r in rows if r["action"] == "field.undo"}
        shown = {}  # (doc, key) -> final outcome for fields that were flagged for review
        auto_errors = 0
        for r in rows:
            if r["action"] != "field.update" or r["id"] in undone:
                continue
            b, a = json.loads(r["before_json"]), json.loads(r["after_json"])
            if b["status"] == "needs_review":
                shown[(a.get("key"), r["id"])] = a["status"]
            elif b["status"] == "auto" and a["status"] == "corrected":
                auto_errors += 1
        outcomes = list(shown.values())
        corrected = outcomes.count("corrected")
        return {"fields_flagged_and_resolved": len(outcomes), "accepted": outcomes.count("accepted"), "corrected": corrected,
                "rejected": outcomes.count("rejected"),
                "correction_rate": round(corrected / len(outcomes), 4) if outcomes else None,
                "auto_accept_errors": auto_errors, "auto_fields_unedited": auto_now,
                "auto_accept_error_rate": round(auto_errors / (auto_errors + auto_now), 4) if (auto_errors + auto_now) else None,
                "documents_reviewed": reviewed_docs}

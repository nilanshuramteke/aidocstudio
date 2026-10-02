"""Stages 6-8: classify -> extract -> validate. Each is idempotent; user-resolved fields are never overwritten."""
import json

from ..classification.hybrid import HybridClassifier
from ..core.ids import new_id
from ..core.interfaces import DocText, LLMProvider, OCRWord, PageText
from ..core.timeutil import now_iso
from ..extraction.confidence import Thresholds, base_confidence, decide, final_confidence
from ..extraction.hybrid import HybridExtractor
from ..jobs.worker import JobContext
from ..storage.settings import SettingsStore
from ..validation.builtin import cross_field, validate_field
from .service import DocumentService
from .types import DocumentTypes

USER_STATUSES = ("accepted", "corrected", "rejected")


def load_doc_text(svc: DocumentService, doc_id: str) -> DocText:
    d = svc.get(doc_id)
    pages = []
    with svc.db.read() as c:
        for r in c.execute("SELECT p.page_no, p.text, o.words_json FROM document_pages p"
                           " LEFT JOIN ocr_results o ON o.page_id=p.id WHERE p.document_id=? ORDER BY p.page_no", (doc_id,)):
            words = None
            if r["words_json"]:
                words = [OCRWord(w["t"], w["x"], w["y"], w["w"], w["h"], w["c"], w.get("line", 0), w.get("block", 0))
                         for w in json.loads(r["words_json"])]
            pages.append(PageText(r["page_no"], r["text"] or "", words))
    return DocText(d["original_name"], pages)


def _thresholds(settings: SettingsStore) -> Thresholds:
    s = settings.all()
    return Thresholds(s["review.auto_accept"], s["review.soft_review"])


def _set_degraded(svc: DocumentService, doc_id: str, tag: str | None, reset: bool = False) -> None:
    d = svc.get(doc_id)
    detail = {} if reset else dict(d["state_detail"] or {})
    tags = [t for t in detail.get("degraded", []) if not tag or t != tag]
    if tag:
        tags.append(tag)
    if tags:
        detail["degraded"] = tags
    else:
        detail.pop("degraded", None)
    with svc.db.write() as c:
        c.execute("UPDATE documents SET state_detail=? WHERE id=?", (json.dumps(detail) if detail else None, doc_id))


def make_classify_handler(svc: DocumentService, types: DocumentTypes, llm: LLMProvider | None):
    def handler(ctx: JobContext) -> None:
        doc_id = ctx.job.document_id
        doc = load_doc_text(svc, doc_id)
        classifier = HybridClassifier(llm)  # per job: carries per-run degraded flag
        all_types = types.list()
        res = classifier.classify(doc, all_types)
        t = next((x for x in all_types if x.name == res.type_name), None) or types.by_name("Other")
        with svc.db.write() as c:
            c.execute("UPDATE documents SET doc_type_id=?, doc_type_conf=?, updated_at=? WHERE id=?",
                      (t.id, res.confidence, now_iso(), doc_id))
        _set_degraded(svc, doc_id, None, reset=True)
        if classifier.degraded:
            _set_degraded(svc, doc_id, f"classify:{classifier.degraded}")
        svc.set_state(doc_id, "classified", svc.get(doc_id)["state_detail"])
        svc.queue.enqueue("stage:extract", document_id=doc_id)

    return handler


def make_extract_handler(svc: DocumentService, types: DocumentTypes, settings: SettingsStore, llm: LLMProvider | None):
    def handler(ctx: JobContext) -> None:
        doc_id = ctx.job.document_id
        d = svc.get(doc_id)
        dt = types.get(d["doc_type_id"]) if d["doc_type_id"] else types.by_name("Other")
        doc = load_doc_text(svc, doc_id)
        model = getattr(llm, "model", "") if llm else ""
        with svc.db.read() as c:
            examples = [(r["field_key"], r["before_value"], r["after_value"], r["context"]) for r in c.execute(
                "SELECT field_key, before_value, after_value, context FROM corrections WHERE doc_type_id=?"
                " AND after_value IS NOT NULL AND (context IS NOT NULL OR before_value IS NOT NULL)"
                " ORDER BY created_at DESC LIMIT 20", (dt.id,))]
        extractor = HybridExtractor(llm, model, examples)
        results = extractor.extract(doc, dt)
        keys = {r.key for r in results}
        now = now_iso()
        with svc.db.write() as c:
            kept = {r["key"] for r in c.execute(
                f"SELECT key FROM extracted_fields WHERE document_id=? AND status IN ({','.join('?' * len(USER_STATUSES))})",
                (doc_id, *USER_STATUSES))}
            # drop auto fields that no longer belong (type changed / template edited)
            c.execute("DELETE FROM extracted_fields WHERE document_id=? AND status IN ('auto','needs_review')"
                      + (f" AND key NOT IN ({','.join('?' * len(keys))})" if keys else ""), (doc_id, *keys))
            for r in results:
                if r.key in kept:
                    continue
                base = base_confidence(r.extractor_conf, r.ocr_conf, r.grounded) if r.value or r.raw_value else 0.0
                c.execute(
                    "INSERT INTO extracted_fields(id,document_id,key,raw_value,value,confidence,base_confidence,page_no,"
                    "bbox_json,status,flag_reason,extractor,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(document_id,key) DO UPDATE SET raw_value=excluded.raw_value, value=excluded.value,"
                    " confidence=excluded.confidence, base_confidence=excluded.base_confidence, page_no=excluded.page_no,"
                    " bbox_json=excluded.bbox_json, status=excluded.status, flag_reason=excluded.flag_reason,"
                    " extractor=excluded.extractor, updated_at=excluded.updated_at",
                    (new_id(), doc_id, r.key, r.raw_value, r.value, base, base, r.page_no,
                     json.dumps(r.bbox) if r.bbox else None, "auto", r.flag, r.extractor, now))
        if extractor.degraded:
            _set_degraded(svc, doc_id, f"extract:{extractor.degraded}")
        svc.set_state(doc_id, "extracted", svc.get(doc_id)["state_detail"])
        svc.queue.enqueue("stage:validate", document_id=doc_id)

    return handler


def review_counts(conn, doc_id: str, th: Thresholds) -> dict:
    """Pending (needs_review) fields split into blocking vs soft, plus how many the user already resolved."""
    pending = blocking = resolved = 0
    for r in conn.execute("SELECT status, flag_reason, confidence FROM extracted_fields WHERE document_id=?", (doc_id,)):
        if r["status"] == "needs_review":
            pending += 1
            if r["flag_reason"] in ("missing_required", "validation_failed", "disagreement") or (r["confidence"] or 0) < th.soft_review:
                blocking += 1
        elif r["status"] in USER_STATUSES:
            resolved += 1
    return {"pending": pending, "blocking": blocking, "soft": pending - blocking, "resolved": resolved}


def run_validation(svc: DocumentService, types: DocumentTypes, settings: SettingsStore, doc_id: str) -> None:
    """Stage 8, callable directly (the review UI re-validates synchronously after every edit)."""
    d = svc.get(doc_id)
    dt = types.get(d["doc_type_id"]) if d["doc_type_id"] else types.by_name("Other")
    fdefs = {f["key"]: f for f in dt.fields}
    th = _thresholds(settings)
    with svc.db.read() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM extracted_fields WHERE document_id=?", (doc_id,))]
    values = {r["key"]: r["value"] for r in rows if r["status"] != "rejected"}
    failed: dict[str, list[str]] = {}
    results: list[tuple[str, str | None, bool, str]] = []
    for r in rows:
        fd = fdefs.get(r["key"])
        if fd is None or r["status"] == "rejected":
            continue
        for rule_id, ok, msg in validate_field(fd, r["value"]):
            results.append((rule_id, r["key"], ok, msg))
            if not ok:
                failed.setdefault(r["key"], []).append(msg)
        if r["raw_value"] and r["value"] is None:  # present but unparseable for its kind
            results.append((f"builtin:{fd.get('kind', 'text')}", r["key"], False,
                            f"Could not be read as a valid {fd.get('kind', 'value')}"))
            failed.setdefault(r["key"], []).append("unparseable")
    for rule_id, involved, ok, msg in cross_field(dt.name, values):
        for k in involved:
            results.append((rule_id, k, ok, msg))
            if not ok:
                failed.setdefault(k, []).append(msg)
    now = now_iso()
    with svc.db.write() as c:
        c.execute("DELETE FROM validation_results WHERE document_id=?", (doc_id,))
        for rule_id, key, ok, msg in results:
            c.execute("INSERT INTO validation_results(id,document_id,rule_id,field_key,passed,message,created_at)"
                      " VALUES(?,?,?,?,?,?,?)", (new_id(), doc_id, rule_id, key, int(ok), msg, now))
        for r in rows:
            if r["status"] in USER_STATUSES:
                continue  # human decisions are never overwritten
            fd = fdefs.get(r["key"], {})
            has_value = r["value"] is not None or r["raw_value"] is not None
            conf = final_confidence(r["base_confidence"] or 0.0, r["key"] in failed)
            status, flag = decide(
                conf, th,
                missing_required=(not has_value and bool(fd.get("required"))),
                disagreement=(r["flag_reason"] == "disagreement"),
                validation_failed=r["key"] in failed)
            if not has_value and not fd.get("required"):
                status, flag = "auto", None  # optional field absent: nothing to review
            c.execute("UPDATE extracted_fields SET confidence=?, status=?, flag_reason=?, updated_at=? WHERE id=?",
                      (conf, status, flag, now, r["id"]))
        counts = review_counts(c, doc_id, th)
        review = "needs_review" if counts["pending"] else ("reviewed" if counts["resolved"] else "none")
        was = c.execute("SELECT review_status FROM documents WHERE id=?", (doc_id,)).fetchone()["review_status"]
        c.execute("UPDATE documents SET review_status=?, updated_at=? WHERE id=?", (review, now, doc_id))
    svc.set_state(doc_id, "needs_review" if counts["pending"] else "ready", svc.get(doc_id)["state_detail"])
    svc.bus.publish("review.queue_changed", {"document_id": doc_id})
    if review == "reviewed" and was != "reviewed":
        svc.emit("document.review_resolved", doc_id)


def make_validate_handler(svc: DocumentService, types: DocumentTypes, settings: SettingsStore):
    def handler(ctx: JobContext) -> None:
        run_validation(svc, types, settings, ctx.job.document_id)
        for kind in svc.after_validate:
            svc.queue.enqueue(kind, document_id=ctx.job.document_id, priority=6)

    return handler

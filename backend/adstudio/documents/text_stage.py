"""Text acquisition stage: PDF text layer -> OCR (+ enhance retry) -> per-page results cached in ocr_results."""
import importlib.metadata
import io
import json

import pymupdf
from PIL import Image

from ..core.errors import NonRetryable
from ..core.timeutil import now_iso
from ..jobs.worker import JobContext
from ..ocr.enhance import enhance
from ..ocr.runner import OCRRunner
from ..storage.settings import SettingsStore
from .pipeline import _render_lock
from .service import DocumentService

MIN_TEXT_LAYER_CHARS = 30  # fewer non-space chars than this on a page => treat as scanned
OCR_TARGET_DPI = 250
MAX_OCR_SIDE = 3500


def _words_json(words) -> str:
    return json.dumps([{"t": w.text, "x": round(w.x, 5), "y": round(w.y, 5), "w": round(w.w, 5),
                        "h": round(w.h, 5), "c": round(w.conf, 3), "line": w.line, "block": w.block}
                       for w in words], ensure_ascii=False)


def _provider_version(engine: str) -> str | None:
    pkg = {"rapidocr": "rapidocr-onnxruntime", "tesseract": "pytesseract"}.get(engine)
    try:
        return importlib.metadata.version(pkg) if pkg else None
    except importlib.metadata.PackageNotFoundError:
        return None


def _pdf_page(path, page_no: int):
    """Returns (text_layer_words | None, text, png_bytes | None). Holds the MuPDF lock."""
    with _render_lock, pymupdf.open(path) as pdf:
        page = pdf[page_no - 1]
        W, H = page.rect.width, page.rect.height
        raw = page.get_text("words")
        if sum(len(w[4]) for w in raw) >= MIN_TEXT_LAYER_CHARS:
            from ..core.interfaces import OCRWord
            words = [OCRWord(w[4], w[0] / W, w[1] / H, (w[2] - w[0]) / W, (w[3] - w[1]) / H, 1.0,
                             line=w[6], block=w[5]) for w in raw]
            return words, page.get_text("text"), None
        scale = min(OCR_TARGET_DPI / 72, MAX_OCR_SIDE / max(W, H))
        return None, "", page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")


def _image_page(path, page_no: int) -> bytes:
    with Image.open(path) as im:
        im.seek(page_no - 1)
        img = im.convert("RGB")
    longest = max(img.size)
    if longest > MAX_OCR_SIDE:
        f = MAX_OCR_SIDE / longest
        img = img.resize((round(img.width * f), round(img.height * f)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def make_text_handler(svc: DocumentService, settings: SettingsStore, runner: OCRRunner | None):
    def handler(ctx: JobContext) -> None:
        doc_id, force = ctx.job.document_id, bool(ctx.job.payload.get("force"))
        cfg = settings.all()
        langs, min_conf = cfg["ocr.languages"], cfg["review.ocr_retry_below"]
        path, mime, _ = svc.original_path(doc_id)
        lang_key = "+".join(langs)
        with svc.db.read() as c:
            pages = [dict(r) for r in c.execute(
                "SELECT p.id, p.page_no, p.text_source, o.provider AS o_provider, o.lang AS o_lang FROM document_pages p"
                " LEFT JOIN ocr_results o ON o.page_id=p.id WHERE p.document_id=? ORDER BY p.page_no", (doc_id,))]
        n = len(pages)
        for i, p in enumerate(pages):
            if ctx.cancelled():
                return
            if p["text_source"] == "native":
                continue
            engine_name = runner.engine if runner else None
            if (not force and p["o_provider"] is not None
                    and (p["o_provider"] == "pymupdf-textlayer" or (p["o_provider"] == engine_name and p["o_lang"] == lang_key))):
                continue  # cached for this engine+language
            words, text, png = (None, "", None)
            if mime == "application/pdf":
                words, text, png = _pdf_page(path, p["page_no"])
            else:
                png = _image_page(path, p["page_no"])
            if words is not None:
                _store(svc, p["id"], "pymupdf-textlayer", None, lang_key, text, "text_layer", None, words)
            else:
                if runner is None:
                    err = "No OCR engine available. Install one (pip install rapidocr-onnxruntime) and reprocess."
                    svc.set_state(doc_id, "failed", {"stage": "text", "error": err})
                    raise NonRetryable(err)
                res = runner.recognize(png, langs)
                if res.words and res.mean_conf < min_conf:  # enhance only when the first pass was weak
                    second = runner.recognize(enhance(png), langs)
                    if second.mean_conf > res.mean_conf:
                        res = second
                _store(svc, p["id"], runner.engine, _provider_version(runner.engine), lang_key, res.text, "ocr",
                       res.mean_conf if res.words else None, res.words)
            ctx.progress((i + 1) / n)
        svc.set_state(doc_id, "text_ready")
        for kind in svc.after_text:
            svc.queue.enqueue(kind, document_id=doc_id)

    def guarded(ctx: JobContext) -> None:
        try:
            handler(ctx)
        except NonRetryable:
            raise
        except Exception as e:  # noqa: BLE001
            if ctx.job.attempts >= ctx.job.max_attempts:
                svc.set_state(ctx.job.document_id, "failed",
                              {"stage": "text", "error": f"{type(e).__name__}: {e}", "attempts": ctx.job.attempts})
            raise

    return guarded


def _store(svc, page_id, provider, version, lang, text, source, conf, words) -> None:
    with svc.db.write() as c:
        c.execute("UPDATE document_pages SET text=?, text_source=?, ocr_conf=? WHERE id=?", (text, source, conf, page_id))
        c.execute("INSERT INTO ocr_results(page_id,provider,provider_version,lang,words_json,created_at) VALUES(?,?,?,?,?,?)"
                  " ON CONFLICT(page_id) DO UPDATE SET provider=excluded.provider, provider_version=excluded.provider_version,"
                  " lang=excluded.lang, words_json=excluded.words_json, created_at=excluded.created_at",
                  (page_id, provider, version, lang, _words_json(words), now_iso()))

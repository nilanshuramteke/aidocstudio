"""Pipeline stages. Each stage is idempotent: results are written in one transaction, re-runs overwrite."""
import io
import threading

import pymupdf
from PIL import Image, ImageSequence

from ..core.errors import NonRetryable
from ..core.ids import new_id
from ..jobs.worker import JobContext
from .formats import PAGINATORS as FORMAT_PAGINATORS
from .service import DocumentService

Image.MAX_IMAGE_PIXELS = 200_000_000  # decompression-bomb guard (PIL raises above 2x this)
MAX_PAGES = 5000
_render_lock = threading.Lock()  # MuPDF is not thread-safe


def _paginate_pdf(path) -> tuple[list[dict], dict]:
    try:
        doc = pymupdf.open(path)
    except Exception as e:  # noqa: BLE001
        raise NonRetryable(f"Corrupt PDF: {e}") from e
    with doc:
        if doc.needs_pass:
            raise NonRetryable("PDF is password protected")
        if doc.page_count == 0:
            raise NonRetryable("PDF has no pages")
        if doc.page_count > MAX_PAGES:
            raise NonRetryable(f"PDF has more than {MAX_PAGES} pages")
        pages = [{"w": p.rect.width, "h": p.rect.height, "text": None, "src": None} for p in doc]
        meta = {k: v for k, v in (doc.metadata or {}).items() if v and k in ("title", "author", "creationDate")}
    return pages, meta


def _paginate_image(path) -> tuple[list[dict], dict]:
    try:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            pages = [{"w": float(f.width), "h": float(f.height), "text": None, "src": None}
                     for f in ImageSequence.Iterator(im)]
    except Exception as e:  # noqa: BLE001
        raise NonRetryable(f"Unreadable image: {e}") from e
    return pages, {}


def _paginate_docx(path) -> tuple[list[dict], dict]:
    import docx
    try:
        d = docx.Document(str(path))
    except Exception as e:  # noqa: BLE001
        raise NonRetryable(f"Unreadable DOCX: {e}") from e
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for t in d.tables:
        for row in t.rows:
            parts.append(" | ".join(c.text.strip() for c in row.cells))
    cp = d.core_properties
    meta = {k: v for k, v in (("title", cp.title), ("author", cp.author)) if v}
    return [{"w": None, "h": None, "text": "\n".join(parts), "src": "native"}], meta


def _paginate_txt(path) -> tuple[list[dict], dict]:
    return [{"w": None, "h": None, "text": path.read_text(encoding="utf-8", errors="replace"), "src": "native"}], {}


_PAGINATORS = {"application/pdf": _paginate_pdf, "image/png": _paginate_image, "image/jpeg": _paginate_image,
               "image/tiff": _paginate_image, "image/webp": _paginate_image,
               "application/vnd.openxmlformats-officedocument.wordprocessingml.document": _paginate_docx,
               "text/plain": _paginate_txt} | FORMAT_PAGINATORS


def make_paginate_handler(svc: DocumentService):
    def handler(ctx: JobContext) -> None:
        doc_id = ctx.job.document_id
        try:
            path, mime, _ = svc.original_path(doc_id)
            pages, meta = _PAGINATORS[mime](path)
            attachments = meta.pop("_attachments", [])
        except NonRetryable as e:
            svc.set_state(doc_id, "failed", {"stage": "paginate", "error": str(e)})
            raise
        except Exception as e:  # noqa: BLE001
            if ctx.job.attempts >= ctx.job.max_attempts:
                svc.set_state(doc_id, "failed", {"stage": "paginate", "error": str(e), "attempts": ctx.job.attempts})
            raise
        with svc.db.write() as c:
            # Upsert (keep page ids) so cached OCR results survive a re-paginate.
            c.execute("DELETE FROM document_pages WHERE document_id=? AND page_no>?", (doc_id, len(pages)))
            for n, p in enumerate(pages, 1):
                c.execute(
                    "INSERT INTO document_pages(id,document_id,page_no,width,height,text,text_source)"
                    " VALUES(?,?,?,?,?,?,?) ON CONFLICT(document_id,page_no) DO UPDATE SET"
                    " width=excluded.width, height=excluded.height,"
                    " text=COALESCE(excluded.text, text), text_source=COALESCE(excluded.text_source, text_source)",
                    (new_id(), doc_id, n, p["w"], p["h"], p["text"], p["src"]))
            c.execute("UPDATE documents SET page_count=? WHERE id=?", (len(pages), doc_id))
            for k, v in meta.items():
                c.execute("INSERT INTO document_metadata(document_id,key,value) VALUES(?,?,?)"
                          " ON CONFLICT(document_id,key) DO UPDATE SET value=excluded.value", (doc_id, k, str(v)))
        if meta.get("subject"):
            with svc.db.write() as c:
                c.execute("UPDATE documents SET title=? WHERE id=?", (str(meta["subject"])[:200], doc_id))
        for name, data in attachments:  # email attachments become child documents
            if data:
                svc.import_stream(name, io.BytesIO(data), source="email", parent_id=doc_id)
        svc.set_state(doc_id, "paged")
        svc.queue.enqueue("stage:text", document_id=doc_id, priority=5)

    return handler


# ── rendering (lazy, cached under derived/) ───────────────────
def render_page(svc: DocumentService, doc_id: str, page_no: int, width: int) -> bytes | None:
    """WebP bytes for a page at the given pixel width, or None for non-visual pages (docx/txt)."""
    width = max(64, min(int(width), 2400))
    d = svc.get(doc_id)
    if page_no < 1 or page_no > (d["page_count"] or 0):
        return None
    cache = svc.files.derived_dir(doc_id, "pages") / f"{page_no:04d}_{width}.webp"
    if cache.exists():
        return cache.read_bytes()
    path, mime, _ = svc.original_path(doc_id)
    if mime == "application/pdf":
        with _render_lock, pymupdf.open(path) as pdf:
            page = pdf[page_no - 1]
            pix = page.get_pixmap(matrix=pymupdf.Matrix(width / page.rect.width, width / page.rect.height), alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    elif mime.startswith("image/"):
        with Image.open(path) as im:
            im.seek(page_no - 1)
            img = im.convert("RGB")
            img = img.resize((width, max(1, round(img.height * width / img.width))), Image.LANCZOS)
    else:
        return None
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=82)
    cache.write_bytes(buf.getvalue())
    return buf.getvalue()


def crop_png(svc: DocumentService, doc_id: str, page_no: int, bbox: list[float], dpi: int = 300) -> bytes:
    """PNG of a normalized [x, y, w, h] region of a page, rendered sharply for OCR of a user-drawn box."""
    path, mime, _ = svc.original_path(doc_id)
    x, y, w, h = bbox
    if mime == "application/pdf":
        with _render_lock, pymupdf.open(path) as pdf:
            page = pdf[page_no - 1]
            W, H = page.rect.width, page.rect.height
            clip = pymupdf.Rect(x * W, y * H, (x + w) * W, (y + h) * H)
            s = dpi / 72
            return page.get_pixmap(matrix=pymupdf.Matrix(s, s), clip=clip, alpha=False).tobytes("png")
    with Image.open(path) as im:
        im.seek(page_no - 1)
        img = im.convert("RGB")
    box = (round(x * img.width), round(y * img.height), round((x + w) * img.width), round((y + h) * img.height))
    img = img.crop(box)
    if img.width < 800:  # small crops OCR badly: scale up
        f = 800 / max(img.width, 1)
        img = img.resize((round(img.width * f), round(img.height * f)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()

"""Type detection by magic bytes and content (never by extension)."""
import io
import json
import re
import zipfile

# kind -> (mime, canonical extension)
KINDS = {
    "pdf": ("application/pdf", "pdf"),
    "png": ("image/png", "png"),
    "jpeg": ("image/jpeg", "jpg"),
    "tiff": ("image/tiff", "tif"),
    "webp": ("image/webp", "webp"),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
    "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx"),
    "txt": ("text/plain", "txt"),
    "csv": ("text/csv", "csv"),
    "json": ("application/json", "json"),
    "xml": ("application/xml", "xml"),
    "eml": ("message/rfc822", "eml"),
    "msg": ("application/vnd.ms-outlook", "msg"),
    "zip": ("application/zip", "zip"),
}
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
STRUCTURED_MAX_BYTES = 50 * 1024 * 1024  # don't fully parse bigger JSON/XML just to sniff them
_EML_HEADERS = ("from:", "to:", "subject:", "date:", "received:", "mime-version:", "message-id:", "return-path:", "delivered-to:", "cc:")
_OFFICE = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml", "pptx": "ppt/presentation.xml"}


def sniff(head: bytes, *, full_path=None) -> str | None:
    """Return a KINDS key or None if unsupported. `head` = first bytes; `full_path` needed for containers/text."""
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(OLE_MAGIC):
        return "msg" if full_path is not None and _is_msg(full_path) else None  # legacy .doc/.xls are not supported
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        if full_path is None:
            return "zip"
        try:
            with zipfile.ZipFile(full_path) as z:
                names = set(z.namelist())
        except zipfile.BadZipFile:
            return None
        if "[Content_Types].xml" in names:
            for kind, marker in _OFFICE.items():
                if marker in names:
                    return kind
        return "zip"
    if not _looks_like_text(head):
        return None
    return _text_kind(head, full_path)


def _is_msg(path) -> bool:
    try:
        import olefile
        with olefile.OleFileIO(str(path)) as ole:
            return any(e and e[0].startswith("__properties_version1.0") for e in ole.listdir())
    except Exception:  # noqa: BLE001
        return False


def _text_kind(head: bytes, full_path) -> str:
    text = head.decode("utf-8", errors="ignore").lstrip("﻿")
    stripped = text.lstrip()
    if full_path is not None and stripped[:1] in ("{", "[", "<"):
        try:
            import os
            if os.path.getsize(full_path) <= STRUCTURED_MAX_BYTES:
                data = open(full_path, "rb").read()
                if stripped[:1] in ("{", "["):
                    json.loads(data.decode("utf-8-sig"))
                    return "json"
                import defusedxml.ElementTree as ET
                ET.parse(io.BytesIO(data))
                return "xml"
        except Exception:  # noqa: BLE001 - fall through to plain text
            pass
    lines = [ln for ln in text.splitlines() if ln.strip()][:20]
    if sum(1 for ln in lines if ln.lower().startswith(_EML_HEADERS)) >= 2 and any(
            ln.lower().startswith(("from:", "subject:", "to:")) for ln in lines):
        return "eml"
    rows = lines[:10]
    if len(rows) >= 2:
        for delim in (",", ";", "\t"):
            counts = {ln.count(delim) for ln in rows}
            if len(counts) == 1 and counts.pop() >= 1:
                return "csv"
    return "txt"


def _looks_like_text(head: bytes) -> bool:
    if not head:
        return False
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as e:
        # a multi-byte char cut at the buffer end is fine
        if e.start < len(head) - 4:
            return False
    return True

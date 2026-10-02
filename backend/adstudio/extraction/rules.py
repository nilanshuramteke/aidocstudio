"""Regex candidate extraction driven by each field's `patterns` (see document type schema)."""
import re
from dataclasses import dataclass

from ..core.interfaces import DocText, DocType


@dataclass
class Candidate:
    raw: str
    page_no: int


def extract_by_rules(doc: DocText, doc_type: DocType) -> dict[str, Candidate]:
    out: dict[str, Candidate] = {}
    for f in doc_type.fields:
        pick = f.get("pick", "first")
        found: list[Candidate] = []
        for pattern in f.get("patterns", []):
            try:
                rx = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
            except re.error:
                continue
            for page in doc.pages:
                for m in rx.finditer(page.text):
                    raw = (m.group(1) if m.groups() else m.group(0)).strip()
                    if raw:
                        found.append(Candidate(raw, page.page_no))
            if found:
                break  # earlier patterns are more specific; stop at the first that matches
        if found:
            out[f["key"]] = found[-1] if pick == "last" else found[0]
    return out

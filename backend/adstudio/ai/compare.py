"""Compare documents: per-document structured answers per aspect, a deterministic diff, then a narrative
written from the diff only (the model never sees raw documents together)."""
import re

from ..core.errors import AppError
from ..core.interfaces import LLMProvider
from ..search.service import SearchService
from .faithfulness import content_tokens
from .promptstore import clip, load, parse_json_object

DEFAULT_ASPECTS = ["payment terms", "termination", "liability", "governing law", "term or duration"]
MAX_DOCS = 5
SUPPORT = 0.6


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


class CompareService:
    def __init__(self, search: SearchService, llm: LLMProvider | None):
        self.search, self.llm = search, llm

    def _titles(self, ids: list[str]) -> dict[str, str]:
        with self.search.db.read() as c:
            rows = c.execute(f"SELECT id,title FROM documents WHERE deleted_at IS NULL AND id IN ({','.join('?' * len(ids))})", ids)
            return {r["id"]: r["title"] for r in rows}

    def compare(self, document_ids: list[str], aspects: list[str] | None = None) -> dict:
        if not (2 <= len(document_ids) <= MAX_DOCS):
            raise AppError(f"Pick between 2 and {MAX_DOCS} documents", code="invalid_scope")
        if self.llm is None:
            raise AppError("Connect a local model to compare documents", code="llm_unavailable", status=503)
        titles = self._titles(document_ids)
        missing = [d for d in document_ids if d not in titles]
        if missing:
            raise AppError(f"Unknown document: {missing[0]}", code="not_found", status=404)
        aspects = aspects or DEFAULT_ASPECTS
        rows = []
        for aspect in aspects:
            cells = {}
            for doc_id in document_ids:
                cells[doc_id] = self._cell(doc_id, aspect)
            vals = [_norm(c["answer"]) for c in cells.values() if c["answer"]]
            rows.append({"aspect": aspect, "cells": cells,
                         "differs": len(set(vals)) > 1,
                         "missing_in": [d for d, c in cells.items() if not c["answer"]]})
        return {"documents": [{"id": d, "title": titles[d]} for d in document_ids], "rows": rows,
                "narrative": self._narrative(rows, titles)}

    def _cell(self, doc_id: str, aspect: str) -> dict:
        chunks = self.search.retrieve_chunks(aspect, document_ids=[doc_id], k=3)
        if not chunks:
            return {"answer": None, "source": None}
        listing = "\n\n".join(f'<passage id="{i}" page="{c["page_no"]}">\n{c["text"]}\n</passage>' for i, c in enumerate(chunks, 1))
        schema = {"type": "object", "properties": {"answer": {"type": ["string", "null"]}, "source": {"type": ["integer", "null"]}}}
        try:
            reply = self.llm.chat([{"role": "user", "content": load("compare.v1").format(aspect=aspect, sources=clip(listing, 6000))}],
                                  schema=schema, temperature=0.0)
        except Exception:  # noqa: BLE001
            return {"answer": None, "source": None, "error": "model_error"}
        obj = parse_json_object(reply if isinstance(reply, str) else "".join(reply)) or {}
        ans, src = obj.get("answer"), obj.get("source")
        if not isinstance(ans, str) or not ans.strip():
            return {"answer": None, "source": None}
        idx = src if isinstance(src, int) and 1 <= src <= len(chunks) else None
        pool = set().union(*(content_tokens(c["text"]) for c in (chunks if idx is None else [chunks[idx - 1]])))
        toks = content_tokens(ans)
        if toks and len(toks & pool) / len(toks) < SUPPORT:  # answer not grounded in the passages: drop it
            return {"answer": None, "source": None, "dropped": "unsupported"}
        c = chunks[(idx or 1) - 1]
        return {"answer": ans.strip(), "source": {"chunk_id": c["id"], "page_no": c["page_no"], "title": c["title"]}}

    def _narrative(self, rows: list[dict], titles: dict[str, str]) -> str:
        lines = []
        for r in rows:
            vals = "; ".join(f'{titles[d]}: {c["answer"] or "not stated"}' for d, c in r["cells"].items())
            lines.append(f'- {r["aspect"]} ({"DIFFERS" if r["differs"] else "same/absent"}): {vals}')
        try:
            out = self.llm.chat([{"role": "user", "content": load("compare_narrative.v1").format(table="\n".join(lines))}],
                                temperature=0.0)
        except Exception:  # noqa: BLE001
            return ""
        return (out if isinstance(out, str) else "".join(out)).strip()

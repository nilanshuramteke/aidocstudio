"""Indexing + hybrid retrieval (FTS5 bm25 + vector KNN, fused with RRF) over document chunks."""
import json
import logging
import re

from ..core.errors import AppError, NotFound
from ..core.ids import new_id
from ..core.interfaces import EmbeddingProvider, OCRWord
from ..core.timeutil import now_iso
from ..storage.db import Database
from .chunker import chunk_page
from .query_parser import parse_query
from .vector import VectorIndex

log = logging.getLogger("adstudio.search")
RRF_K = 60
TOP_N = 50
DATE_KEYS = ("invoice_date", "date", "effective_date")
ENTITY_KEYS = ("vendor", "merchant", "customer")
SUMMARY_KEYS = ("vendor", "merchant", "invoice_number", "invoice_date", "date", "total")
MARK_START, MARK_END = "\x02", "\x03"  # snippet highlight sentinels (UI splits on them; never raw HTML)
MAX_PREFILTER = 20000


def fts_query(text: str, *, mode_or: bool = False) -> str | None:
    toks = re.findall(r"\w+", text, re.UNICODE)
    if not toks:
        return None
    parts = [f'"{t}"' for t in toks[:-1]] + [f'"{toks[-1]}"*']  # last token matches as a prefix (type-ahead)
    return (" OR " if mode_or else " ").join(parts)


class SearchService:
    def __init__(self, db: Database, vectors: VectorIndex | None, embedding: EmbeddingProvider | None = None):
        self.db, self.vectors, self.embedding = db, vectors, embedding

    # ── indexing ──────────────────────────────────────────────
    def index_document(self, doc_id: str) -> list[int]:
        with self.db.read() as c:
            pages = [(r["page_no"], r["text"] or "", r["words_json"]) for r in c.execute(
                "SELECT p.page_no, p.text, o.words_json FROM document_pages p LEFT JOIN ocr_results o ON o.page_id=p.id"
                " WHERE p.document_id=? ORDER BY p.page_no", (doc_id,))]
            old = [r[0] for r in c.execute("SELECT rid FROM chunks WHERE document_id=?", (doc_id,))]
        chunks, ord_ = [], 0
        for page_no, text, wj in pages:
            words = [OCRWord(w["t"], w["x"], w["y"], w["w"], w["h"], w["c"]) for w in json.loads(wj)] if wj else None
            cs = chunk_page(page_no, text, words, start_ord=ord_)
            chunks += cs
            ord_ += len(cs)
        if self.vectors:
            self.vectors.delete_chunks(old)
        rids = []
        with self.db.write() as c:
            c.execute("DELETE FROM chunks WHERE document_id=?", (doc_id,))
            for ch in chunks:
                cur = c.execute("INSERT INTO chunks(id,document_id,page_no,ord,text,char_start,char_end,bbox_json)"
                                " VALUES(?,?,?,?,?,?,?,?)",
                                (new_id(), doc_id, ch.page_no, ch.ord, ch.text, ch.char_start, ch.char_end,
                                 json.dumps(ch.bbox) if ch.bbox else None))
                rids.append(cur.lastrowid)
        return rids

    def index_fields(self, doc_id: str) -> None:
        with self.db.write() as c:
            c.execute("DELETE FROM field_fts WHERE document_id=?", (doc_id,))
            for r in c.execute("SELECT key, value FROM extracted_fields WHERE document_id=? AND value IS NOT NULL"
                               " AND status!='rejected'", (doc_id,)).fetchall():
                c.execute("INSERT INTO field_fts(document_id,key,value) VALUES(?,?,?)", (doc_id, r["key"], r["value"]))

    def embed_document(self, doc_id: str, batch: int = 32) -> bool:
        """Embed all chunks of a document. Returns True if the index was reset (embedding model changed)."""
        if not (self.embedding and self.vectors):
            return False
        with self.db.read() as c:
            rows = [(r["rid"], r["text"]) for r in c.execute("SELECT rid, text FROM chunks WHERE document_id=? ORDER BY rid",
                                                             (doc_id,))]
        reset = False
        for i in range(0, len(rows), batch):
            part = rows[i:i + batch]
            vecs = self.embedding.embed([t for _, t in part], kind="doc")
            if i == 0:
                reset = self.vectors.ensure(self.embedding.model_id, len(vecs[0]))
            self.vectors.add([(rid, v) for (rid, _), v in zip(part, vecs)])
        return reset

    def doc_ids_with_chunks(self) -> list[str]:
        with self.db.read() as c:
            return [r[0] for r in c.execute("SELECT DISTINCT document_id FROM chunks")]

    # ── filters ───────────────────────────────────────────────
    @staticmethod
    def _filter_sql(f: dict) -> tuple[str, list]:
        w, p = ["d.deleted_at IS NULL"], []
        if f.get("type"):
            w.append("d.doc_type_id IN (SELECT id FROM document_types WHERE lower(name)=?)")
            p.append(str(f["type"]).lower())
        if f.get("document_ids") is not None:  # scope restriction (Ask AI over selected documents)
            ids = list(f["document_ids"])
            w.append(f"d.id IN ({','.join('?' * len(ids))})" if ids else "0")
            p += ids
        if f.get("tag"):
            w.append("EXISTS (SELECT 1 FROM document_tags dt JOIN tags t ON t.id=dt.tag_id WHERE dt.document_id=d.id AND t.name=?)")
            p.append(str(f["tag"]))
        if f.get("collection"):  # manual collection by id (smart collections resolve to document_ids before searching)
            w.append("EXISTS (SELECT 1 FROM collection_documents cd WHERE cd.document_id=d.id AND cd.collection_id=?)")
            p.append(str(f["collection"]))
        if f.get("review"):
            w.append("d.review_status=?"); p.append(f["review"])
        if f.get("state"):
            w.append("d.state=?"); p.append(f["state"])
        dk = ",".join("?" * len(DATE_KEYS))
        if f.get("date_from"):
            w.append(f"EXISTS (SELECT 1 FROM extracted_fields x WHERE x.document_id=d.id AND x.key IN ({dk}) AND x.value>=?)")
            p += [*DATE_KEYS, f["date_from"]]
        if f.get("date_to"):
            w.append(f"EXISTS (SELECT 1 FROM extracted_fields x WHERE x.document_id=d.id AND x.key IN ({dk}) AND x.value<=?)")
            p += [*DATE_KEYS, f["date_to"]]
        if f.get("amount_min") is not None:
            w.append("EXISTS (SELECT 1 FROM extracted_fields x WHERE x.document_id=d.id AND x.key='total'"
                     " AND CAST(x.value AS REAL)>=?)")
            p.append(float(f["amount_min"]))
        if f.get("amount_max") is not None:
            w.append("EXISTS (SELECT 1 FROM extracted_fields x WHERE x.document_id=d.id AND x.key='total'"
                     " AND CAST(x.value AS REAL)<=?)")
            p.append(float(f["amount_max"]))
        if f.get("entity"):
            ek = ",".join("?" * len(ENTITY_KEYS))
            w.append(f"EXISTS (SELECT 1 FROM extracted_fields x WHERE x.document_id=d.id AND x.key IN ({ek})"
                     " AND x.value LIKE ? ESCAPE '\\')")
            like = "%" + str(f["entity"]).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            p += [*ENTITY_KEYS, like]
        return " AND ".join(w), p

    # ── retrieval ─────────────────────────────────────────────
    def _keyword(self, text: str, where: str, params: list) -> list[dict]:
        for mode_or in (False, True):  # AND first for precision; OR only if nothing matched
            q = fts_query(text, mode_or=mode_or)
            if q is None:
                return []
            with self.db.read() as c:
                rows = c.execute(
                    "SELECT c.rid, c.document_id, c.page_no, c.bbox_json, bm25(chunks_fts) AS score,"
                    f" snippet(chunks_fts, 0, '{MARK_START}', '{MARK_END}', '…', 24) AS snip"
                    " FROM chunks_fts JOIN chunks c ON c.rid=chunks_fts.rowid JOIN documents d ON d.id=c.document_id"
                    f" WHERE chunks_fts MATCH ? AND {where} ORDER BY score LIMIT {TOP_N}", [q, *params]).fetchall()
            if rows:
                return [dict(r) for r in rows]
        return []

    def _semantic(self, text: str, where: str, params: list, filtered: bool) -> tuple[list[dict], str | None]:
        if not (self.embedding and self.vectors and text.strip()):
            return [], "no embedding model configured"
        if self.vectors.active_model() is None:
            return [], "nothing embedded yet"
        try:
            qv = self.embedding.embed([text], kind="query")[0]
        except Exception as e:  # noqa: BLE001
            return [], f"embedding model unavailable ({type(e).__name__})"
        allowed = None
        if filtered:
            with self.db.read() as c:
                allowed = [r[0] for r in c.execute(
                    f"SELECT c.rid FROM chunks c JOIN documents d ON d.id=c.document_id WHERE {where} LIMIT {MAX_PREFILTER}",
                    params)]
        hits = self.vectors.search(qv, TOP_N, allowed)
        if not hits:
            return [], None
        dist = dict(hits)
        with self.db.read() as c:
            rows = c.execute(
                "SELECT c.rid, c.document_id, c.page_no, c.bbox_json, c.text FROM chunks c JOIN documents d ON d.id=c.document_id"
                f" WHERE c.rid IN ({','.join('?' * len(dist))}) AND d.deleted_at IS NULL", list(dist)).fetchall()
        out = [{**dict(r), "score": dist[r["rid"]], "snip": r["text"][:220]} for r in rows]
        out.sort(key=lambda r: r["score"])
        return out, None

    def _field_hits(self, text: str, where: str, params: list) -> list[dict]:
        q = fts_query(text)
        if q is None:
            return []
        with self.db.read() as c:
            rows = c.execute(
                "SELECT field_fts.document_id, field_fts.key, field_fts.value, bm25(field_fts) AS score FROM field_fts"
                f" JOIN documents d ON d.id=field_fts.document_id WHERE field_fts MATCH ? AND {where}"
                f" ORDER BY score LIMIT {TOP_N}", [q, *params]).fetchall()
        return [dict(r) for r in rows]

    def retrieve_chunks(self, text: str, *, document_ids: list[str] | None = None, k: int = 10) -> list[dict]:
        """Top-k chunks by hybrid (keyword + vector) reciprocal-rank fusion, optionally limited to some documents."""
        where, params = self._filter_sql({"document_ids": document_ids} if document_ids is not None else {})
        scores: dict[int, float] = {}
        for rank, r in enumerate(self._keyword(text, where, params), 1):
            scores[r["rid"]] = scores.get(r["rid"], 0.0) + 1.0 / (RRF_K + rank)
        sem, _ = self._semantic(text, where, params, document_ids is not None)
        for rank, r in enumerate(sem, 1):
            scores[r["rid"]] = scores.get(r["rid"], 0.0) + 1.0 / (RRF_K + rank)
        top = sorted(scores, key=lambda r: -scores[r])[:k]
        if not top:
            return []
        with self.db.read() as c:
            rows = {r["rid"]: dict(r) for r in c.execute(
                "SELECT c.rid, c.id, c.document_id, c.page_no, c.bbox_json, c.text, d.title FROM chunks c"
                f" JOIN documents d ON d.id=c.document_id WHERE c.rid IN ({','.join('?' * len(top))})", top)}
        out = []
        for rid in top:
            r = rows[rid]
            r["bbox"] = json.loads(r.pop("bbox_json")) if r["bbox_json"] else None
            r["score"] = scores[rid]
            out.append(r)
        return out

    def _infer_type(self, text: str, parsed: dict) -> str:
        """Turn a bare document-type word ("invoices", or "invoice" next to another filter) into a type filter."""
        with self.db.read() as c:
            names = [r["name"] for r in c.execute("SELECT name FROM document_types")]
        for name in names:
            m = re.search(r"\b" + re.escape(name) + r"(s?)\b", text, re.I)
            if m and (m.group(1) or parsed):
                parsed["type"] = name.lower()
                return re.sub(r"\s+", " ", text[:m.start()] + " " + text[m.end():]).strip()
        return text

    def search(self, q: str, filters: dict | None = None, *, mode: str = "best", limit: int = 20,
               parse: bool = True) -> dict:
        if mode not in ("best", "keyword", "meaning"):
            raise AppError("mode must be best, keyword or meaning", code="invalid_mode")
        limit = max(1, min(limit, 5000))  # the HTTP route caps interactive searches at 100
        parsed, text = parse_query(q) if parse else ({}, q.strip())
        if parse and "type" not in parsed:
            text = self._infer_type(text, parsed)
        active = {**parsed, **(filters or {})}
        where, params = self._filter_sql(active)
        filtered = len(active) > 0

        ranked: dict[str, dict] = {}

        def add(list_name: str, doc_id: str, rank: int, snippet: dict | None = None, field: dict | None = None):
            e = ranked.setdefault(doc_id, {"score": 0.0, "snippets": [], "fields": [], "lists": set()})
            if list_name not in e["lists"]:  # a document counts once per list (its best-ranked hit)
                e["score"] += 1.0 / (RRF_K + rank)
                e["lists"].add(list_name)
            if snippet and len(e["snippets"]) < 3:
                e["snippets"].append(snippet)
            if field:
                e["fields"].append(field)

        semantic = {"available": False, "reason": None}
        if text:
            if mode in ("best", "keyword"):
                for rank, r in enumerate(self._keyword(text, where, params), 1):
                    add("kw", r["document_id"], rank, {"page_no": r["page_no"], "text": r["snip"],
                                                       "bbox": json.loads(r["bbox_json"]) if r["bbox_json"] else None})
                for rank, r in enumerate(self._field_hits(text, where, params), 1):
                    add("field", r["document_id"], rank, field={"key": r["key"], "value": r["value"]})
            if mode in ("best", "meaning"):
                hits, why = self._semantic(text, where, params, filtered)
                semantic = {"available": why is None, "reason": why}
                for rank, r in enumerate(hits, 1):
                    add("vec", r["document_id"], rank, {"page_no": r["page_no"], "text": r["snip"],
                                                        "bbox": json.loads(r["bbox_json"]) if r["bbox_json"] else None})
            order = sorted(ranked, key=lambda d: -ranked[d]["score"])
        else:  # filters only: newest first
            with self.db.read() as c:
                order = [r[0] for r in c.execute(
                    f"SELECT d.id FROM documents d WHERE {where} ORDER BY d.id DESC LIMIT ?", [*params, limit])]
            for d in order:
                ranked[d] = {"score": 0.0, "snippets": [], "fields": [], "lists": set()}
        total = len(order)
        order = order[:limit]
        self._record_history(q)
        return {"query": q, "text": text, "parsed_filters": parsed, "filters": active, "mode": mode,
                "semantic": semantic, "total": total,
                "results": [self._doc_row(d, ranked[d]) for d in order]}

    def _doc_row(self, doc_id: str, e: dict) -> dict:
        with self.db.read() as c:
            d = c.execute("SELECT d.id,d.title,d.mime,d.state,d.review_status,d.page_count,t.name AS doc_type FROM documents d"
                          " LEFT JOIN document_types t ON t.id=d.doc_type_id WHERE d.id=?", (doc_id,)).fetchone()
            fields = {r["key"]: r["value"] for r in c.execute(
                f"SELECT key, value FROM extracted_fields WHERE document_id=? AND key IN ({','.join('?' * len(SUMMARY_KEYS))})"
                " AND value IS NOT NULL", (doc_id, *SUMMARY_KEYS))}
        return {"document": dict(d), "score": round(e["score"], 5), "snippets": e["snippets"],
                "field_hits": e["fields"][:3], "summary": fields}

    # ── history / saved / suggest ─────────────────────────────
    def _record_history(self, q: str) -> None:
        q = q.strip()
        if not q:
            return
        with self.db.write() as c:
            last = c.execute("SELECT q FROM search_history ORDER BY id DESC LIMIT 1").fetchone()
            if not last or last["q"] != q:
                c.execute("INSERT INTO search_history(q,created_at) VALUES(?,?)", (q, now_iso()))

    def history(self, limit: int = 20) -> list[dict]:
        with self.db.read() as c:
            return [dict(r) for r in c.execute("SELECT q, created_at FROM search_history ORDER BY id DESC LIMIT ?", (limit,))]

    def clear_history(self) -> None:
        with self.db.write() as c:
            c.execute("DELETE FROM search_history")

    def suggest(self, prefix: str, limit: int = 8) -> list[dict]:
        prefix = prefix.strip()
        if not prefix:
            return [{"kind": "recent", "text": h["q"]} for h in self.history(limit)]
        like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        out: list[dict] = []
        with self.db.read() as c:
            out += [{"kind": "recent", "text": r[0]} for r in c.execute(
                "SELECT DISTINCT q FROM search_history WHERE q LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?", (like, limit))]
            out += [{"kind": "document", "text": r[0]} for r in c.execute(
                "SELECT title FROM documents WHERE deleted_at IS NULL AND title LIKE ? ESCAPE '\\' LIMIT ?", (like, limit))]
            out += [{"kind": "entity", "text": r[0]} for r in c.execute(
                "SELECT DISTINCT value FROM extracted_fields WHERE key IN ('vendor','merchant','customer') AND value LIKE ?"
                " ESCAPE '\\' LIMIT ?", (like, limit))]
            out += [{"kind": "type", "text": f"type:{r[0].lower()}"} for r in c.execute(
                "SELECT name FROM document_types WHERE name LIKE ? ESCAPE '\\'", (like,))]
        seen, uniq = set(), []
        for s in out:
            if s["text"].lower() not in seen:
                seen.add(s["text"].lower())
                uniq.append(s)
        return uniq[:limit]

    def save(self, name: str, query: dict) -> dict:
        if not name.strip():
            raise AppError("Name is required", code="invalid_name")
        sid = new_id()
        with self.db.write() as c:
            c.execute("INSERT INTO saved_searches(id,name,query_json,created_at) VALUES(?,?,?,?)",
                      (sid, name.strip(), json.dumps(query), now_iso()))
        return {"id": sid, "name": name.strip(), "query": query}

    def saved(self) -> list[dict]:
        with self.db.read() as c:
            return [{"id": r["id"], "name": r["name"], "query": json.loads(r["query_json"])}
                    for r in c.execute("SELECT * FROM saved_searches ORDER BY created_at")]

    def delete_saved(self, sid: str) -> None:
        with self.db.write() as c:
            if not c.execute("DELETE FROM saved_searches WHERE id=?", (sid,)).rowcount:
                raise NotFound("Saved search not found")

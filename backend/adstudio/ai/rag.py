"""Ask AI: scoped hybrid retrieval -> grounded prompt -> streamed answer -> citation post-check -> persistence."""
import json
import logging
from typing import Iterator

from ..core.errors import AppError, NotFound
from ..core.ids import new_id
from ..core.interfaces import LLMProvider
from ..core.timeutil import now_iso
from ..search.service import SearchService
from ..storage.db import Database
from .faithfulness import NOT_FOUND_TEXT, best_quote, check_answer, strip_citations
from .promptstore import load

log = logging.getLogger("adstudio.rag")
PROMPT_VERSION = "rag.v1"
TOP_K = 10
HISTORY_TURNS = 6
SHORT_QUESTION_WORDS = 6  # follow-ups this short borrow the previous question for retrieval
Event = tuple[str, dict]


class RagService:
    def __init__(self, db: Database, search: SearchService, llm: LLMProvider | None, collections=None):
        self.db, self.search, self.llm, self.collections = db, search, llm, collections  # collections: id -> doc ids

    # ── scope ─────────────────────────────────────────────────
    def resolve_scope(self, scope: dict | None) -> list[str] | None:
        """None = all documents. Otherwise an explicit id list (selection)."""
        scope = scope or {"type": "all"}
        if scope.get("document_ids") is not None:
            ids = scope["document_ids"]
            if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
                raise AppError("scope.document_ids must be a list of ids", code="invalid_scope")
            return ids
        if scope.get("collection_id"):
            if self.collections is None:
                raise AppError("Collections are not available", code="invalid_scope")
            return self.collections(scope["collection_id"])
        if scope.get("type", "all") != "all":
            raise AppError("scope must be {type:'all'}, {document_ids:[...]} or {collection_id:'...'}", code="invalid_scope")
        return None

    # ── conversations ─────────────────────────────────────────
    def _conversation(self, conv_id: str | None, scope: dict, first_message: str) -> str:
        now = now_iso()
        with self.db.write() as c:
            if conv_id:
                if not c.execute("SELECT 1 FROM ai_conversations WHERE id=?", (conv_id,)).fetchone():
                    raise NotFound("Conversation not found")
                c.execute("UPDATE ai_conversations SET updated_at=? WHERE id=?", (now, conv_id))
                return conv_id
            conv_id = new_id()
            c.execute("INSERT INTO ai_conversations(id,title,scope_json,created_at,updated_at) VALUES(?,?,?,?,?)",
                      (conv_id, first_message[:80], json.dumps(scope), now, now))
        return conv_id

    def _history(self, conv_id: str) -> list[dict]:
        with self.db.read() as c:
            rows = c.execute("SELECT role, content FROM ai_messages WHERE conversation_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
                             (conv_id, HISTORY_TURNS)).fetchall()
        return [{"role": r["role"], "content": strip_citations(r["content"])} for r in reversed(rows)]

    def _save_message(self, conv_id: str, role: str, content: str, extra: dict | None = None, model: str | None = None) -> str:
        mid = new_id()
        with self.db.write() as c:
            c.execute("INSERT INTO ai_messages(id,conversation_id,role,content,citations_json,model,created_at) VALUES(?,?,?,?,?,?,?)",
                      (mid, conv_id, role, content, json.dumps(extra) if extra is not None else None, model, now_iso()))
        return mid

    def list_conversations(self) -> list[dict]:
        with self.db.read() as c:
            return [dict(r) | {"scope": json.loads(r["scope_json"])} for r in c.execute(
                "SELECT id,title,scope_json,created_at,updated_at FROM ai_conversations ORDER BY updated_at DESC LIMIT 100")]

    def get_conversation(self, conv_id: str) -> dict:
        with self.db.read() as c:
            conv = c.execute("SELECT * FROM ai_conversations WHERE id=?", (conv_id,)).fetchone()
            if not conv:
                raise NotFound("Conversation not found")
            msgs = [dict(r) for r in c.execute(
                "SELECT id,role,content,citations_json,model,created_at FROM ai_messages WHERE conversation_id=? ORDER BY created_at, id",
                (conv_id,))]
        for m in msgs:
            extra = json.loads(m.pop("citations_json")) if m["citations_json"] else {}
            m["citations"], m["warnings"] = extra.get("citations", []), extra.get("warnings", [])
        return {"id": conv["id"], "title": conv["title"], "scope": json.loads(conv["scope_json"]), "messages": msgs}

    def delete_conversation(self, conv_id: str) -> None:
        with self.db.write() as c:
            if not c.execute("DELETE FROM ai_conversations WHERE id=?", (conv_id,)).rowcount:
                raise NotFound("Conversation not found")

    # ── answering ─────────────────────────────────────────────
    @staticmethod
    def build_prompt(chunks: list[dict]) -> str:
        blocks = [f'<source id="S{i}" document="{c["title"]}" page="{c["page_no"]}">\n{c["text"]}\n</source>'
                  for i, c in enumerate(chunks, 1)]
        return load(PROMPT_VERSION).format(sources="\n\n".join(blocks))

    def answer_stream(self, message: str, scope: dict | None = None, conversation_id: str | None = None) -> Iterator[Event]:
        message = (message or "").strip()
        if not message:
            raise AppError("message is required", code="invalid_body")
        doc_ids = self.resolve_scope(scope)
        scope = scope or {"type": "all"}
        conv_id = self._conversation(conversation_id, scope, message)
        history = self._history(conv_id)
        self._save_message(conv_id, "user", message)

        query = message
        if len(message.split()) < SHORT_QUESTION_WORDS:
            prev = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
            query = f"{prev} {message}".strip()
        chunks = self.search.retrieve_chunks(query, document_ids=doc_ids, k=TOP_K)
        sources = [{"n": i, "chunk_id": c["id"], "document_id": c["document_id"], "title": c["title"],
                    "page_no": c["page_no"], "bbox": c["bbox"], "preview": c["text"][:200]} for i, c in enumerate(chunks, 1)]
        yield "meta", {"conversation_id": conv_id}
        yield "sources", {"sources": sources}

        if not chunks:  # nothing retrieved: abstain without asking the model to invent something
            yield from self._finish(conv_id, NOT_FOUND_TEXT, [], [], True, None)
            return
        if self.llm is None:
            yield "error", {"code": "llm_unavailable", "detail": "Connect a local model to get written answers. "
                            "The passages above are the most relevant ones found."}
            return
        messages = [{"role": "system", "content": self.build_prompt(chunks)}, *history,
                    {"role": "user", "content": message}]
        parts: list[str] = []
        try:
            for tok in self.llm.chat(messages, temperature=0.0, stream=True):
                parts.append(tok)
                yield "token", {"t": tok}
        except Exception as e:  # noqa: BLE001
            log.warning("LLM failed during chat: %s", e)
            yield "error", {"code": "llm_error", "detail": f"The model failed to answer ({type(e).__name__}). Try again."}
            return
        checked = check_answer("".join(parts), {i: c["text"] for i, c in enumerate(chunks, 1)})
        citations = []
        for n in checked.used:
            c = chunks[n - 1]
            citations.append({"n": n, "chunk_id": c["id"], "document_id": c["document_id"], "title": c["title"],
                              "page_no": c["page_no"], "bbox": c["bbox"], "quote": best_quote(c["text"], _claim_for(checked.answer, n))})
        yield from self._finish(conv_id, checked.answer, citations, checked.warnings, checked.not_found,
                                getattr(self.llm, "model", None), grounded=checked.grounded)

    def _finish(self, conv_id, answer, citations, warnings, not_found, model, grounded=None) -> Iterator[Event]:
        for c in citations:
            yield "citation", c
        for w in warnings:
            yield "warning", w
        mid = self._save_message(conv_id, "assistant", answer, {"citations": citations, "warnings": warnings}, model)
        yield "done", {"message_id": mid, "conversation_id": conv_id, "answer": answer, "citations": citations,
                       "warnings": warnings, "not_found": not_found,
                       "grounded": bool(grounded) if grounded is not None else not_found}


def _claim_for(answer: str, n: int) -> str:
    """The sentence(s) of the answer that cite source n (used to pick the supporting quote)."""
    hits = [s for s in answer.split(chr(10)) if f"[S{n}]" in s]
    return strip_citations(" ".join(hits)) or answer

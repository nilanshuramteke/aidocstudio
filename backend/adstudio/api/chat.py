"""Ask AI routes. /chat streams Server-Sent Events: meta, sources, token*, citation*, warning*, done | error."""
import json

from fastapi import APIRouter, Body, Request
from fastapi.responses import StreamingResponse

from ..core.errors import AppError

router = APIRouter(prefix="/chat")


def _c(request: Request):
    return request.app.state.c


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("")
def chat(request: Request, body: dict = Body(...)):
    rag = _c(request).rag
    message, scope, conv = body.get("message"), body.get("scope"), body.get("conversation_id")
    if not isinstance(message, str) or not message.strip():
        raise AppError("message is required", code="invalid_body")
    rag.resolve_scope(scope)  # validate before streaming starts so errors are proper HTTP 400s
    stream = rag.answer_stream(message, scope, conv)
    first = next(stream)  # surfaces NotFound(conversation) as an HTTP error too

    def gen():
        try:
            yield _sse(*first)
            for ev, data in stream:
                yield _sse(ev, data)
        finally:
            stream.close()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/conversations")
def conversations(request: Request):
    return {"items": _c(request).rag.list_conversations()}


@router.get("/conversations/{conv_id}")
def conversation(request: Request, conv_id: str):
    return _c(request).rag.get_conversation(conv_id)


@router.delete("/conversations/{conv_id}")
def delete_conversation(request: Request, conv_id: str):
    _c(request).rag.delete_conversation(conv_id)
    return {"id": conv_id, "deleted": True}


@router.post("/compare")
def compare(request: Request, body: dict = Body(...)):
    ids = body.get("document_ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise AppError("document_ids must be a list of ids", code="invalid_body")
    return _c(request).compare.compare(ids, body.get("aspects"))

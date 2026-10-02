"""Job handlers for indexing. Indexing runs right after text acquisition so documents are keyword-searchable early."""
from ..documents.service import DocumentService
from ..jobs.worker import JobContext
from .service import SearchService


def make_index_handler(search: SearchService, docs: DocumentService):
    def handler(ctx: JobContext) -> None:
        search.index_document(ctx.job.document_id)
        search.index_fields(ctx.job.document_id)
        if search.embedding:
            docs.queue.enqueue("stage:embed", document_id=ctx.job.document_id, priority=7)  # slower; after keyword
    return handler


def make_embed_handler(search: SearchService, docs: DocumentService):
    def handler(ctx: JobContext) -> None:
        reset = search.embed_document(ctx.job.document_id)
        if reset:  # embedding model/dimension changed: every other document must be re-embedded
            for doc_id in search.doc_ids_with_chunks():
                if doc_id != ctx.job.document_id:
                    docs.queue.enqueue("stage:embed", document_id=doc_id, priority=8)
    return handler


def make_fields_handler(search: SearchService):
    def handler(ctx: JobContext) -> None:
        search.index_fields(ctx.job.document_id)
    return handler

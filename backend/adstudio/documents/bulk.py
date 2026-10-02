"""Bulk actions over many documents. One place so the API, workflows and the UI share the same semantics."""
from ..core.errors import AppError, NotFound
from ..organization.service import OrganizationService
from .review import ReviewService
from .service import DocumentService

ACTIONS = ("accept_all_confident", "delete", "reprocess", "tag", "untag", "add_to_collection", "remove_from_collection",
           "export")


class BulkService:
    def __init__(self, docs: DocumentService, review: ReviewService, org: OrganizationService, exporter=None):
        self.docs, self.review, self.org, self.exporter = docs, review, org, exporter

    def run(self, ids: list[str], action: str, params: dict | None = None) -> dict:
        params = params or {}
        if action not in ACTIONS:
            raise AppError(f"action must be one of: {', '.join(ACTIONS)}", code="invalid_action")
        if action in ("tag", "untag"):
            tags = params.get("tags")
            if not isinstance(tags, list) or not tags or not all(isinstance(t, str) for t in tags):
                raise AppError("`params.tags` must be a non-empty list of names", code="invalid_body")
            self.org.tag_documents(ids, add=tags if action == "tag" else [], remove=tags if action == "untag" else [])
            return {"results": [{"id": i, "ok": True} for i in ids]}
        if action in ("add_to_collection", "remove_from_collection"):
            cid = params.get("collection_id")
            if not isinstance(cid, str):
                raise AppError("`params.collection_id` is required", code="invalid_body")
            n = (self.org.add_to_collection if action == "add_to_collection" else self.org.remove_from_collection)(cid, ids)
            return {"results": [{"id": i, "ok": True} for i in ids], "changed": n}
        if action == "export":
            if self.exporter is None:
                raise AppError("Export is not available", code="unavailable", status=503)
            return {"file": self.exporter.export(ids, params.get("format", "csv"), params.get("fields"))}
        return {"results": self.review.bulk(ids, action)}

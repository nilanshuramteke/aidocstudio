"""Tags + collections. A smart collection is a saved search evaluated on demand (never a copy of ids)."""
import json

from ..core.errors import AppError, NotFound
from ..core.ids import new_id
from ..core.timeutil import now_iso
from ..search.service import SearchService
from ..storage.db import Database
from ..storage.errors import IntegrityError

SMART_LIMIT = 5000


class OrganizationService:
    def __init__(self, db: Database, search: SearchService):
        self.db, self.search = db, search

    # ── tags ──────────────────────────────────────────────────
    def list_tags(self) -> list[dict]:
        with self.db.read() as c:
            return [dict(r) for r in c.execute(
                "SELECT t.id, t.name, t.color, COUNT(d.id) AS documents FROM tags t"
                " LEFT JOIN document_tags dt ON dt.tag_id=t.id LEFT JOIN documents d ON d.id=dt.document_id AND d.deleted_at IS NULL"
                " GROUP BY t.id ORDER BY t.name")]

    def _tag_id(self, name: str, *, create: bool) -> str | None:
        name = name.strip()
        if not name or len(name) > 60:
            raise AppError("Tag names must be 1-60 characters", code="invalid_name")
        with self.db.read() as c:
            r = c.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()
        if r:
            return r["id"]
        if not create:
            return None
        return self.create_tag(name)["id"]

    def create_tag(self, name: str, color: str | None = None) -> dict:
        name = name.strip()
        if not name or len(name) > 60:
            raise AppError("Tag names must be 1-60 characters", code="invalid_name")
        tid = new_id()
        try:
            with self.db.write() as c:
                c.execute("INSERT INTO tags(id,name,color) VALUES(?,?,?)", (tid, name, color))
        except IntegrityError as e:
            raise AppError(f"Tag '{name}' already exists", code="duplicate", status=409) from e
        return {"id": tid, "name": name, "color": color}

    def update_tag(self, tag_id: str, changes: dict) -> dict:
        with self.db.read() as c:
            t = c.execute("SELECT * FROM tags WHERE id=?", (tag_id,)).fetchone()
        if not t:
            raise NotFound("Tag not found")
        name, color = changes.get("name", t["name"]).strip(), changes.get("color", t["color"])
        try:
            with self.db.write() as c:
                c.execute("UPDATE tags SET name=?, color=? WHERE id=?", (name, color, tag_id))
        except IntegrityError as e:
            raise AppError(f"Tag '{name}' already exists", code="duplicate", status=409) from e
        return {"id": tag_id, "name": name, "color": color}

    def delete_tag(self, tag_id: str) -> None:
        with self.db.write() as c:
            if not c.execute("DELETE FROM tags WHERE id=?", (tag_id,)).rowcount:
                raise NotFound("Tag not found")

    def tags_of(self, doc_id: str) -> list[str]:
        with self.db.read() as c:
            return [r["name"] for r in c.execute(
                "SELECT t.name FROM document_tags dt JOIN tags t ON t.id=dt.tag_id WHERE dt.document_id=? ORDER BY t.name", (doc_id,))]

    def tag_documents(self, doc_ids: list[str], add: list[str] = (), remove: list[str] = ()) -> None:
        add_ids = [self._tag_id(n, create=True) for n in add]
        rem_ids = [i for i in (self._tag_id(n, create=False) for n in remove) if i]
        with self.db.write() as c:
            for d in doc_ids:
                if not c.execute("SELECT 1 FROM documents WHERE id=? AND deleted_at IS NULL", (d,)).fetchone():
                    continue
                for t in add_ids:
                    c.execute("INSERT OR IGNORE INTO document_tags(document_id,tag_id) VALUES(?,?)", (d, t))
                for t in rem_ids:
                    c.execute("DELETE FROM document_tags WHERE document_id=? AND tag_id=?", (d, t))

    # ── collections ───────────────────────────────────────────
    def create_collection(self, name: str, kind: str = "manual", query: dict | None = None) -> dict:
        if kind not in ("manual", "smart"):
            raise AppError("kind must be manual or smart", code="invalid_kind")
        if not name.strip():
            raise AppError("Name is required", code="invalid_name")
        if kind == "smart" and not isinstance(query, dict):
            raise AppError("A smart collection needs a `query` ({q, filters})", code="invalid_query")
        cid = new_id()
        try:
            with self.db.write() as c:
                c.execute("INSERT INTO collections(id,name,kind,query_json,created_at) VALUES(?,?,?,?,?)",
                          (cid, name.strip(), kind, json.dumps(query) if kind == "smart" else None, now_iso()))
        except IntegrityError as e:
            raise AppError(f"A collection named '{name}' already exists", code="duplicate", status=409) from e
        return self.get_collection(cid)

    def get_collection(self, cid: str) -> dict:
        with self.db.read() as c:
            r = c.execute("SELECT * FROM collections WHERE id=?", (cid,)).fetchone()
        if not r:
            raise NotFound("Collection not found")
        return {"id": r["id"], "name": r["name"], "kind": r["kind"],
                "query": json.loads(r["query_json"]) if r["query_json"] else None, "documents": len(self.document_ids(cid))}

    def list_collections(self) -> list[dict]:
        with self.db.read() as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM collections ORDER BY name")]
        return [self.get_collection(i) for i in ids]

    def delete_collection(self, cid: str) -> None:
        with self.db.write() as c:
            if not c.execute("DELETE FROM collections WHERE id=?", (cid,)).rowcount:
                raise NotFound("Collection not found")

    def document_ids(self, cid: str) -> list[str]:
        with self.db.read() as c:
            r = c.execute("SELECT kind, query_json FROM collections WHERE id=?", (cid,)).fetchone()
            if not r:
                raise NotFound("Collection not found")
            if r["kind"] == "manual":
                return [x[0] for x in c.execute(
                    "SELECT cd.document_id FROM collection_documents cd JOIN documents d ON d.id=cd.document_id"
                    " WHERE cd.collection_id=? AND d.deleted_at IS NULL ORDER BY cd.document_id DESC", (cid,))]
        q = json.loads(r["query_json"])
        res = self.search.search(q.get("q", ""), q.get("filters"), mode=q.get("mode", "best"), limit=SMART_LIMIT,
                                 parse=bool(q.get("parse", True)))
        return [x["document"]["id"] for x in res["results"]]

    def add_to_collection(self, cid: str, doc_ids: list[str]) -> int:
        with self.db.read() as c:
            r = c.execute("SELECT kind FROM collections WHERE id=?", (cid,)).fetchone()
        if not r:
            raise NotFound("Collection not found")
        if r["kind"] != "manual":
            raise AppError("Smart collections fill themselves from their search", code="smart_collection", status=409)
        n = 0
        with self.db.write() as c:
            for d in doc_ids:
                if c.execute("SELECT 1 FROM documents WHERE id=? AND deleted_at IS NULL", (d,)).fetchone():
                    n += c.execute("INSERT OR IGNORE INTO collection_documents(collection_id,document_id) VALUES(?,?)", (cid, d)).rowcount
        return n

    def remove_from_collection(self, cid: str, doc_ids: list[str]) -> int:
        with self.db.write() as c:
            return sum(c.execute("DELETE FROM collection_documents WHERE collection_id=? AND document_id=?", (cid, d)).rowcount
                       for d in doc_ids)

    def collection_by_name(self, name: str) -> dict | None:
        with self.db.read() as c:
            r = c.execute("SELECT id FROM collections WHERE name=?", (name,)).fetchone()
        return self.get_collection(r["id"]) if r else None

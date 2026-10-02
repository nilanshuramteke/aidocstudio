"""Entity resolution + relationships + document links over extracted fields. Deterministic; no LLM."""
from __future__ import annotations  # methods named `list` would otherwise shadow the builtin in annotations

import json
import re
from difflib import SequenceMatcher

from ..core.errors import AppError, NotFound
from ..core.ids import new_id
from ..core.timeutil import now_iso
from ..storage.db import Database
from ..storage.errors import IntegrityError

FUZZY = 0.92
MAX_GRAPH_NODES = 500
_SUFFIXES = {"pvt", "private", "ltd", "limited", "llp", "inc", "incorporated", "llc", "corp", "corporation", "co",
             "company", "gmbh", "plc", "sa", "bv", "pte", "opc"}
_PREFIXES = {"m/s", "ms", "messrs", "the"}
# default field -> (entity kind, role); a field in a type schema can override with {"entity": "organization", "role": "x"}
DEFAULT_ENTITY_FIELDS = {"vendor": ("organization", "vendor"), "customer": ("organization", "customer"),
                         "merchant": ("organization", "merchant"), "parties": ("organization", "party")}
IDENTIFIER_FIELDS = {"vendor": ("gstin", "vendor_gstin")}  # entity field -> (identifier kind, field holding it)


def normalize_name(name: str) -> str:
    s = re.sub(r"[^\w\s/]", " ", name.lower().replace("&", " and "))
    toks = [t for t in s.split() if t]
    while toks and toks[0] in _PREFIXES:
        toks.pop(0)
    while len(toks) > 1 and toks[-1] in _SUFFIXES:
        toks.pop()
    return " ".join(toks)


def split_parties(raw: str) -> list[str]:
    parts = re.split(r"\s+and\s+|\s*;\s*|\s*&\s*|\s*,\s*(?=[A-Z])", raw)
    return [p.strip(" .,") for p in parts if len(p.strip(" .,")) >= 2]


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b or a.replace(" ", "") == b.replace(" ", ""):
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


class KnowledgeService:
    def __init__(self, db: Database, types=None):
        self.db, self.types = db, types

    # ── resolution ────────────────────────────────────────────
    def resolve(self, kind: str, name: str, identifiers: dict[str, str] | None = None) -> str:
        """Find or create the entity for this name (+ strong identifiers). Adds the surface form as an alias."""
        name = re.sub(r"\s+", " ", name).strip()
        norm = normalize_name(name)
        if not norm:
            raise AppError("Empty entity name", code="invalid_name")
        with self.db.write() as c:
            eid = None
            for ik, iv in (identifiers or {}).items():  # strong identifier wins over any name similarity
                r = c.execute("SELECT entity_id FROM entity_identifiers WHERE kind=? AND value=?", (ik, iv)).fetchone()
                if r:
                    eid = r["entity_id"]
                    break
            if eid is None:
                eid = self._match_by_name(c, kind, norm)
            if eid is None:
                eid = new_id()
                c.execute("INSERT INTO entities(id,kind,canonical_name,norm,nk,created_at) VALUES(?,?,?,?,?,?)",
                          (eid, kind, name, norm, norm.replace(" ", ""), now_iso()))
            c.execute("INSERT OR IGNORE INTO entity_aliases(entity_id,alias,norm,nk) VALUES(?,?,?,?)", (eid, name, norm, norm.replace(" ", "")))
            for ik, iv in (identifiers or {}).items():
                c.execute("INSERT OR IGNORE INTO entity_identifiers(entity_id,kind,value) VALUES(?,?,?)", (eid, ik, iv))
        return eid

    @staticmethod
    def _match_by_name(c, kind: str, norm: str) -> str | None:
        nk = norm.replace(" ", "")
        exact = c.execute("SELECT id FROM entities WHERE kind=? AND nk=? LIMIT 1", (kind, nk)).fetchone() or c.execute(
            "SELECT a.entity_id AS id FROM entity_aliases a JOIN entities e ON e.id=a.entity_id WHERE e.kind=? AND a.nk=? LIMIT 1",
            (kind, nk)).fetchone()
        if exact:
            return exact["id"]
        if len(nk) < 4:
            return None  # too short for fuzzy matching to be trustworthy
        like = nk[:3].replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"  # blocking key: first 3 chars
        rows = c.execute("SELECT id, norm FROM entities WHERE kind=? AND nk LIKE ? ESCAPE '!' LIMIT 2000", (kind, like)).fetchall()
        rows += c.execute("SELECT a.entity_id AS id, a.norm AS norm FROM entity_aliases a JOIN entities e ON e.id=a.entity_id"
                          " WHERE e.kind=? AND a.nk LIKE ? ESCAPE '!' LIMIT 2000", (kind, like)).fetchall()
        best, best_id = 0.0, None
        for r in rows:
            sc = _similar(norm, r["norm"])
            if sc > best:
                best, best_id = sc, r["id"]
        return best_id if best >= FUZZY else None

    # ── linking ───────────────────────────────────────────────
    def link_document(self, doc_id: str) -> dict:
        """Rebuild entities/relationships/links for one document from its verified extracted fields."""
        with self.db.read() as c:
            d = c.execute("SELECT id, doc_type_id, created_at, deleted_at FROM documents WHERE id=?", (doc_id,)).fetchone()
            if not d:
                raise NotFound("Document not found")
            fields = {r["key"]: dict(r) for r in c.execute(
                "SELECT id, key, value, status FROM extracted_fields WHERE document_id=? AND value IS NOT NULL"
                " AND status IN ('auto','accepted','corrected')", (doc_id,))}
            tname = c.execute("SELECT name FROM document_types WHERE id=?", (d["doc_type_id"],)).fetchone() if d["doc_type_id"] else None
            schema = {f["key"]: f for f in json.loads(
                c.execute("SELECT fields_json FROM document_types WHERE id=?", (d["doc_type_id"],)).fetchone()["fields_json"])} \
                if d["doc_type_id"] else {}
        type_name = tname["name"] if tname else None
        by_role: dict[str, list[str]] = {}
        links: list[tuple[str, str]] = []  # (entity_id, role)
        field_of: dict[tuple[str, str], str] = {}
        for key, f in fields.items():
            spec = schema.get(key, {})
            kind, role = ((spec["entity"], spec.get("role", key)) if spec.get("entity") else DEFAULT_ENTITY_FIELDS.get(key, (None, None)))
            if not kind:
                continue
            names = split_parties(f["value"]) if role == "party" else [f["value"]]
            idents = {}
            # a human-corrected name overrides the identifier: the user is telling us this is a different party
            if key in IDENTIFIER_FIELDS and IDENTIFIER_FIELDS[key][1] in fields and f["status"] != "corrected":
                idents = {IDENTIFIER_FIELDS[key][0]: fields[IDENTIFIER_FIELDS[key][1]]["value"]}
            for nm in names[:6]:
                eid = self.resolve(kind, nm, idents if len(names) == 1 else None)
                links.append((eid, role))
                by_role.setdefault(role, []).append(eid)
                field_of[(eid, role)] = f["id"]
        now = now_iso()
        with self.db.write() as c:
            c.execute("DELETE FROM document_entities WHERE document_id=?", (doc_id,))
            c.execute("DELETE FROM relationships WHERE evidence_document_id=?", (doc_id,))
            c.execute("DELETE FROM document_links WHERE kind IN ('duplicate','supersedes') AND (a=? OR b=?)", (doc_id, doc_id))
            if d["deleted_at"]:
                return {"entities": 0, "relationships": 0, "links": 0}
            for eid, role in set(links):
                c.execute("INSERT OR IGNORE INTO document_entities(document_id,entity_id,role,field_id) VALUES(?,?,?,?)",
                          (doc_id, eid, role, field_of.get((eid, role))))
            rels = 0
            for v in set(by_role.get("vendor", [])):
                for cu in set(by_role.get("customer", [])):
                    if v != cu:
                        rels += c.execute("INSERT OR IGNORE INTO relationships(id,src_entity_id,dst_entity_id,kind,evidence_document_id,confidence,created_at)"
                                          " VALUES(?,?,?,?,?,?,?)", (new_id(), v, cu, "supplies", doc_id, 0.9, now)).rowcount
            parties = sorted(set(by_role.get("party", [])))
            for i, a in enumerate(parties):
                for b in parties[i + 1:]:
                    rels += c.execute("INSERT OR IGNORE INTO relationships(id,src_entity_id,dst_entity_id,kind,evidence_document_id,confidence,created_at)"
                                      " VALUES(?,?,?,?,?,?,?)", (new_id(), a, b, "party_to", doc_id, 0.8, now)).rowcount
            nlinks = self._detect_links(c, doc_id, d["created_at"], type_name, fields, by_role)
        return {"entities": len(set(links)), "relationships": rels, "links": nlinks}

    @staticmethod
    def _detect_links(c, doc_id: str, created_at: str, type_name: str | None, fields: dict, by_role: dict) -> int:
        n = 0
        if type_name == "Invoice" and fields.get("invoice_number") and by_role.get("vendor"):
            vendor = by_role["vendor"][0]
            for o in c.execute(
                    "SELECT d.id, d.created_at, tot.value AS total FROM documents d"
                    " JOIN document_entities de ON de.document_id=d.id AND de.entity_id=? AND de.role='vendor'"
                    " JOIN extracted_fields inv ON inv.document_id=d.id AND inv.key='invoice_number' AND inv.value=?"
                    " LEFT JOIN extracted_fields tot ON tot.document_id=d.id AND tot.key='total'"
                    " WHERE d.id!=? AND d.deleted_at IS NULL", (vendor, fields["invoice_number"]["value"], doc_id)).fetchall():
                mine = (fields.get("total") or {}).get("value")
                if o["total"] == mine:  # same vendor + number + amount: a duplicate copy
                    a, b = sorted((doc_id, o["id"]))
                    n += c.execute("INSERT OR IGNORE INTO document_links(a,b,kind,score) VALUES(?,?,?,?)", (a, b, "duplicate", 1.0)).rowcount
                else:  # same number, different amount: the newer one is a correction of the older
                    newer, older = (doc_id, o["id"]) if created_at >= o["created_at"] else (o["id"], doc_id)
                    n += c.execute("INSERT OR IGNORE INTO document_links(a,b,kind,score) VALUES(?,?,?,?)", (newer, older, "supersedes", 0.8)).rowcount
        if type_name == "Contract" and by_role.get("party") and fields.get("effective_date"):
            mine_parties = set(by_role["party"])
            cands = c.execute(
                "SELECT d.id, ed.value AS eff FROM documents d JOIN document_entities de ON de.document_id=d.id AND de.role='party'"
                " JOIN extracted_fields ed ON ed.document_id=d.id AND ed.key='effective_date'"
                " WHERE de.entity_id IN ({}) AND d.id!=? AND d.deleted_at IS NULL GROUP BY d.id".format(",".join("?" * len(mine_parties))),
                (*mine_parties, doc_id)).fetchall()
            for o in cands:
                other = {r[0] for r in c.execute("SELECT entity_id FROM document_entities WHERE document_id=? AND role='party'", (o["id"],))}
                if other == mine_parties and o["eff"] != fields["effective_date"]["value"]:
                    newer, older = (doc_id, o["id"]) if fields["effective_date"]["value"] > o["eff"] else (o["id"], doc_id)
                    n += c.execute("INSERT OR IGNORE INTO document_links(a,b,kind,score) VALUES(?,?,?,?)", (newer, older, "supersedes", 0.7)).rowcount
        return n

    # ── queries ───────────────────────────────────────────────
    def search(self, q: str = "", kind: str | None = None, limit: int = 25) -> list[dict]:
        norm = normalize_name(q) if q.strip() else ""
        like = "%" + norm.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        sql = ("SELECT e.id, e.kind, e.canonical_name AS name, COUNT(DISTINCT de.document_id) AS documents FROM entities e"
               " LEFT JOIN document_entities de ON de.entity_id=e.id WHERE (e.norm LIKE ? ESCAPE '\\' OR EXISTS ("
               "SELECT 1 FROM entity_aliases a WHERE a.entity_id=e.id AND a.norm LIKE ? ESCAPE '\\'))")
        params: list = [like, like]
        if kind:
            sql += " AND e.kind=?"
            params.append(kind)
        sql += " GROUP BY e.id ORDER BY documents DESC, e.canonical_name LIMIT ?"
        with self.db.read() as c:
            return [dict(r) for r in c.execute(sql, [*params, max(1, min(limit, 100))])]

    def get(self, eid: str) -> dict:
        with self.db.read() as c:
            e = c.execute("SELECT * FROM entities WHERE id=?", (eid,)).fetchone()
            if not e:
                raise NotFound("Entity not found")
            aliases = [r[0] for r in c.execute("SELECT alias FROM entity_aliases WHERE entity_id=? ORDER BY alias", (eid,))]
            idents = {r["kind"]: r["value"] for r in c.execute("SELECT kind, value FROM entity_identifiers WHERE entity_id=?", (eid,))}
            docs = [dict(r) for r in c.execute(
                "SELECT d.id, d.title, de.role, d.state FROM document_entities de JOIN documents d ON d.id=de.document_id"
                " WHERE de.entity_id=? AND d.deleted_at IS NULL ORDER BY d.created_at DESC LIMIT 200", (eid,))]
            rels = [dict(r) for r in c.execute(
                "SELECT r.kind, CASE WHEN r.src_entity_id=:e THEN 'out' ELSE 'in' END AS direction,"
                " o.id AS other_id, o.canonical_name AS other_name, COUNT(DISTINCT r.evidence_document_id) AS evidence_documents"
                " FROM relationships r JOIN entities o ON o.id = CASE WHEN r.src_entity_id=:e THEN r.dst_entity_id ELSE r.src_entity_id END"
                " WHERE r.src_entity_id=:e OR r.dst_entity_id=:e GROUP BY r.kind, direction, o.id ORDER BY evidence_documents DESC", {"e": eid})]
        return {"id": e["id"], "kind": e["kind"], "name": e["canonical_name"], "aliases": aliases, "identifiers": idents,
                "documents": docs, "relationships": rels}

    def graph(self, eid: str, depth: int = 1) -> dict:
        """Nodes/edges within `depth` hops (1..3), both directions, bounded to MAX_GRAPH_NODES.

        Breadth-first with the heaviest neighbours (most evidence documents) kept first. A plain recursive CTE walks
        EVERY path before LIMIT applies, which took 27 s from a 6,500-relationship hub at 100k entities
        (scripts/bench_graph.py); bounding the frontier per level keeps it to milliseconds.
        """
        depth = max(1, min(depth, 3))
        root = self.get(eid)
        depths: dict[str, int] = {eid: 0}
        frontier, truncated = [eid], False
        with self.db.read() as c:
            for d in range(1, depth + 1):
                if not frontier:
                    break
                weight: dict[str, int] = {}
                for k in range(0, len(frontier), 400):
                    part = frontier[k:k + 400]
                    marks = ",".join("?" * len(part))
                    for col, other in (("src_entity_id", "dst_entity_id"), ("dst_entity_id", "src_entity_id")):
                        for r in c.execute(f"SELECT {other} AS o, COUNT(*) AS w FROM relationships WHERE {col} IN ({marks}) GROUP BY {other}", part):
                            if r["o"] not in depths:
                                weight[r["o"]] = weight.get(r["o"], 0) + r["w"]
                room = MAX_GRAPH_NODES - len(depths)
                ranked = sorted(weight, key=lambda n: (-weight[n], n))
                if len(ranked) > room:
                    truncated = True
                frontier = ranked[:max(0, room)]
                for n in frontier:
                    depths[n] = d
            ids = list(depths)
            marks = ",".join("?" * len(ids))
            names = {r["id"]: dict(r) for r in c.execute(f"SELECT id, canonical_name AS name, kind FROM entities WHERE id IN ({marks})", ids)}
            edges = c.execute(
                "SELECT src_entity_id AS src, dst_entity_id AS dst, kind, COUNT(DISTINCT evidence_document_id) AS evidence FROM relationships"
                f" WHERE src_entity_id IN ({marks}) AND dst_entity_id IN ({marks}) GROUP BY src, dst, kind", ids + ids).fetchall()
        nodes = sorted(({**names[n], "depth": d} for n, d in depths.items() if n in names), key=lambda n: (n["depth"], n["name"]))
        return {"root": eid, "root_name": root["name"], "depth": depth, "truncated": truncated,
                "nodes": nodes, "edges": [dict(e) for e in edges]}

    def related_documents(self, doc_id: str) -> dict:
        with self.db.read() as c:
            mine = [dict(r) for r in c.execute(
                "SELECT e.id, e.canonical_name AS name, e.kind, de.role FROM document_entities de JOIN entities e ON e.id=de.entity_id"
                " WHERE de.document_id=?", (doc_id,))]
            shared = [dict(r) for r in c.execute(
                "SELECT d.id, d.title, d.state, GROUP_CONCAT(DISTINCT e.canonical_name) AS via FROM document_entities me"
                " JOIN document_entities o ON o.entity_id=me.entity_id AND o.document_id!=me.document_id"
                " JOIN documents d ON d.id=o.document_id AND d.deleted_at IS NULL JOIN entities e ON e.id=me.entity_id"
                " WHERE me.document_id=? GROUP BY d.id ORDER BY COUNT(DISTINCT me.entity_id) DESC, d.created_at DESC LIMIT 50", (doc_id,))]
            links = [dict(r) for r in c.execute(
                "SELECT l.kind, l.score, CASE WHEN l.a=:d THEN l.b ELSE l.a END AS other_id,"
                " CASE WHEN l.a=:d THEN 'out' ELSE 'in' END AS direction, d.title AS other_title FROM document_links l"
                " JOIN documents d ON d.id = CASE WHEN l.a=:d THEN l.b ELSE l.a END AND d.deleted_at IS NULL"
                " WHERE l.a=:d OR l.b=:d", {"d": doc_id})]
        return {"entities": mine, "related": shared, "links": links}

    # ── manual corrections ────────────────────────────────────
    def rename(self, eid: str, name: str) -> dict:
        self.get(eid)
        if not name.strip():
            raise AppError("Name is required", code="invalid_name")
        try:
            with self.db.write() as c:
                nm = normalize_name(name)
                c.execute("UPDATE entities SET canonical_name=?, norm=?, nk=? WHERE id=?", (name.strip(), nm, nm.replace(" ", ""), eid))
                c.execute("INSERT OR IGNORE INTO entity_aliases(entity_id,alias,norm,nk) VALUES(?,?,?,?)", (eid, name.strip(), nm, nm.replace(" ", "")))
        except IntegrityError as e:
            raise AppError("Another entity already has that name; merge them instead", code="duplicate", status=409) from e
        return self.get(eid)

    def merge(self, src: str, into: str) -> dict:
        """Fold entity `src` into `into` (wrong automatic resolution, or the same company under two names)."""
        if src == into:
            raise AppError("Cannot merge an entity into itself", code="invalid_body")
        s, t = self.get(src), self.get(into)
        if s["kind"] != t["kind"]:
            raise AppError("Only entities of the same kind can be merged", code="kind_mismatch", status=409)
        with self.db.write() as c:
            c.execute("INSERT OR IGNORE INTO entity_aliases(entity_id,alias,norm,nk) SELECT ?, alias, norm, nk FROM entity_aliases WHERE entity_id=?", (into, src))
            c.execute("INSERT OR IGNORE INTO entity_identifiers(entity_id,kind,value) SELECT ?, kind, value FROM entity_identifiers WHERE entity_id=?", (into, src))
            c.execute("INSERT OR IGNORE INTO entity_aliases(entity_id,alias,norm,nk) SELECT ?, canonical_name, norm, nk FROM entities WHERE id=?", (into, src))
            c.execute("UPDATE OR IGNORE document_entities SET entity_id=? WHERE entity_id=?", (into, src))
            c.execute("UPDATE OR IGNORE relationships SET src_entity_id=? WHERE src_entity_id=?", (into, src))
            c.execute("UPDATE OR IGNORE relationships SET dst_entity_id=? WHERE dst_entity_id=?", (into, src))
            c.execute("DELETE FROM relationships WHERE src_entity_id=dst_entity_id")  # a merge can create self-loops
            c.execute("DELETE FROM entities WHERE id=?", (src,))  # cascades the leftovers (duplicate rows ignored above)
        return self.get(into)

    def prune_orphans(self) -> int:
        """Delete entities that no document or relationship references (maintenance; not run per document)."""
        with self.db.write() as c:
            return c.execute(
                "DELETE FROM entities WHERE id NOT IN (SELECT entity_id FROM document_entities)"
                " AND id NOT IN (SELECT src_entity_id FROM relationships) AND id NOT IN (SELECT dst_entity_id FROM relationships)").rowcount

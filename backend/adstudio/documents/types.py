"""Document types (extraction templates): seed data + CRUD. fields_json is the template."""
import json
import re

from ..core.errors import AppError, NotFound
from ..core.interfaces import DocType
from ..core.timeutil import now_iso
from ..storage.db import Database

_DATE = (r"(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}|\d{1,2}\s*[A-Za-z]{3,9}\.?,?\s*\d{4}"
         r"|[A-Za-z]{3,9}\.?\s*\d{1,2},?\s*\d{4}|\d{4}-\d{2}-\d{2})")
_AMT = r"(?:rs\.?|inr|₹|\$)?\s*([\d,]+(?:\.\d{1,2})?)"
_GSTIN = r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]"
VALID_KINDS = {"text", "date", "amount", "id"}

SEED: list[dict] = [
    {"id": "builtin:invoice", "name": "Invoice",
     "description": "A bill issued by a vendor requesting payment for goods or services.",
     "keywords": [["tax invoice", 3], ["invoice", 3], ["invoice no", 2], ["gstin", 2], ["bill to", 1],
                  ["amount due", 1], ["due date", 1], ["subtotal", 1], ["amount payable", 1]],
     "fields": [
         {"key": "invoice_number", "label": "Invoice number", "kind": "id", "required": True,
          "patterns": [r"invoice\s*(?:no|number|num|#)\.?\s*[:\-#]?\s*([A-Z0-9][A-Z0-9\-/]{2,})",
                       r"\binv[\-\s]?(?:no)?\.?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/]{2,})"]},
         {"key": "invoice_date", "label": "Invoice date", "kind": "date", "required": True,
          "patterns": [r"(?<!due )(?<!due)(?:invoice\s*)?dated?\s*[:\-]?\s*" + _DATE]},
         {"key": "due_date", "label": "Due date", "kind": "date", "patterns": [r"due\s*date\s*[:\-]?\s*" + _DATE]},
         {"key": "vendor", "label": "Vendor", "kind": "text", "required": True,
          "patterns": [r"(?:vendor|supplier|seller)\s*[:\-]?\s*([^\n]{3,80})",
                       r"^\s*([A-Z][\w&.,' ]{2,60}(?:pvt\.?\s*ltd\.?|private\s*limited|limited|llp|inc\.?|llc|corp\.?))"]},
         {"key": "vendor_gstin", "label": "Vendor GSTIN", "kind": "id", "validators": ["gstin"],
          "patterns": [r"gstin\s*(?:no\.?)?\s*[:\-]?\s*(" + _GSTIN + ")", r"\b(" + _GSTIN + r")\b"]},
         {"key": "customer", "label": "Customer", "kind": "text",
          "patterns": [r"(?:bill(?:ed)?\s*to|customer|buyer)\s*[:\-]?\s*([^\n]{3,80})"]},
         {"key": "subtotal", "label": "Subtotal", "kind": "amount",
          "patterns": [r"sub\s*-?\s*total\s*[:\-]?\s*" + _AMT]},
         {"key": "tax", "label": "Tax", "kind": "amount",
          "patterns": [r"(?<![a-z])(?:gst|igst|cgst|sgst|vat|tax)\s*(?:\(?\d+(?:\.\d+)?%\)?)?\s*[:\-]?\s*" + _AMT]},
         {"key": "total", "label": "Total", "kind": "amount", "required": True, "pick": "last",
          "patterns": [r"(?<![a-z])(?:grand\s*total|total\s*amount\s*(?:payable|due)?|amount\s*(?:payable|due)"
                       r"|total\s*payable|total)\s*[:\-]?\s*" + _AMT]},
     ]},
    {"id": "builtin:receipt", "name": "Receipt",
     "description": "Proof of a completed payment at a shop or service provider.",
     "keywords": [["receipt", 3], ["thank you", 1], ["cash", 1], ["paid", 1], ["change", 1], ["transaction", 1],
                  ["payment received", 2]],
     "fields": [
         {"key": "merchant", "label": "Merchant", "kind": "text", "required": True, "patterns": [r"\A\s*([^\n]{3,60})"]},
         {"key": "date", "label": "Date", "kind": "date", "required": True,
          "patterns": [r"(?:date|dated)\s*[:\-]?\s*" + _DATE, r"\b" + _DATE]},
         {"key": "total", "label": "Total", "kind": "amount", "required": True, "pick": "last",
          "patterns": [r"(?<![a-z])(?:grand\s*total|total\s*paid|amount\s*paid|total)\s*[:\-]?\s*" + _AMT]},
         {"key": "payment_method", "label": "Payment method", "kind": "text",
          "patterns": [r"\b(cash|credit\s*card|debit\s*card|upi|net\s*banking|card)\b"]},
     ]},
    {"id": "builtin:contract", "name": "Contract",
     "description": "A legal agreement between two or more parties.",
     "keywords": [["this agreement", 3], ["agreement", 3], ["parties", 2], ["termination", 2], ["governing law", 2],
                  ["hereinafter", 2], ["whereas", 2], ["witness", 1], ["terms and conditions", 1]],
     "fields": [
         {"key": "effective_date", "label": "Effective date", "kind": "date",
          "patterns": [r"(?:effective\s*date|effective\s*as\s*of|made\s*(?:on|as\s*of)|dated)\s*[:\-]?\s*" + _DATE]},
         {"key": "parties", "label": "Parties", "kind": "text"},
         {"key": "term", "label": "Term / duration", "kind": "text"},
         {"key": "payment_terms", "label": "Payment terms", "kind": "text"},
         {"key": "governing_law", "label": "Governing law", "kind": "text",
          "patterns": [r"governed\s*by\s*(?:the\s*)?laws?\s*of\s*([A-Za-z ,]{3,60})"]},
     ]},
    {"id": "builtin:other", "name": "Other", "description": "Anything that is not one of the other types.",
     "keywords": [], "fields": []},
]
BUILTIN_RULES = [("builtin:gstin", "GSTIN valid"), ("builtin:pan", "PAN valid"), ("builtin:date", "Date valid"),
                 ("builtin:amount", "Amount valid"), ("builtin:total_matches", "Subtotal + tax = total"),
                 ("builtin:due_after_invoice", "Due date on/after invoice date")]


def ensure_seed(db: Database) -> None:
    """Idempotent: inserts missing built-ins, never overwrites user edits."""
    with db.write() as c:
        for t in SEED:
            c.execute("INSERT OR IGNORE INTO document_types(id,name,description,keywords_json,fields_json,builtin,created_at)"
                      " VALUES(?,?,?,?,?,1,?)",
                      (t["id"], t["name"], t["description"], json.dumps(t["keywords"]), json.dumps(t["fields"]), now_iso()))
        for rid, name in BUILTIN_RULES:
            c.execute("INSERT OR IGNORE INTO validation_rules(id,doc_type_id,name,kind,config_json) VALUES(?,?,?,?,?)",
                      (rid, None, name, "builtin", "{}"))


def _row_to_type(r) -> DocType:
    return DocType(r["id"], r["name"], r["description"] or "", json.loads(r["keywords_json"] or "[]"),
                   json.loads(r["fields_json"]))


def validate_fields_schema(fields) -> list[dict]:
    if not isinstance(fields, list):
        raise AppError("`fields` must be a list", code="invalid_fields")
    seen = set()
    for f in fields:
        if not isinstance(f, dict) or not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", str(f.get("key", ""))):
            raise AppError("Each field needs a snake_case `key`", code="invalid_fields")
        if f["key"] in seen:
            raise AppError(f"Duplicate field key '{f['key']}'", code="invalid_fields")
        seen.add(f["key"])
        if f.get("kind", "text") not in VALID_KINDS:
            raise AppError(f"Field '{f['key']}' has unknown kind", code="invalid_fields")
        for p in f.get("patterns", []):
            try:
                re.compile(p)
            except re.error as e:
                raise AppError(f"Field '{f['key']}' has an invalid pattern: {e}", code="invalid_fields") from e
    return fields


class DocumentTypes:
    def __init__(self, db: Database):
        self.db = db

    def list(self) -> list[DocType]:
        with self.db.read() as c:
            return [_row_to_type(r) for r in c.execute("SELECT * FROM document_types ORDER BY builtin DESC, name")]

    def get(self, type_id: str) -> DocType:
        with self.db.read() as c:
            r = c.execute("SELECT * FROM document_types WHERE id=?", (type_id,)).fetchone()
        if not r:
            raise NotFound(f"Document type {type_id} not found")
        return _row_to_type(r)

    def by_name(self, name: str) -> DocType | None:
        with self.db.read() as c:
            r = c.execute("SELECT * FROM document_types WHERE name=?", (name,)).fetchone()
        return _row_to_type(r) if r else None

    def create(self, name: str, description: str, keywords: list, fields: list) -> DocType:
        from ..core.ids import new_id
        if not name.strip():
            raise AppError("Name is required", code="invalid_name")
        validate_fields_schema(fields)
        tid = new_id()
        try:
            with self.db.write() as c:
                c.execute("INSERT INTO document_types(id,name,description,keywords_json,fields_json,builtin,created_at)"
                          " VALUES(?,?,?,?,?,0,?)", (tid, name.strip(), description, json.dumps(keywords),
                                                     json.dumps(fields), now_iso()))
        except Exception as e:  # sqlite3.IntegrityError on duplicate name
            if "UNIQUE" in str(e):
                raise AppError(f"A document type named '{name}' already exists", code="duplicate", status=409) from e
            raise
        return self.get(tid)

    def update(self, type_id: str, changes: dict) -> DocType:
        cur = self.get(type_id)
        name = changes.get("name", cur.name)
        if cur.name != name and cur.id.startswith("builtin:"):
            raise AppError("Built-in types cannot be renamed", code="builtin_locked")
        fields = validate_fields_schema(changes["fields"]) if "fields" in changes else cur.fields
        with self.db.write() as c:
            c.execute("UPDATE document_types SET name=?, description=?, keywords_json=?, fields_json=? WHERE id=?",
                      (name, changes.get("description", cur.description),
                       json.dumps(changes.get("keywords", cur.keywords)), json.dumps(fields), type_id))
        return self.get(type_id)

    def delete(self, type_id: str) -> None:
        cur = self.get(type_id)
        if cur.id.startswith("builtin:"):
            raise AppError("Built-in types cannot be deleted", code="builtin_locked", status=409)
        with self.db.write() as c:
            c.execute("UPDATE documents SET doc_type_id=NULL, doc_type_conf=NULL WHERE doc_type_id=?", (type_id,))
            c.execute("DELETE FROM document_types WHERE id=?", (type_id,))

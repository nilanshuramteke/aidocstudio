"""Workflow rules: trigger -> conditions -> actions. Rules are data; actions are registered code. LLM output never
reaches an action directly (actions are chosen by the rule, parameters come from the rule + document fields)."""
from __future__ import annotations  # methods named `list` shadow the builtin inside the class body

import json
import logging
import re
import shutil
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ..core.errors import AppError, NotFound
from ..core.events import EventBus
from ..core.ids import new_id
from ..core.timeutil import now_iso
from ..export.service import ExportService
from ..organization.service import OrganizationService
from ..storage.db import Database
from ..storage.errors import IntegrityError
from ..storage.settings import SettingsStore

log = logging.getLogger("adstudio.workflows")
EVENTS = ("document.imported", "document.classified", "document.extracted", "document.ready",
          "document.needs_review", "document.review_resolved")
OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "contains", "matches", "in", "exists")
ACTION_TYPES = ("apply_tag", "remove_tag", "add_to_collection", "export_copy", "run_export", "webhook", "notify")


# ── definition validation ─────────────────────────────────────
def _check_condition(node, depth=0) -> None:
    if depth > 6 or not isinstance(node, dict):
        raise AppError("Conditions must be nested objects (max depth 6)", code="invalid_workflow")
    if "all" in node or "any" in node:
        kind = "all" if "all" in node else "any"
        if not isinstance(node[kind], list):
            raise AppError(f"`{kind}` must be a list", code="invalid_workflow")
        for n in node[kind]:
            _check_condition(n, depth + 1)
    elif "not" in node:
        _check_condition(node["not"], depth + 1)
    else:
        if not isinstance(node.get("field"), str) or node.get("op") not in OPS:
            raise AppError(f"Each condition needs a `field` and an `op` in {', '.join(OPS)}", code="invalid_workflow")
        if node["op"] == "matches":
            try:
                re.compile(str(node.get("value", "")))
            except re.error as e:
                raise AppError(f"Invalid regex: {e}", code="invalid_workflow") from e


def validate_definition(d) -> dict:
    if not isinstance(d, dict):
        raise AppError("Workflow definition must be an object", code="invalid_workflow")
    ev = (d.get("trigger") or {}).get("event")
    if ev not in EVENTS:
        raise AppError(f"trigger.event must be one of: {', '.join(EVENTS)}", code="invalid_workflow")
    _check_condition(d.get("conditions") or {"all": []})
    actions = d.get("actions")
    if not isinstance(actions, list) or not actions:
        raise AppError("At least one action is required", code="invalid_workflow")
    for a in actions:
        if not isinstance(a, dict) or a.get("type") not in ACTION_TYPES:
            raise AppError(f"Unknown action type; allowed: {', '.join(ACTION_TYPES)}", code="invalid_workflow")
        need = {"apply_tag": "tag", "remove_tag": "tag", "add_to_collection": "collection", "export_copy": "target",
                "webhook": "url", "notify": "message"}.get(a["type"])
        if need and not isinstance(a.get(need), str):
            raise AppError(f"Action {a['type']} needs `{need}`", code="invalid_workflow")
        if a["type"] == "webhook" and not re.match(r"^https?://", a["url"]):
            raise AppError("Webhook URL must start with http:// or https://", code="invalid_workflow")
    return d


# ── conditions ────────────────────────────────────────────────
@dataclass
class DocContext:
    doc: dict
    fields: dict[str, str | None]
    tags: list[str]
    type_name: str | None

    def value(self, name: str):
        if name.startswith("field:"):
            return self.fields.get(name[6:])
        if name == "doc_type":
            return self.type_name
        if name == "tag":
            return self.tags
        return self.doc.get(name)


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def eval_leaf(c: dict, ctx: DocContext) -> bool:
    actual, op, want = ctx.value(c["field"]), c["op"], c.get("value")
    if op == "exists":
        return actual not in (None, "", [])
    if actual is None:
        return op == "ne"
    if isinstance(actual, list):  # tags
        return {"contains": want in actual, "in": any(a in (want or []) for a in actual), "eq": want in actual,
                "ne": want not in actual}.get(op, False)
    if op in ("gt", "gte", "lt", "lte"):
        a, w = _num(actual), _num(want)
        if a is None or w is None:  # dates compare lexicographically (ISO)
            a, w = str(actual), str(want)
        return {"gt": a > w, "gte": a >= w, "lt": a < w, "lte": a <= w}[op]
    a, w = str(actual).lower(), (str(want).lower() if not isinstance(want, list) else [str(x).lower() for x in want])
    if op == "eq":
        return a == w
    if op == "ne":
        return a != w
    if op == "contains":
        return str(w) in a
    if op == "in":
        return a in (w if isinstance(w, list) else [w])
    if op == "matches":
        return re.search(str(want), str(actual), re.IGNORECASE) is not None
    return False


def eval_conditions(node: dict, ctx: DocContext, trace: list | None = None) -> bool:
    trace = trace if trace is not None else []
    if not node:
        return True
    if "all" in node:
        return all(eval_conditions(n, ctx, trace) for n in node["all"])
    if "any" in node:
        return any(eval_conditions(n, ctx, trace) for n in node["any"])
    if "not" in node:
        return not eval_conditions(node["not"], ctx, trace)
    ok = eval_leaf(node, ctx)
    trace.append({"field": node["field"], "op": node["op"], "value": node.get("value"),
                  "actual": ctx.value(node["field"]), "passed": ok})
    return ok


# ── templates ─────────────────────────────────────────────────
_PLACEHOLDER = re.compile(r"\{(\w+)(?::([^}]*))?\}")
_BAD = re.compile(r'[<>:"|?*\\/\x00-\x1f]')  # includes both slashes: a value can never add path segments


def render_template(tpl: str, ctx: DocContext) -> str:
    """{vendor}/{invoice_date:%Y-%m}/{original_name}. Each value becomes ONE sanitized path segment."""
    values = {**{k: v for k, v in ctx.fields.items()}, "original_name": ctx.doc["original_name"], "title": ctx.doc["title"],
              "doc_type": ctx.type_name, "id": ctx.doc["id"], "state": ctx.doc["state"]}

    def sub(m: re.Match) -> str:
        v = values.get(m.group(1))
        if v in (None, ""):
            return "unknown"
        if m.group(2):
            try:
                return date.fromisoformat(str(v)).strftime(m.group(2))
            except ValueError:
                pass
        return str(v)

    parts = []
    for seg in re.split(r"[\\/]+", tpl):
        rendered = _BAD.sub("_", _PLACEHOLDER.sub(sub, seg)).strip().strip(".")
        if rendered and rendered not in (".", ".."):
            parts.append(rendered[:120])
    return "/".join(parts)


# ── service ───────────────────────────────────────────────────
@dataclass
class ActionResult:
    type: str
    ok: bool
    message: str
    detail: dict = field(default_factory=dict)


class WorkflowService:
    def __init__(self, db: Database, org: OrganizationService, exporter: ExportService, settings: SettingsStore,
                 bus: EventBus, docs, types):
        self.db, self.org, self.exporter, self.settings, self.bus, self.docs, self.types = db, org, exporter, settings, bus, docs, types

    # CRUD
    def create(self, name: str, definition: dict, enabled: bool = True) -> dict:
        if not name.strip():
            raise AppError("Name is required", code="invalid_name")
        validate_definition(definition)
        wid, now = new_id(), now_iso()
        with self.db.write() as c:
            c.execute("INSERT INTO workflows(id,name,enabled,definition_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                      (wid, name.strip(), int(enabled), json.dumps(definition), now, now))
        return self.get(wid)

    def get(self, wid: str) -> dict:
        with self.db.read() as c:
            r = c.execute("SELECT * FROM workflows WHERE id=?", (wid,)).fetchone()
        if not r:
            raise NotFound("Workflow not found")
        return {"id": r["id"], "name": r["name"], "enabled": bool(r["enabled"]), "definition": json.loads(r["definition_json"]),
                "created_at": r["created_at"], "updated_at": r["updated_at"]}

    def list(self) -> list[dict]:
        with self.db.read() as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM workflows ORDER BY created_at")]
        out = []
        for i in ids:
            w = self.get(i)
            with self.db.read() as c:
                last = c.execute("SELECT status, finished_at FROM workflow_runs WHERE workflow_id=? ORDER BY started_at DESC LIMIT 1", (i,)).fetchone()
            w["last_run"] = dict(last) if last else None
            out.append(w)
        return out

    def update(self, wid: str, changes: dict) -> dict:
        cur = self.get(wid)
        definition = validate_definition(changes["definition"]) if "definition" in changes else cur["definition"]
        with self.db.write() as c:
            c.execute("UPDATE workflows SET name=?, enabled=?, definition_json=?, updated_at=? WHERE id=?",
                      (changes.get("name", cur["name"]), int(changes.get("enabled", cur["enabled"])), json.dumps(definition), now_iso(), wid))
        return self.get(wid)

    def delete(self, wid: str) -> None:
        with self.db.write() as c:
            if not c.execute("DELETE FROM workflows WHERE id=?", (wid,)).rowcount:
                raise NotFound("Workflow not found")

    def runs(self, wid: str, limit: int = 50) -> list[dict]:
        self.get(wid)
        with self.db.read() as c:
            return [dict(r) | {"log": json.loads(r["log_json"]) if r["log_json"] else None} for r in c.execute(
                "SELECT id, document_id, event, status, log_json, started_at, finished_at FROM workflow_runs"
                " WHERE workflow_id=? ORDER BY started_at DESC LIMIT ?", (wid, limit))]

    def listens_to(self, event: str) -> bool:
        with self.db.read() as c:
            for r in c.execute("SELECT definition_json FROM workflows WHERE enabled=1"):
                if json.loads(r["definition_json"]).get("trigger", {}).get("event") == event:
                    return True
        return False

    # context
    def context(self, doc_id: str) -> DocContext:
        d = self.docs.get(doc_id)
        with self.db.read() as c:
            fields = {r["key"]: r["value"] for r in c.execute(
                "SELECT key, value FROM extracted_fields WHERE document_id=? AND status!='rejected'", (doc_id,))}
            t = c.execute("SELECT name FROM document_types WHERE id=?", (d["doc_type_id"],)).fetchone() if d["doc_type_id"] else None
        return DocContext(d, fields, self.org.tags_of(doc_id), t["name"] if t else None)

    # execution
    def dispatch(self, event: str, doc_id: str) -> list[dict]:
        out = []
        for w in self.list():
            if not w["enabled"] or w["definition"]["trigger"]["event"] != event:
                continue
            out.append(self.run(w["id"], doc_id, event=event))
        return out

    def run(self, wid: str, doc_id: str, *, event: str | None = None, dry_run: bool = False) -> dict:
        w = self.get(wid)
        d = w["definition"]
        event = event or d["trigger"]["event"]
        ctx = self.context(doc_id)
        trace: list = []
        matched = eval_conditions(d.get("conditions") or {}, ctx, trace)
        result = {"workflow_id": wid, "document_id": doc_id, "event": event, "matched": matched, "conditions": trace,
                  "dry_run": dry_run, "actions": []}
        if not matched:
            result["status"] = "skipped"
            return result
        if not dry_run:
            started = now_iso()
            try:  # loop guard: the unique index makes a second run for the same (workflow, document, event) impossible
                with self.db.write() as c:
                    c.execute("INSERT INTO workflow_runs(id,workflow_id,document_id,event,status,started_at) VALUES(?,?,?,?,?,?)",
                              (rid := new_id(), wid, doc_id, event, "running", started))
            except IntegrityError:
                result["status"] = "already_ran"
                return result
        ok_all = True
        for a in d["actions"]:
            try:
                res = self._act(a, ctx, dry_run)
            except Exception as e:  # noqa: BLE001 - one failing action must not stop the rest or the pipeline
                log.warning("workflow %s action %s failed: %s", wid, a.get("type"), e)
                res = ActionResult(a["type"], False, f"{type(e).__name__}: {e}")
            ok_all &= res.ok
            result["actions"].append({"type": res.type, "ok": res.ok, "message": res.message, **res.detail})
        result["status"] = "ok" if ok_all else "failed"
        if not dry_run:
            with self.db.write() as c:
                c.execute("UPDATE workflow_runs SET status=?, log_json=?, finished_at=? WHERE id=?",
                          (result["status"], json.dumps({"conditions": trace, "actions": result["actions"]}), now_iso(), rid))
        return result

    # actions ---------------------------------------------------
    def _act(self, a: dict, ctx: DocContext, dry: bool) -> ActionResult:
        t, doc_id = a["type"], ctx.doc["id"]
        if t in ("apply_tag", "remove_tag"):
            if not dry:
                self.org.tag_documents([doc_id], add=[a["tag"]] if t == "apply_tag" else [], remove=[a["tag"]] if t == "remove_tag" else [])
            return ActionResult(t, True, f"{'Tag' if t == 'apply_tag' else 'Untag'} '{a['tag']}'")
        if t == "add_to_collection":
            col = self.org.collection_by_name(a["collection"])
            if col is None:
                return ActionResult(t, False, f"Collection '{a['collection']}' does not exist")
            if col["kind"] != "manual":
                return ActionResult(t, False, f"'{a['collection']}' is a smart collection")
            if not dry:
                self.org.add_to_collection(col["id"], [doc_id])
            return ActionResult(t, True, f"Add to collection '{a['collection']}'")
        if t == "export_copy":
            return self._export_copy(a, ctx, dry)
        if t == "run_export":
            if dry:
                return ActionResult(t, True, f"Export as {a.get('format', 'csv')}")
            res = self.exporter.export([doc_id], a.get("format", "csv"), a.get("fields"), name="workflow")
            return ActionResult(t, True, f"Exported {res['name']}", {"file": res["name"]})
        if t == "notify":
            msg = _PLACEHOLDER.sub(lambda m: str(ctx.fields.get(m.group(1)) or ctx.doc.get(m.group(1)) or ""), a["message"])
            if not dry:
                self.bus.publish("notification", {"message": msg, "document_id": doc_id})
            return ActionResult(t, True, f"Notify: {msg}")
        if t == "webhook":
            if not self.settings.all().get("automation.allow_webhooks"):
                return ActionResult(t, False, "Webhooks are disabled (Settings > Automation > allow webhooks)")
            if not re.match(r"^https?://", a["url"]):
                return ActionResult(t, False, "Webhook URL must be http(s)")
            if not dry:
                body = json.dumps({"event": "workflow", "document": {"id": doc_id, "title": ctx.doc["title"], "type": ctx.type_name,
                                                                      "fields": ctx.fields}}).encode()
                req = urllib.request.Request(a["url"], data=body, headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10).close()
            return ActionResult(t, True, f"POST {a['url']}")
        return ActionResult(t, False, "Unknown action")

    def _export_copy(self, a: dict, ctx: DocContext, dry: bool) -> ActionResult:
        target = Path(a["target"])
        if not target.is_absolute():
            return ActionResult("export_copy", False, "target must be an absolute folder path")
        rel = render_template(a.get("template") or "{original_name}", ctx)
        if not rel:
            return ActionResult("export_copy", False, "template rendered an empty path")
        dest = (target / rel).resolve()
        if not dest.is_relative_to(target.resolve()):  # belt and braces: sanitized segments should already prevent this
            return ActionResult("export_copy", False, "template escapes the target folder")
        if dry:
            return ActionResult("export_copy", True, f"Copy to {dest}", {"path": str(dest)})
        src, _, _ = self.docs.original_path(ctx.doc["id"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        n, final = 1, dest
        while final.exists():
            final = dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
            n += 1
        shutil.copy2(src, final)  # a copy: the content-addressed original is never moved or modified
        return ActionResult("export_copy", True, f"Copied to {final}", {"path": str(final)})

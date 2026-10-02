import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from adstudio.core.errors import AppError
from adstudio.workflows.engine import DocContext, eval_conditions, render_template, validate_definition

from .test_documents import run_workers, settle, upload
from .test_extraction import INVOICE_TEXT, pdf_with_lines
from .test_queue import wait_for
from .test_search import drain


def ctx(fields=None, tags=(), type_name="Invoice", **doc):
    d = {"id": "01X", "original_name": "inv.pdf", "title": "inv", "state": "ready", "source": "upload",
         "mime": "application/pdf", "review_status": "none", **doc}
    return DocContext(d, fields or {}, list(tags), type_name)


# ── definition + conditions ───────────────────────────────────
@pytest.mark.parametrize("bad", [
    None, {}, {"trigger": {"event": "nope"}, "actions": [{"type": "notify", "message": "x"}]},
    {"trigger": {"event": "document.ready"}, "actions": []},
    {"trigger": {"event": "document.ready"}, "actions": [{"type": "rm_rf"}]},
    {"trigger": {"event": "document.ready"}, "actions": [{"type": "apply_tag"}]},
    {"trigger": {"event": "document.ready"}, "conditions": {"all": [{"field": "x", "op": "zz"}]}, "actions": [{"type": "notify", "message": "x"}]},
    {"trigger": {"event": "document.ready"}, "conditions": {"all": [{"field": "x", "op": "matches", "value": "(["}]}, "actions": [{"type": "notify", "message": "x"}]},
])
def test_invalid_definitions_rejected(bad):
    with pytest.raises(AppError) as e:
        validate_definition(bad)
    assert e.value.code == "invalid_workflow"


def test_condition_operators():
    c = ctx({"total": "54280.00", "vendor": "ABC Pvt Ltd", "invoice_date": "2026-03-12"}, tags=["urgent", "q1"])
    ev = lambda n: eval_conditions(n, c)  # noqa: E731
    assert ev({"field": "doc_type", "op": "eq", "value": "invoice"})  # case-insensitive
    assert ev({"field": "field:total", "op": "gt", "value": 50000}) and not ev({"field": "field:total", "op": "lt", "value": 50000})
    assert ev({"field": "field:total", "op": "gte", "value": "54,280"})  # numeric with a comma
    assert ev({"field": "field:invoice_date", "op": "lt", "value": "2026-04-01"})  # ISO dates compare as text
    assert ev({"field": "field:vendor", "op": "contains", "value": "pvt"}) and ev({"field": "field:vendor", "op": "matches", "value": "^abc"})
    assert ev({"field": "field:vendor", "op": "in", "value": ["xyz", "abc pvt ltd"]})
    assert ev({"field": "tag", "op": "contains", "value": "urgent"}) and ev({"field": "tag", "op": "ne", "value": "later"})
    assert ev({"field": "field:missing", "op": "exists"}) is False and ev({"field": "field:vendor", "op": "exists"})
    assert ev({"field": "field:missing", "op": "ne", "value": "x"})  # absent != anything
    assert ev({"all": [{"field": "state", "op": "eq", "value": "ready"}, {"any": [{"field": "tag", "op": "contains", "value": "nope"},
                                                                                   {"not": {"field": "field:total", "op": "lt", "value": 1}}]}]})
    assert ev({}) is True
    trace = []
    eval_conditions({"all": [{"field": "state", "op": "eq", "value": "x"}]}, c, trace)
    assert trace[0]["actual"] == "ready" and trace[0]["passed"] is False


def test_template_rendering_is_sanitized():
    c = ctx({"vendor": "ABC/../../evil", "invoice_date": "2026-03-12", "customer": 'A:B*C?'})
    assert render_template("{vendor}/{invoice_date:%Y-%m}/{original_name}", c) == "ABC_.._.._evil/2026-03/inv.pdf"
    assert render_template("../../{customer}/{missing}/x.pdf", c) == "A_B_C_/unknown/x.pdf"  # '..' segments dropped
    assert render_template("{invoice_date:%B %Y}", c) == "March 2026"
    assert render_template("{invoice_date:%Y}/{doc_type}", ctx({"invoice_date": "garbage"})) == "garbage/Invoice"


# ── end to end ────────────────────────────────────────────────
BIG_RULE = {"name": "File big invoices", "definition": {
    "trigger": {"event": "document.ready"},
    "conditions": {"all": [{"field": "doc_type", "op": "eq", "value": "Invoice"}, {"field": "field:total", "op": "gt", "value": 50000}]},
    "actions": [{"type": "apply_tag", "tag": "big"}, {"type": "add_to_collection", "collection": "Finance"},
                {"type": "export_copy", "target": "{target}", "template": "{vendor}/{invoice_date:%Y-%m}/{original_name}"},
                {"type": "notify", "message": "High-value invoice {invoice_number}"}]}}


def make_rule(client, tmp_path, rule=None):
    rule = json.loads(json.dumps(rule or BIG_RULE).replace("{target}", str(tmp_path / "out").replace("\\", "/")))
    return client.post("/api/v1/workflows", json=rule).json()


def test_workflow_runs_actions_once_and_never_touches_the_original(client, tmp_path):
    client.post("/api/v1/collections", json={"name": "Finance"})
    wf = make_rule(client, tmp_path)
    data = pdf_with_lines(INVOICE_TEXT)
    r = upload(client, "inv.pdf", data)
    run_workers(client)
    settle(client, r["document_id"])
    drain(client)
    doc = r["document_id"]
    assert client.get(f"/api/v1/documents/{doc}").json()["tags"] == ["big"]
    assert client.get("/api/v1/collections").json()["items"][0]["documents"] == 1
    copied = list((tmp_path / "out").rglob("inv.pdf"))
    assert len(copied) == 1 and copied[0].parent.relative_to(tmp_path / "out").as_posix() == "ABC Private Limited/2026-03"
    assert hashlib.sha256(copied[0].read_bytes()).hexdigest() == hashlib.sha256(data).hexdigest()
    path, _, _ = client.container.docs.original_path(doc)
    assert path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == hashlib.sha256(data).hexdigest()  # original intact
    runs = client.get(f"/api/v1/workflows/{wf['id']}/runs").json()["items"]
    assert len(runs) == 1 and runs[0]["status"] == "ok" and [a["type"] for a in runs[0]["log"]["actions"]][-1] == "notify"
    # loop guard: reprocessing reaches `ready` again but the rule does not run twice
    client.post(f"/api/v1/documents/{doc}/reprocess", json={"from_stage": "classify"})
    settle(client, doc)
    drain(client)
    assert len(client.get(f"/api/v1/workflows/{wf['id']}/runs").json()["items"]) == 1
    assert len(list((tmp_path / "out").rglob("*.pdf"))) == 1


def test_non_matching_documents_do_not_run(client, tmp_path):
    client.post("/api/v1/collections", json={"name": "Finance"})
    wf = make_rule(client, tmp_path)
    r = upload(client, "small.pdf", pdf_with_lines(INVOICE_TEXT.replace("54,280.00", "1,000.00").replace("46,000.00", "800.00").replace("8,280.00", "200.00")))
    run_workers(client)
    settle(client, r["document_id"])
    drain(client)
    assert client.get(f"/api/v1/workflows/{wf['id']}/runs").json()["items"] == []
    assert client.get(f"/api/v1/documents/{r['document_id']}").json()["tags"] == []


def test_dry_run_reports_without_side_effects_and_a_failing_action_does_not_stop_the_rest(client, tmp_path):
    wf = make_rule(client, tmp_path)  # note: collection "Finance" does NOT exist
    r = upload(client, "inv.pdf", pdf_with_lines(INVOICE_TEXT))
    run_workers(client)
    settle(client, r["document_id"])
    drain(client)
    runs = client.get(f"/api/v1/workflows/{wf['id']}/runs").json()["items"]
    acts = {a["type"]: a for a in runs[0]["log"]["actions"]}
    assert runs[0]["status"] == "failed" and acts["add_to_collection"]["ok"] is False and "does not exist" in acts["add_to_collection"]["message"]
    assert acts["apply_tag"]["ok"] and acts["export_copy"]["ok"] and acts["notify"]["ok"]  # the others still ran
    assert client.container.docs.get(r["document_id"])["state"] in ("ready", "needs_review")  # pipeline unaffected

    # dry run on another document (rule disabled so it does not fire for real): nothing changes
    client.patch(f"/api/v1/workflows/{wf['id']}", json={"enabled": False})
    r2 = upload(client, "inv2.pdf", pdf_with_lines(INVOICE_TEXT.replace("INV-20491", "INV-2")))
    settle(client, r2["document_id"])
    drain(client)
    out = client.post(f"/api/v1/workflows/{wf['id']}/run", json={"document_id": r2["document_id"]}).json()  # dry_run defaults true
    assert out["dry_run"] and out["matched"] and out["status"] == "failed"
    assert any(a["type"] == "export_copy" and "path" in a for a in out["actions"])
    assert len(list((tmp_path / "out").rglob("*.pdf"))) == 1  # the dry run copied nothing
    assert client.get(f"/api/v1/documents/{r2['document_id']}").json()["tags"] == []
    assert len(client.get(f"/api/v1/workflows/{wf['id']}/runs").json()["items"]) == 1  # and logged no run


def test_webhook_is_off_by_default_and_posts_when_enabled(client):
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}/hook"
        wf = client.post("/api/v1/workflows", json={"name": "hook", "definition": {
            "trigger": {"event": "document.imported"}, "actions": [{"type": "webhook", "url": url}]}}).json()
        doc = upload(client, "n.txt", b"hello webhook world")["document_id"]
        run_workers(client)
        runs = lambda: client.get(f"/api/v1/workflows/{wf['id']}/runs").json()["items"]  # noqa: E731
        assert wait_for(runs, 10)
        assert runs()[0]["status"] == "failed" and "disabled" in runs()[0]["log"]["actions"][0]["message"] and got == []
        client.patch("/api/v1/settings", json={"automation.allow_webhooks": True})
        again = client.post(f"/api/v1/workflows/{wf['id']}/run", json={"document_id": doc, "dry_run": False}).json()
        assert again["status"] == "already_ran" and got == []  # loop guard: one run per (workflow, document, event)
        with client.container.db.write() as c:
            c.execute("DELETE FROM workflow_runs")
        ok = client.post(f"/api/v1/workflows/{wf['id']}/run", json={"document_id": doc, "dry_run": False}).json()
        assert ok["status"] == "ok" and got[0]["document"]["id"] == doc
        bad = client.post("/api/v1/workflows", json={"name": "x", "definition": {
            "trigger": {"event": "document.ready"}, "actions": [{"type": "webhook", "url": "file:///etc/passwd"}]}})
        assert bad.status_code == 400 and bad.json()["code"] == "invalid_workflow"  # only http(s) URLs can be saved
    finally:
        srv.shutdown()


def test_review_resolved_event_triggers_rule(client):
    wf = client.post("/api/v1/workflows", json={"name": "tag reviewed", "definition": {
        "trigger": {"event": "document.review_resolved"}, "actions": [{"type": "apply_tag", "tag": "checked"}]}}).json()
    bad = INVOICE_TEXT.replace("54,280.00", "60,000.00")
    r = upload(client, "inv.pdf", pdf_with_lines(bad))
    run_workers(client)
    settle(client, r["document_id"])
    drain(client)
    assert client.get(f"/api/v1/documents/{r['document_id']}").json()["tags"] == []  # not resolved yet
    client.patch(f"/api/v1/documents/{r['document_id']}/fields/total", json={"value": "54,280.00"})
    assert wait_for(lambda: client.get(f"/api/v1/documents/{r['document_id']}").json()["tags"] == ["checked"], 10)
    assert wf["id"]


def test_workflow_crud_and_validation_errors_over_http(client):
    assert client.post("/api/v1/workflows", json={"name": "x", "definition": {"trigger": {"event": "bad"}}}).json()["code"] == "invalid_workflow"
    wf = client.post("/api/v1/workflows", json={"name": "ok", "definition": {
        "trigger": {"event": "document.ready"}, "actions": [{"type": "notify", "message": "hi"}]}}).json()
    assert client.patch(f"/api/v1/workflows/{wf['id']}", json={"enabled": False}).json()["enabled"] is False
    assert client.get("/api/v1/workflows").json()["items"][0]["last_run"] is None
    assert client.post(f"/api/v1/workflows/{wf['id']}/run", json={}).json()["code"] == "invalid_body"
    assert client.delete(f"/api/v1/workflows/{wf['id']}").status_code == 200
    assert client.get(f"/api/v1/workflows/{wf['id']}").status_code == 404

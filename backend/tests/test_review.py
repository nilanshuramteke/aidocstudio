import json

import pytest
from fastapi.testclient import TestClient

from adstudio.api.app import create_app
from adstudio.core import container as cmod
from adstudio.core.fakes import FakeLLM, FakeOCR
from adstudio.core.security import COOKIE_NAME

from .test_chat import ScriptedLLM
from .test_documents import run_workers, settle, upload
from .test_extraction import GOOD_GSTIN, INVOICE_TEXT, fields_of, pdf_with_lines
from .test_ocr import CountingOCR, scanned_pdf
from .test_queue import wait_for
from .test_search import drain, search, titles

BAD_TOTAL_TEXT = INVOICE_TEXT.replace("54,280.00", "60,000.00")


def invoice(client, text=INVOICE_TEXT, name="inv.pdf"):
    r = upload(client, name, pdf_with_lines(text))
    run_workers(client)
    settle(client, r["document_id"])
    drain(client)
    return r["document_id"]


def patch(client, doc, key, **body):
    return client.patch(f"/api/v1/documents/{doc}/fields/{key}", json=body)


def field(client, doc, key):
    return fields_of(client, doc)[1][key]


# ── edit / validate / accept / reject ─────────────────────────
def test_edit_validates_live_and_marks_corrected(client):
    doc = invoice(client)
    assert client.post(f"/api/v1/documents/{doc}/fields/invoice_date/validate", json={"value": "31/02/2026"}).json() == {
        "ok": False, "value": None, "messages": ["Not a valid date"]}
    ok = client.post(f"/api/v1/documents/{doc}/fields/invoice_date/validate", json={"value": "13 March 2026"}).json()
    assert ok == {"ok": True, "value": "2026-03-13", "messages": []}
    bad = patch(client, doc, "invoice_date", value="31/02/2026")
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_value"
    r = patch(client, doc, "invoice_date", value="13/03/2026").json()
    assert r["value"] == "2026-03-13" and r["status"] == "corrected" and r["confidence"] == 1.0
    with client.container.db.read() as c:
        assert c.execute("SELECT before_value, after_value FROM corrections").fetchone()[:] == ("2026-03-12", "2026-03-13")


def test_validator_failures_block_unless_forced(client):
    bad = GOOD_GSTIN[:-1] + ("A" if GOOD_GSTIN[-1] != "A" else "B")
    doc = invoice(client, INVOICE_TEXT.replace(GOOD_GSTIN, bad))
    assert field(client, doc, "vendor_gstin")["status"] == "needs_review"
    r = patch(client, doc, "vendor_gstin", value=bad)
    assert r.status_code == 422 and "checksum" in r.json()["detail"] and r.json()["code"] == "validation_failed"
    assert patch(client, doc, "vendor_gstin", value=GOOD_GSTIN).json()["status"] == "corrected"
    assert field(client, doc, "vendor_gstin")["validation"] == []  # re-validated after the edit
    forced = patch(client, doc, "vendor_gstin", value=bad, force=True).json()
    assert forced["status"] == "corrected" and forced["validation"]  # user override kept, failing check still shown


def test_accept_reject_and_edge_cases(client):
    doc = invoice(client, "Tax Invoice\nInvoice No: INV-9\nBill To: Acme Traders")
    assert field(client, doc, "total")["flag_reason"] == "missing_required"
    r = patch(client, doc, "total", status="accepted")
    assert r.status_code == 409 and r.json()["code"] == "nothing_to_accept"
    assert patch(client, doc, "customer", status="rejected").json()["status"] == "rejected"
    assert patch(client, doc, "customer", status="maybe").status_code == 400
    assert patch(client, doc, "nope", status="accepted").status_code == 404
    assert patch(client, doc, "total", value="12,500.00").json()["value"] == "12500.00"
    # corrected values are searchable; rejected values are not
    assert titles(search(client, "12500")) == ["inv"]
    assert search(client, "Acme Traders", mode="keyword")["results"][0]["field_hits"] == []


# ── undo ──────────────────────────────────────────────────────
def test_undo_restores_exact_previous_state_stepwise(client):
    doc = invoice(client)
    original = field(client, doc, "total")
    patch(client, doc, "total", value="1.00")
    patch(client, doc, "customer", status="rejected")
    assert client.post(f"/api/v1/documents/{doc}/fields/undo").json()["key"] == "customer"
    assert field(client, doc, "customer")["status"] == "auto"
    assert field(client, doc, "total")["value"] == "1.00"
    assert client.post(f"/api/v1/documents/{doc}/fields/undo").json()["key"] == "total"
    restored = field(client, doc, "total")
    assert {k: restored[k] for k in ("value", "raw_value", "status", "bbox", "page_no", "extractor")} == \
           {k: original[k] for k in ("value", "raw_value", "status", "bbox", "page_no", "extractor")}
    with client.container.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM corrections").fetchone()[0] == 0
    assert client.post(f"/api/v1/documents/{doc}/fields/undo").json()["code"] == "nothing_to_undo"


# ── cross-field reaction ──────────────────────────────────────
def test_fixing_total_clears_dependent_flags_and_completes_review(client):
    doc = invoice(client, BAD_TOTAL_TEXT)
    f = fields_of(client, doc)[1]
    assert {k for k, v in f.items() if v["flag_reason"] == "validation_failed"} == {"subtotal", "tax", "total"}
    assert client.container.docs.get(doc)["state"] == "needs_review"
    patch(client, doc, "total", value="54,280.00")
    f = fields_of(client, doc)[1]
    assert all(v["status"] in ("auto", "corrected") for v in f.values())  # subtotal/tax unflagged by the edit
    d = client.container.docs.get(doc)
    assert d["state"] == "ready" and d["review_status"] == "reviewed"


# ── queue / next / complete / bulk ────────────────────────────
def soften(client, doc, key, conf=0.8):
    with client.container.db.write() as c:
        c.execute("UPDATE extracted_fields SET status='needs_review', flag_reason='low_confidence', confidence=? "
                  "WHERE document_id=? AND key=?", (conf, doc, key))
        c.execute("UPDATE documents SET review_status='needs_review', state='needs_review' WHERE id=?", (doc,))


def test_queue_order_next_complete_and_bulk(client):
    blocking = invoice(client, BAD_TOTAL_TEXT.replace("INV-20491", "INV-1"), "a.pdf")
    soft = invoice(client, INVOICE_TEXT.replace("INV-20491", "INV-2"), "b.pdf")
    soften(client, soft, "customer", 0.8)
    q = client.get("/api/v1/review/queue").json()
    assert [i["document_id"] for i in q["items"]] == [blocking, soft]  # blocking first
    assert q["items"][0]["blocking"] == 3 and q["items"][1]["soft"] == 1 and q["fields_left"] == 4
    assert [i["document_id"] for i in client.get("/api/v1/review/queue?type=invoice").json()["items"]] == [blocking, soft]
    assert client.get("/api/v1/review/queue?type=contract").json()["items"] == []
    assert [i["document_id"] for i in client.get("/api/v1/review/queue?min_conf=0.75").json()["items"]] == [soft]

    nxt = client.get("/api/v1/review/next").json()
    assert nxt["document_id"] == blocking and nxt["fields"][0]["blocking"] and nxt["queue"] == 2
    assert client.get(f"/api/v1/review/next?after={blocking}").json()["document_id"] == soft

    r = client.post(f"/api/v1/review/{blocking}/complete")
    assert r.status_code == 409 and r.json()["code"] == "blocking_fields"

    res = client.post("/api/v1/documents/bulk", json={"ids": [blocking, soft], "action": "accept_all_confident"}).json()["results"]
    assert [x["accepted"] for x in res] == [0, 1]  # blocking fields are never bulk-accepted
    assert field(client, soft, "customer")["status"] == "accepted"
    d = client.container.docs.get(soft)
    assert d["state"] == "ready" and d["review_status"] == "reviewed"
    assert client.post("/api/v1/documents/bulk", json={"ids": [soft], "action": "explode"}).status_code == 400

    patch(client, blocking, "total", value="54,280.00")  # fix the root cause, then complete
    assert client.post(f"/api/v1/review/{blocking}/complete").json()["state"] == "ready"
    assert client.get("/api/v1/review/queue").json()["items"] == []
    assert client.get("/api/v1/review/next").json() == {"document_id": None, "fields": [], "queue": 0}


def test_complete_accepts_remaining_soft_fields(client):
    doc = invoice(client)
    soften(client, doc, "customer", 0.8)
    soften(client, doc, "tax", 0.75)
    assert client.post(f"/api/v1/review/{doc}/complete").json()["review_status"] == "reviewed"
    assert {field(client, doc, k)["status"] for k in ("customer", "tax")} == {"accepted"}


def test_clear_a_20_document_queue_by_the_keyboard_equivalent_flow(client):
    """Same actions the keyboard maps to: Enter accept, E edit, Backspace reject, Tab next, Ctrl+Z undo."""
    for i in range(20):
        upload(client, f"inv{i}.pdf", pdf_with_lines(BAD_TOTAL_TEXT.replace("INV-20491", f"INV-{i:04d}")))
    run_workers(client)
    drain(client, 60)
    assert client.get("/api/v1/review/queue").json()["documents"] == 20
    steps = 0
    while (nxt := client.get("/api/v1/review/next").json())["document_id"]:
        doc = nxt["document_id"]
        for f in nxt["fields"]:
            if f["key"] == "total":
                patch(client, doc, "total", value="54,280.00")
        if steps == 0:  # exercise undo mid-flow, then redo the edit
            client.post(f"/api/v1/documents/{doc}/fields/undo")
            patch(client, doc, "total", value="54,280.00")
        assert client.post(f"/api/v1/review/{doc}/complete").status_code == 200
        steps += 1
        assert steps <= 20
    assert steps == 20 and client.get("/api/v1/review/queue").json()["items"] == []
    m = client.get("/api/v1/review/metrics").json()
    assert m["documents_reviewed"] == 20 and m["corrected"] >= 20


# ── re-extract region ─────────────────────────────────────────
def test_rerun_region_reads_text_layer_exactly(client):
    doc = invoice(client)
    src = field(client, doc, "invoice_number")
    r = client.post(f"/api/v1/documents/{doc}/fields/customer/rerun", json={"page_no": 1, "bbox": src["bbox"]}).json()
    assert r["raw_value"] == "INV-20491" and r["extractor"] == "region:textlayer" and r["bbox"] == [round(v, 5) for v in src["bbox"]]
    # outside page / empty region / missing page
    assert client.post(f"/api/v1/documents/{doc}/fields/customer/rerun", json={"page_no": 1, "bbox": [0.9, 0.9, 0.5, 0.5]}).json()["code"] == "invalid_region"
    assert client.post(f"/api/v1/documents/{doc}/fields/customer/rerun", json={"page_no": 1, "bbox": [0.9, 0.95, 0.05, 0.03]}).json()["code"] == "empty_region"
    assert client.post(f"/api/v1/documents/{doc}/fields/customer/rerun", json={"bbox": [0.1, 0.1, 0.1, 0.1]}).json()["code"] == "invalid_body"


def test_rerun_region_on_scanned_page_uses_ocr_on_the_crop(client):
    counter = CountingOCR()
    client.container.ocr_runner.inline = counter
    client.container.ocr_runner.engine = "counting"
    r = upload(client, "scan.pdf", scanned_pdf("Tax Invoice Invoice No INV-77 Total 10.00"))
    run_workers(client)
    settle(client, r["document_id"])
    drain(client)
    base_calls = counter.calls
    with client.container.db.write() as c:  # make sure a total row exists to re-extract into
        c.execute("INSERT OR IGNORE INTO extracted_fields(id,document_id,key,status,updated_at) VALUES('x',?,'total','needs_review','t')",
                  (r["document_id"],))
    out = client.post(f"/api/v1/documents/{r['document_id']}/fields/total/rerun", json={"page_no": 1, "bbox": [0.1, 0.1, 0.5, 0.2]})
    assert out.status_code == 200 and counter.calls == base_calls + 1
    assert out.json()["extractor"] == "region:counting" and out.json()["raw_value"].startswith("call")


def test_rerun_without_region_reextracts_and_native_text_has_no_regions(client):
    doc = invoice(client)
    patch(client, doc, "total", value="1.00")
    r = client.post(f"/api/v1/documents/{doc}/fields/total/rerun").json()
    assert r["value"] == "54280.00" and r["status"] in ("auto", "needs_review") and r["extractor"] == "rule"
    note = upload(client, "n.txt", b"Tax Invoice\nInvoice No: INV-5\nTotal: 5.00\n")["document_id"]
    run_workers(client)
    settle(client, note)
    resp = client.post(f"/api/v1/documents/{note}/fields/total/rerun", json={"page_no": 1, "bbox": [0.1, 0.1, 0.2, 0.2]})
    assert resp.json()["code"] == "no_region_support"


# ── metrics, few-shot, settings ───────────────────────────────
def test_metrics_count_corrections_and_auto_errors_and_ignore_undone(client):
    doc = invoice(client, BAD_TOTAL_TEXT)
    patch(client, doc, "tax", status="accepted")  # flagged -> accepted
    patch(client, doc, "total", value="54,280.00")  # flagged -> corrected
    patch(client, doc, "customer", value="Acme Traders Ltd")  # auto field edited -> auto-accept error
    patch(client, doc, "vendor", value="Wrong Co")
    client.post(f"/api/v1/documents/{doc}/fields/undo")  # undone: must not count
    m = client.get("/api/v1/review/metrics").json()
    assert m["corrected"] == 1 and m["accepted"] >= 1 and m["auto_accept_errors"] == 1
    assert 0 < m["correction_rate"] <= 1 and m["auto_accept_error_rate"] > 0


def test_corrections_become_few_shot_examples_for_the_model(config):
    llm = ScriptedLLM(reply="{}")
    c = cmod.build_container(config, ocr=FakeOCR(), llm=llm, start_workers=False, auto_llm=False)
    with TestClient(create_app(c), base_url="http://127.0.0.1") as tc:
        tc.cookies.set(COOKIE_NAME, c.auth.session_token)
        tc.container = c
        a = invoice(tc, INVOICE_TEXT.replace("Total Amount Payable", "Amount"), "a.pdf")  # rules miss total -> LLM asked
        patch(tc, a, "total", value="54,280.00")
        llm.calls.clear()
        upload(tc, "b.pdf", pdf_with_lines(INVOICE_TEXT.replace("INV-20491", "INV-2").replace("Total Amount Payable", "Amount")))
        wait_for(lambda: any("Extract the requested fields" in x["messages"][0]["content"] for x in llm.calls), 20)
        prompt = next(x["messages"][0]["content"] for x in llm.calls if "Extract the requested fields" in x["messages"][0]["content"])
        assert "Earlier human corrections" in prompt and "'54280.00'" in prompt and "Amount: 54,280.00" in prompt


def test_threshold_settings_validated(client):
    assert client.patch("/api/v1/settings", json={"review.auto_accept": 0.8, "review.soft_review": 0.9}).json()["code"] == "invalid_setting"
    assert client.patch("/api/v1/settings", json={"review.auto_accept": 1.5}).json()["code"] == "invalid_setting"
    assert client.patch("/api/v1/settings", json={"review.soft_review": 0.6, "review.auto_accept": 0.95}).status_code == 200

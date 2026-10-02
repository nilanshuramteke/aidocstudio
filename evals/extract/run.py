"""Classification + extraction eval against a REAL local model: `python evals/extract/run.py [model]`.

Text documents with gold type and fields (some phrased so the rules cannot catch them, forcing the LLM path).
Reports type accuracy, field recall/precision, and how many extracted values failed grounding. Skips if Ollama is down.
"""
import io
import json
import sys
import tempfile
import time
from pathlib import Path

from adstudio.ai.providers.ollama import OllamaLLM
from adstudio.core.config import Config
from adstudio.core.container import build_container
from adstudio.core.fakes import FakeOCR

CASES = [
    ("inv1", "Invoice", {"invoice_number": "INV-20491", "total": "54280"},
     "TAX INVOICE\nVendor: ABC Private Limited\nInvoice No: INV-20491\nDate: 12/03/2026\nGSTIN: 27AAPFU0939F1ZV\nTotal Amount Payable: Rs. 54,280.00"),
    ("inv2", "Invoice", {"invoice_number": "2026/0087", "total": "1180"},
     "Bill from Sharma Traders\nBill reference 2026/0087 dated 5 Jan 2026\nSubtotal 1,000.00  GST 18% 180.00\nGrand total due: 1,180.00"),
    ("rcp1", "Receipt", {"total": "1250"},
     "Cafe Mocha Roasters\nThank you for dining with us\nCoffee x2 800\nCake 450\nTotal 1,250.00\nPaid by card"),
    ("con1", "Contract", {},
     "SERVICE AGREEMENT\nThis agreement is made between Acme Corp and Beta LLP. The parties agree to the terms below. "
     "Term: 12 months. Either party may terminate with 60 days notice. Governing law: India."),
    ("oth1", "Other", {},
     "Hey, just a reminder that the team lunch is on Friday at 1pm. Bring your own drinks. Cheers!"),
]


def main() -> None:
    model = sys.argv[1] if len(sys.argv) > 1 else "llama3.1:8b"
    llm = OllamaLLM(model=model)
    h = llm.health()
    if not h.ok:
        print(f"SKIPPED: {h.detail}")
        return
    c = build_container(Config.load(Path(tempfile.mkdtemp()), workers=2), ocr=FakeOCR(), llm=llm, start_workers=True, auto_llm=False)
    ids = {n: c.docs.import_stream(f"{n}.txt", io.BytesIO(t.encode())).document_id for n, _, _, t in CASES}
    t0 = time.perf_counter()
    while any(c.queue.list(s) for s in ("queued", "running")):
        time.sleep(1)
    secs = round(time.perf_counter() - t0, 1)
    type_ok = fields_hit = fields_total = extracted = ungrounded = 0
    for n, gtype, gfields, _ in CASES:
        with c.db.read() as conn:
            d = conn.execute("SELECT d.doc_type_id, t.name tname FROM documents d LEFT JOIN document_types t ON t.id=d.doc_type_id WHERE d.id=?", (ids[n],)).fetchone()
            rows = conn.execute("SELECT key, value, status, confidence, extractor FROM extracted_fields WHERE document_id=?", (ids[n],)).fetchall()
        got = {r["key"]: r for r in rows}
        type_ok += d["tname"] == gtype
        line = [f"{n}: type={d['tname']} (want {gtype})"]
        for k, v in gfields.items():
            fields_total += 1
            val = (got[k]["value"] if k in got and got[k]["value"] else "").replace(",", "")
            hit = v in val
            fields_hit += hit
            line.append(f"{k}={val!r} {'ok' if hit else 'MISS'}")
        extracted += sum(1 for r in rows if r["value"])
        ungrounded += sum(1 for r in rows if r["value"] and (r["confidence"] or 0) < 0.5)
        line.append("extractors=" + ",".join(sorted({r["extractor"] or "-" for r in rows if r["value"]}))); print("  ".join(line))
    out = {"model": model, "type_accuracy": type_ok / len(CASES), "field_recall": fields_hit / max(1, fields_total),
           "values_extracted": extracted, "low_confidence_values": ungrounded, "seconds": secs}
    print(json.dumps(out, indent=2))
    Path(__file__).with_name("results.json").write_text(json.dumps(out, indent=2))
    c.stop()


if __name__ == "__main__":
    main()

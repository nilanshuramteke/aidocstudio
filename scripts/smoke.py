"""End-to-end smoke test against a REAL launched app (real server, real OCR engine, built frontend):
`python scripts/smoke.py`. Uses a temp data dir; touches nothing in your library. Exit code 0 = all good."""
import io
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pymupdf
from PIL import Image, ImageDraw, ImageFont

TEXT = """ABC Private Limited
Tax Invoice
Invoice No: INV-20491
Invoice Date: 12/03/2026
GSTIN: 27ABCDE1234F1ZV
Bill To: Acme Traders
Subtotal: 46,000.00
GST 18%: 8,280.00
Total Amount Payable: 54,280.00"""


def pdf_bytes() -> bytes:
    d = pymupdf.open()
    p = d.new_page(width=595, height=842)
    for i, line in enumerate(TEXT.splitlines()):
        p.insert_text((50, 60 + i * 18), line, fontsize=11)
    return d.tobytes()


def scan_png() -> bytes:
    img = Image.new("RGB", (1000, 420), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 34)
    except OSError:
        font = ImageFont.load_default(size=34)
    for i, line in enumerate(["Receipt", "Cafe Mocha Roasters", "Thank you for dining", "Total 1,250.00 paid in cash"]):
        d.text((30, 25 + i * 70), line, fill="black", font=font)
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


def main() -> int:
    data = tempfile.mkdtemp(prefix="adstudio-smoke-")
    proc = subprocess.Popen([sys.executable, "-m", "adstudio.main", "--data-dir", data, "--no-browser"], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, cwd=Path(__file__).parent.parent / "backend")
    try:
        url = None
        t0 = time.time()
        while time.time() - t0 < 60 and url is None:
            line = proc.stdout.readline()
            m = re.search(r"Open: (http://\S+)", line)
            url = m.group(1) if m else None
        assert url, "server did not start"
        base = url.split("/?")[0]
        c = httpx.Client(base_url=base, follow_redirects=True, timeout=30)
        r = c.get(url.replace(base, ""))
        assert r.status_code == 200 and "<div id=\"root\">" in r.text, "built frontend is not being served"
        print("OK  launcher link -> session cookie -> SPA served")
        assert c.get("/api/v1/health").json()["db"]["ok"], "health"
        health = c.get("/api/v1/health").json()
        print(f"OK  health: ocr={health['providers']['ocr']['ok']} llm={health['providers']['llm']['ok']} vec={health['capabilities']['vec']}")

        res = c.post("/api/v1/documents/import", files=[("files", ("invoice.pdf", pdf_bytes())), ("files", ("receipt.png", scan_png()))]).json()["results"]
        ids = [x["document_id"] for x in res]
        assert all(x["status"] == "created" for x in res), res
        deadline = time.time() + 180
        while time.time() < deadline:
            states = [c.get(f"/api/v1/documents/{i}").json()["state"] for i in ids]
            if all(s in ("ready", "needs_review", "failed") for s in states):
                break
            time.sleep(1)
        print("OK  pipeline finished:", dict(zip(["invoice.pdf", "receipt.png"], states)))
        assert "failed" not in states, states

        f = {x["key"]: x for x in c.get(f"/api/v1/documents/{ids[0]}/fields").json()["fields"]}
        assert f["invoice_number"]["value"] == "INV-20491" and f["total"]["value"] == "54280.00", f
        print(f"OK  extraction: invoice_number={f['invoice_number']['value']} total={f['total']['value']} (bbox {f['total']['bbox'] is not None})")
        ocr_text = c.get(f"/api/v1/documents/{ids[1]}/pages/1/text").json()["text"]
        assert "cafe" in ocr_text.lower() or "mocha" in ocr_text.lower(), ocr_text
        print(f"OK  real OCR on the scanned receipt: {ocr_text[:60]!r}")

        time.sleep(2)  # indexing jobs
        s = c.post("/api/v1/search", json={"q": "Acme Traders"}).json()
        assert s["results"] and s["results"][0]["document"]["title"] == "invoice", s
        s2 = c.post("/api/v1/search", json={"q": "type:invoice above 50000 in 2026"}).json()
        assert len(s2["results"]) == 1
        print(f"OK  search: keyword hit + parsed filters {s2['parsed_filters']}")

        r = c.patch(f"/api/v1/documents/{ids[0]}/fields/customer", json={"value": "Acme Trading Co"})
        assert r.status_code == 200 and r.json()["status"] == "corrected"
        assert c.post("/api/v1/export", json={"document_ids": ids, "format": "csv"}).status_code == 200
        b = c.post("/api/v1/backup", json={"passphrase": "smoke"}).json()
        assert b["encrypted"] and b["size"] > 0
        print("OK  review edit, CSV export, encrypted backup")
        chat = c.post("/api/v1/chat", json={"message": "What is the invoice total?"})
        events = re.findall(r"^event: (\w+)", chat.text, re.M)
        assert "sources" in events and ("done" in events or "error" in events), events
        print(f"OK  Ask AI stream events: {sorted(set(events))}")
        print("\nSMOKE PASSED")
        return 0
    except AssertionError as e:
        print("SMOKE FAILED:", e)
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())

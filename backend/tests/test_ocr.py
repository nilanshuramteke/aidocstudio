import io
import json

import cv2
import numpy as np
import pymupdf
import pytest
from PIL import Image, ImageDraw, ImageFont

from adstudio.core.interfaces import OCRCaps, OCRPage, OCRWord, ProviderHealth
from adstudio.ocr.enhance import _rotate, enhance, estimate_skew
from adstudio.ocr.runner import OCRRunner, OCRTimeout

from .test_documents import make_pdf, make_png, settle, upload, run_workers


def text_image(text="Invoice Total 1234", size=(900, 200), angle=0.0) -> Image.Image:
    img = Image.new("L", size, 255)
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 48)
    except OSError:
        font = ImageFont.load_default(size=48)
    for i, line in enumerate(text.split("\n")):
        d.text((30, 30 + i * 70), line, fill=0, font=font)
    return img.rotate(angle, expand=False, fillcolor=255) if angle else img


def png_bytes(img: Image.Image) -> bytes:
    b = io.BytesIO()
    img.convert("RGB").save(b, "PNG")
    return b.getvalue()


def scanned_pdf(text="Invoice Total 1234") -> bytes:
    d = pymupdf.open()
    page = d.new_page(width=400, height=200)
    page.insert_image(page.rect, stream=png_bytes(text_image(text)))
    return d.tobytes()


class CountingOCR:
    name = "counting"

    def __init__(self, confs=(0.95,)):
        self.calls, self.confs = 0, list(confs)

    def capabilities(self):
        return OCRCaps(langs=["eng"])

    def health(self):
        return ProviderHealth(True)

    def recognize(self, image, lang):
        conf = self.confs[min(self.calls, len(self.confs) - 1)]
        self.calls += 1
        return OCRPage([OCRWord(f"call{self.calls}", 0.1, 0.1, 0.1, 0.05, conf)], f"call{self.calls}", conf)


def words(client, doc_id, n=1):
    return client.get(f"/api/v1/documents/{doc_id}/pages/{n}/words").json()


def test_born_digital_pdf_skips_ocr_and_has_normalized_boxes(client):
    fake = client.container.ocr_runner.inline
    r = upload(client, "digital.pdf", make_pdf(1))
    run_workers(client)
    settle(client, r["document_id"])
    w = words(client, r["document_id"])
    assert w["provider"] == "pymupdf-textlayer" and w["text_source"] == "text_layer"
    assert {x["t"] for x in w["words"]} >= {"Page", "hello", "invoice"}
    assert all(0 <= x["x"] <= 1 and 0 <= x["y"] <= 1 and 0 < x["w"] <= 1 for x in w["words"])
    assert "invoice" in client.get(f"/api/v1/documents/{r['document_id']}/pages/1/text").json()["text"]
    assert fake.__class__.__name__ == "FakeOCR"  # never invoked: page text came from the text layer


def test_scanned_pdf_and_image_go_through_ocr(client):
    pdf = upload(client, "scan.pdf", scanned_pdf())
    img = upload(client, "photo.png", make_png("white"))
    run_workers(client)
    settle(client, pdf["document_id"])
    settle(client, img["document_id"])
    assert words(client, pdf["document_id"])["text_source"] == "ocr"
    assert words(client, img["document_id"])["provider"] == "fake-ocr"
    d = client.get(f"/api/v1/documents/{pdf['document_id']}").json()
    assert d["pages"][0]["ocr_conf"] == pytest.approx(0.95)


def test_ocr_results_cached_until_forced(client):
    counter = CountingOCR()
    client.container.ocr_runner.inline = counter
    client.container.ocr_runner.engine = "counting"
    r = upload(client, "scan.pdf", scanned_pdf())
    run_workers(client)
    settle(client, r["document_id"])
    assert counter.calls == 1
    client.post(f"/api/v1/documents/{r['document_id']}/reprocess", json={"from_stage": "paginate"})
    settle(client, r["document_id"])
    import time; time.sleep(0.5)
    assert counter.calls == 1  # served from ocr_results cache
    client.post(f"/api/v1/documents/{r['document_id']}/reprocess", json={"from_stage": "text"})
    from .test_queue import wait_for
    assert wait_for(lambda: counter.calls == 2)


def test_low_confidence_triggers_enhance_and_keeps_better(client):
    counter = CountingOCR(confs=(0.5, 0.9))
    client.container.ocr_runner.inline = counter
    client.container.ocr_runner.engine = "counting"
    r = upload(client, "scan.pdf", scanned_pdf())
    run_workers(client)
    settle(client, r["document_id"])
    assert counter.calls == 2
    assert words(client, r["document_id"])["words"][0]["t"] == "call2"  # the enhanced pass won

    counter2 = CountingOCR(confs=(0.5, 0.3))  # enhancing made it worse: keep the original
    client.container.ocr_runner.inline = counter2
    r2 = upload(client, "scan2.pdf", scanned_pdf("Different words here"))
    settle(client, r2["document_id"])
    assert words(client, r2["document_id"])["words"][0]["t"] == "call1"


def test_missing_ocr_engine_fails_with_actionable_message(config, monkeypatch):
    from fastapi.testclient import TestClient
    from adstudio.api.app import create_app
    from adstudio.core import container as cmod
    from adstudio.core.security import COOKIE_NAME

    monkeypatch.setattr(cmod, "choose_engine", lambda pref: None)
    c = cmod.build_container(config, start_workers=False, auto_llm=False)
    with TestClient(create_app(c), base_url="http://127.0.0.1") as tc:
        tc.cookies.set(COOKIE_NAME, c.auth.session_token)
        tc.container = c
        assert tc.get("/api/v1/health").json()["providers"]["ocr"]["configured"] is False
        r = upload(tc, "scan.pdf", scanned_pdf())
        c.start()
        d = settle(tc, r["document_id"])
        assert d["state"] == "failed" and "No OCR engine" in d["state_detail"]["error"]
        ok = upload(tc, "digital.pdf", make_pdf(1))  # text-layer docs still work without any engine
        assert settle(tc, ok["document_id"])["state"] in ("ready", "needs_review")


# ── enhance ───────────────────────────────────────────────────
@pytest.mark.parametrize("angle", [3.0, -2.5])
def test_deskew_estimate_recovers_rotation(angle):
    img = np.array(text_image("Invoice Total 1234\nSecond line of text here\nThird line", (900, 320), angle=angle))
    est = estimate_skew(img)
    assert abs(est + angle) <= 0.75 or abs(est - angle) <= 0.75  # sign convention is cv2's; magnitude must match
    fixed = _rotate(img, est)
    assert abs(estimate_skew(fixed)) <= 0.5  # straightened


def test_enhance_returns_binary_png():
    out = enhance(png_bytes(text_image(angle=2.0)))
    arr = cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_GRAYSCALE)
    assert set(np.unique(arr)) <= {0, 255}


# ── real engine through a spawned process ─────────────────────
def test_runner_timeout_kills_pool_and_recovers():
    pytest.importorskip("rapidocr_onnxruntime")
    runner = OCRRunner("rapidocr", workers=1, timeout_s=0.05)  # cold model load cannot finish in 50 ms
    png = png_bytes(text_image())
    with pytest.raises(OCRTimeout):
        runner.recognize(png, ["eng"])
    runner.timeout_s = 120
    page = runner.recognize(png, ["eng"])  # fresh pool works after the kill
    runner.close()
    assert "Invoice" in page.text or "Total" in page.text
    assert all(0 <= w.x <= 1 and 0 <= w.y <= 1 for w in page.words)


def test_real_engine_end_to_end(config):
    pytest.importorskip("rapidocr_onnxruntime")
    from fastapi.testclient import TestClient
    from adstudio.api.app import create_app
    from adstudio.core.container import build_container
    from adstudio.core.security import COOKIE_NAME

    c = build_container(config, start_workers=False, auto_llm=False)
    assert c.ocr_runner is not None and c.ocr_runner.engine in ("rapidocr", "tesseract")
    with TestClient(create_app(c), base_url="http://127.0.0.1") as tc:
        tc.cookies.set(COOKIE_NAME, c.auth.session_token)
        tc.container = c
        r = upload(tc, "scan.pdf", scanned_pdf("Invoice Total 1234"))
        c.start()
        from .test_queue import wait_for
        assert wait_for(lambda: c.docs.get(r["document_id"])["state"] in ("ready", "needs_review", "failed"), timeout=120)
        text = tc.get(f"/api/v1/documents/{r['document_id']}/pages/1/text").json()["text"].lower()
        assert "invoice" in text and "1234" in text

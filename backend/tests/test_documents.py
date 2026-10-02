import io
import time
import zipfile

import docx
import pymupdf
from PIL import Image

from adstudio.core.config import Config
from adstudio.core.container import build_container
from adstudio.documents.service import Limits
from adstudio.ingestion.sniff import sniff

from .test_queue import wait_for


def make_pdf(pages=2, password=None) -> bytes:
    d = pymupdf.open()
    for i in range(pages):
        d.new_page(width=300, height=400).insert_text((50, 100), f"Page {i + 1} hello invoice total amount due payable within thirty days")
    kw = {"encryption": pymupdf.PDF_ENCRYPT_AES_256, "user_pw": password, "owner_pw": password} if password else {}
    return d.tobytes(**kw)


def make_png(color="red", size=(120, 80)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "PNG")
    return b.getvalue()


def make_docx(text="Contract between A and B") -> bytes:
    d = docx.Document()
    d.add_paragraph(text)
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def make_zip(entries: dict[str, bytes]) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for n, data in entries.items():
            z.writestr(n, data)
    return b.getvalue()


def upload(client, name, data):
    return client.post("/api/v1/documents/import", files=[("files", (name, data))]).json()["results"][0]


def run_workers(client):
    c = client.container
    c.start()
    return c


TERMINAL = ("ready", "needs_review", "failed")


def settle(client, doc_id, timeout=30):
    """Wait for the whole pipeline (paginate -> text -> classify -> extract -> validate) to finish."""
    q = client.container.docs
    assert wait_for(lambda: q.get(doc_id)["state"] in TERMINAL, timeout), "pipeline did not finish"
    return q.get(doc_id)


# ── sniffing ──────────────────────────────────────────────────
def test_sniff_by_magic_not_extension():
    assert sniff(make_pdf()[:16]) == "pdf"
    assert sniff(make_png()[:16]) == "png"
    assert sniff(b"hello, plain text\n") == "txt"
    assert sniff(b"MZ\x90\x00\x03\x00\x00\x00") is None  # an .exe renamed .pdf is not a pdf
    assert sniff(b"\x00\x01\x02binary") is None


# ── import + pipeline ─────────────────────────────────────────
def test_import_pdf_paginates_and_renders(client):
    r = upload(client, "invoice.pdf", make_pdf(3))
    assert r["status"] == "created"
    run_workers(client)
    d = settle(client, r["document_id"])
    assert d["state"] in ("ready", "needs_review") and d["page_count"] == 3 and len(d["pages"]) == 3
    img = client.get(f"/api/v1/documents/{r['document_id']}/pages/2/image?w=300")
    assert img.status_code == 200 and img.headers["content-type"] == "image/webp"
    assert Image.open(io.BytesIO(img.content)).width == 300
    assert client.get(f"/api/v1/documents/{r['document_id']}/pages/9/image").status_code == 404


def test_page_render_is_fast_after_first(client):
    r = upload(client, "a.pdf", make_pdf(1))
    run_workers(client)
    settle(client, r["document_id"])
    url = f"/api/v1/documents/{r['document_id']}/pages/1/image"
    client.get(url)
    t = time.perf_counter()
    client.get(url)
    assert time.perf_counter() - t < 1.0


def test_duplicate_import_returns_existing(client):
    data = make_png()
    first = upload(client, "a.png", data)
    second = upload(client, "renamed.png", data)
    assert second["status"] == "duplicate" and second["document_id"] == first["document_id"]
    assert len(client.get("/api/v1/documents").json()["items"]) == 1


def test_reimport_after_soft_delete_creates_new_doc(client):
    data = make_png()
    first = upload(client, "a.png", data)
    assert client.delete(f"/api/v1/documents/{first['document_id']}").status_code == 200
    assert client.get(f"/api/v1/documents/{first['document_id']}").status_code == 404
    second = upload(client, "a.png", data)
    assert second["status"] == "created" and second["document_id"] != first["document_id"]
    assert client.post(f"/api/v1/documents/{first['document_id']}/restore").status_code == 409


def test_corrupt_and_encrypted_pdfs_fail_with_actionable_message(client):
    bad = upload(client, "bad.pdf", b"%PDF-1.4\nthis is not really a pdf")
    enc = upload(client, "locked.pdf", make_pdf(1, password="secret"))
    run_workers(client)
    b, e = settle(client, bad["document_id"]), settle(client, enc["document_id"])
    assert b["state"] == "failed" and "Corrupt" in b["state_detail"]["error"]
    assert e["state"] == "failed" and "password" in e["state_detail"]["error"]
    # failed docs never block the queue
    ok = upload(client, "ok.pdf", make_pdf(1))
    assert settle(client, ok["document_id"])["state"] in ("ready", "needs_review")


def test_unsupported_and_empty_rejected(client):
    assert upload(client, "x.pdf", b"MZ\x90\x00binary\x00\x00")["status"] == "rejected"
    assert upload(client, "empty.txt", b"")["reason"] == "empty file"


def test_oversize_rejected(client):
    client.container.docs.limits = Limits(max_file_bytes=1000)
    r = upload(client, "big.txt", b"a" * 5000)
    assert r["status"] == "rejected" and "exceeds" in r["reason"]


def test_docx_and_txt_native_text(client):
    d = upload(client, "contract.docx", make_docx())
    t = upload(client, "note.txt", "héllo wörld".encode())
    run_workers(client)
    settle(client, d["document_id"])
    settle(client, t["document_id"])
    assert "Contract between" in client.get(f"/api/v1/documents/{d['document_id']}/pages/1/text").json()["text"]
    assert client.get(f"/api/v1/documents/{t['document_id']}/pages/1/text").json()["text"] == "héllo wörld"
    assert client.get(f"/api/v1/documents/{d['document_id']}/pages/1/image").json()["code"] == "no_image"


def test_multiframe_tiff(client):
    frames = [Image.new("RGB", (50, 50), c) for c in ("red", "blue")]
    b = io.BytesIO()
    frames[0].save(b, "TIFF", save_all=True, append_images=frames[1:])
    r = upload(client, "scan.tif", b.getvalue())
    run_workers(client)
    assert settle(client, r["document_id"])["page_count"] == 2
    assert client.get(f"/api/v1/documents/{r['document_id']}/pages/2/image?w=100").status_code == 200


def test_file_endpoint_range_and_safe_headers(client):
    r = upload(client, "n.txt", b"0123456789")
    resp = client.get(f"/api/v1/documents/{r['document_id']}/file", headers={"Range": "bytes=2-4"})
    assert resp.status_code == 206 and resp.content == b"234"
    assert "sandbox" in resp.headers["content-security-policy"]


# ── zip handling ──────────────────────────────────────────────
def test_zip_expands_children_and_blocks_traversal(client):
    z = make_zip({"a.png": make_png("red"), "../../evil.txt": b"owned", "sub/b.png": make_png("blue"),
                  "inner.zip": make_zip({"x.txt": b"hi"})})
    r = upload(client, "bundle.zip", z)
    kids = {c["name"]: c for c in r["children"]}
    assert kids["a.png"]["status"] == "created" and kids["b.png"]["status"] == "created"
    assert kids["evil.txt"]["status"] == "created"  # basename only; nothing written outside the store
    assert kids["inner.zip"]["children"][0]["status"] == "created"  # nested archives expand (depth <= 3)
    root = client.container.config.data_root
    assert not (root.parent / "evil.txt").exists() and not (root.parent.parent / "evil.txt").exists()


def test_zip_bomb_guards(client):
    bomb = make_zip({"zeros.txt": b"a" * 5_000_000})  # ~5 MB of 'a' deflates to a few KB
    r = upload(client, "bomb.zip", bomb)
    assert r["children"][0]["status"] == "rejected" and "ratio" in r["children"][0]["reason"]
    client.container.docs.limits = Limits(zip_max_entries=2)
    many = make_zip({f"{i}.txt": f"doc {i}".encode() for i in range(5)})
    assert upload(client, "many.zip", many)["status"] == "rejected"


def test_import_paths_folder(client, tmp_path):
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "a.png").write_bytes(make_png("red"))
    (tmp_path / "in" / "b.txt").write_bytes(b"hello")
    res = client.post("/api/v1/documents/import-paths", json={"paths": [str(tmp_path / "in"), "/nope"]}).json()["results"]
    assert [r["status"] for r in res] == ["created", "created", "rejected"]


def test_list_filters_and_cursor(client):
    for i in range(5):
        upload(client, f"doc{i}.txt", f"content {i}".encode())
    page1 = client.get("/api/v1/documents?limit=2").json()
    assert len(page1["items"]) == 2 and page1["next_cursor"]
    page2 = client.get(f"/api/v1/documents?limit=2&cursor={page1['next_cursor']}").json()
    assert {d["id"] for d in page1["items"]}.isdisjoint(d["id"] for d in page2["items"])
    assert len(client.get("/api/v1/documents?q=doc3").json()["items"]) == 1
    assert client.get("/api/v1/documents?q=%25").json()["items"] == []  # LIKE wildcards are escaped


def test_import_200_mixed_files_without_crash(client):
    kinds = [lambda i: make_png(f"#{i % 256:02x}{i % 7:02x}00"), lambda i: f"text {i}".encode(),
             lambda i: make_pdf(1)[:-1] + bytes([i % 250]) + b"\n", lambda i: b"garbage\x00" + bytes([i % 250])]
    files = [("files", (f"f{i}.bin", kinds[i % 4](i))) for i in range(200)]
    res = client.post("/api/v1/documents/import", files=files).json()["results"]
    assert len(res) == 200
    assert {r["status"] for r in res} <= {"created", "duplicate", "rejected"}
    run_workers(client)
    assert wait_for(lambda: not client.container.queue.list("queued") and not client.container.queue.list("running"),
                    timeout=60)

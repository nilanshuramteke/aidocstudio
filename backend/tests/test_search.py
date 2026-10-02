import json

import pytest
from fastapi.testclient import TestClient

from adstudio.api.app import create_app
from adstudio.core import container as cmod
from adstudio.core.fakes import FakeLLM, FakeOCR
from adstudio.core.interfaces import OCRWord, ProviderHealth
from adstudio.core.security import COOKIE_NAME
from adstudio.search.chunker import chunk_page
from adstudio.search.query_parser import parse_query
from adstudio.search.service import MARK_END, MARK_START, fts_query

from .test_documents import run_workers, settle, upload
from .test_extraction import GOOD_GSTIN, INVOICE_TEXT, pdf_with_lines
from .test_queue import wait_for


# ── chunker ───────────────────────────────────────────────────
def test_chunker_page_bounded_overlap_and_bbox():
    text = " ".join(f"w{i}" for i in range(600))
    words = [OCRWord(f"w{i}", i / 1000, 0.1, 0.001, 0.02, 0.9) for i in range(600)]
    cs = chunk_page(3, text, words, target=260, overlap=40)
    assert [c.page_no for c in cs] == [3] * len(cs) and [c.ord for c in cs] == list(range(len(cs)))
    assert cs[0].text.split()[-40:] == cs[1].text.split()[:40]  # overlap
    assert cs[-1].text.endswith("w599") and all(c.bbox and 0 <= c.bbox[0] <= 1 for c in cs)
    assert text[cs[1].char_start:cs[1].char_end] == cs[1].text
    assert chunk_page(1, "   \n ", None) == []
    assert len(chunk_page(1, "short page", None)) == 1


# ── query parsing ─────────────────────────────────────────────
@pytest.mark.parametrize("q,filters,rest", [
    ("invoices in 2026", {"date_from": "2026-01-01", "date_to": "2026-12-31"}, "invoices"),
    ("rent in March 2026", {"date_from": "2026-03-01", "date_to": "2026-03-31"}, "rent"),
    ("total above ₹50,000", {"amount_min": 50000.0}, "total"),
    ("over 2 lakh", {"amount_min": 200000.0}, ""),
    ("below 1.5k receipts", {"amount_max": 1500.0}, "receipts"),
    ("from ABC Private Limited above 100", {"entity": "ABC Private Limited", "amount_min": 100.0}, ""),
    ("type:invoice before:2026-03-01 laptop", {"type": "invoice", "date_to": "2026-03-01"}, "laptop"),
    ("review:needs", {"review": "needs_review"}, ""),
    ("plain words only", {}, "plain words only"),
])
def test_query_parser(q, filters, rest):
    f, r = parse_query(q)
    assert f == filters and r == rest


def test_fts_query_is_sanitized():
    assert fts_query('hello "world') == '"hello" "world"*'
    assert fts_query("NEAR(a b) OR * -x") == '"NEAR" "a" "b" "OR" "x"*'  # operators become plain quoted terms
    assert fts_query("!!!") is None
    assert fts_query("a b", mode_or=True) == '"a" OR "b"*'


# ── helpers ───────────────────────────────────────────────────
def drain(client, timeout=30):
    q = client.container.queue
    assert wait_for(lambda: not q.list("queued") and not q.list("running"), timeout), "jobs did not drain"


def search(client, q, **kw):
    return client.post("/api/v1/search", json={"q": q, **kw}).json()


def titles(res):
    return [r["document"]["title"] for r in res["results"]]


@pytest.fixture
def corpus(client):
    ids = {}
    ids["inv"] = upload(client, "abc-invoice.pdf", pdf_with_lines(INVOICE_TEXT))["document_id"]
    ids["lease"] = upload(client, "lease.txt", b"This lease agreement covers the rental of the warehouse. The tenant shall "
                          b"pay monthly rent on the first day. Termination requires ninety days notice. "
                          b"This agreement is governed by the laws of Maharashtra. The parties hereinafter agree.")["document_id"]
    ids["menu"] = upload(client, "menu.txt", b"Receipt thank you for dining. Paneer butter masala, naan, "
                         b"and mango lassi were served. Cash payment received. Total 1,250.00")["document_id"]
    run_workers(client)
    for d in ids.values():
        settle(client, d)
    drain(client)
    return ids


# ── keyword + filters + fields ────────────────────────────────
def test_keyword_search_snippets_and_early_availability(client, corpus):
    r = search(client, "warehouse rental")
    assert titles(r)[0] == "lease"
    sn = r["results"][0]["snippets"][0]
    assert MARK_START in sn["text"] and MARK_END in sn["text"] and sn["page_no"] == 1
    assert "warehouse" in sn["text"].replace(MARK_START, "").replace(MARK_END, "").lower()


def test_prefix_matching_and_or_fallback(client, corpus):
    assert titles(search(client, "warehou"))[0] == "lease"  # type-ahead prefix on the last token
    assert "lease" in titles(search(client, "warehouse zzzzqqq"))  # AND finds nothing -> OR fallback


def test_field_values_are_searchable_with_hits(client, corpus):
    r = search(client, "Acme Traders")
    top = r["results"][0]
    assert top["document"]["title"] == "abc-invoice" and any(h["key"] == "customer" for h in top["field_hits"])
    assert top["summary"]["invoice_number"] == "INV-20491"


def test_filters_parsed_and_applied(client, corpus):
    r = search(client, "type:invoice")
    assert titles(r) == ["abc-invoice"] and r["parsed_filters"] == {"type": "invoice"}
    assert titles(search(client, "invoice total above 50000")) == ["abc-invoice"]
    assert search(client, "invoice total above 60000")["results"] == []
    assert titles(search(client, "invoice in 2026")) == ["abc-invoice"]
    assert search(client, "invoice in 2024")["results"] == []
    assert titles(search(client, "from ABC")) == ["abc-invoice"]  # vendor field LIKE
    # filter-only query (no text) lists matches, newest first
    assert titles(search(client, "type:receipt")) == ["menu"]
    # explicit filters override chips; parse=false treats q as plain text
    r2 = search(client, "invoice", filters={"type": "contract"}, parse=False)
    assert r2["results"] == []


def test_bare_type_word_becomes_a_type_chip(client, corpus):
    r = search(client, "invoices above 50,000")
    assert titles(r) == ["abc-invoice"] and r["parsed_filters"]["type"] == "invoice" and r["text"] == ""
    assert search(client, "invoices")["parsed_filters"] == {"type": "invoice"}
    assert "type" not in search(client, "invoice")["parsed_filters"]  # singular on its own stays a plain keyword


def test_soft_deleted_documents_leave_search(client, corpus):
    assert titles(search(client, "warehouse"))
    client.delete(f"/api/v1/documents/{corpus['lease']}")
    assert search(client, "warehouse")["results"] == []


def test_reprocess_replaces_chunks_without_duplicates(client, corpus):
    client.post(f"/api/v1/documents/{corpus['lease']}/reprocess", json={"from_stage": "text"})
    settle(client, corpus["lease"])
    drain(client)
    with client.container.db.read() as c:
        n = c.execute("SELECT COUNT(*) FROM chunks WHERE document_id=?", (corpus["lease"],)).fetchone()[0]
        fts = c.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'warehouse'").fetchone()[0]
    assert n == 1 and fts == 1


def test_history_suggest_saved(client, corpus):
    search(client, "warehouse")
    search(client, "warehouse")  # consecutive duplicates collapse
    assert [h["q"] for h in client.get("/api/v1/search/history").json()["items"]] == ["warehouse"]
    sug = client.get("/api/v1/search/suggest?q=ware").json()["items"]
    assert {"kind": "recent", "text": "warehouse"} in sug
    assert any(s["kind"] == "type" for s in client.get("/api/v1/search/suggest?q=inv").json()["items"])
    saved = client.post("/api/v1/search/saved", json={"name": "Big invoices", "query": {"q": "type:invoice above 50000"}}).json()
    assert client.get("/api/v1/search/saved").json()["items"][0]["name"] == "Big invoices"
    assert client.delete(f"/api/v1/search/saved/{saved['id']}").status_code == 200
    assert client.delete(f"/api/v1/search/saved/{saved['id']}").status_code == 404
    client.delete("/api/v1/search/history")
    assert client.get("/api/v1/search/history").json()["items"] == []


def test_bad_inputs(client):
    assert client.post("/api/v1/search", json={"q": 5}).status_code == 400
    assert client.post("/api/v1/search", json={"q": "x", "mode": "psychic"}).json()["code"] == "invalid_mode"
    assert search(client, "")["results"] == []  # empty corpus, empty query


# ── semantic / hybrid ─────────────────────────────────────────
CONCEPTS = [("vehicle", "car", "automobile", "truck"), ("money", "payment", "invoice", "rent", "pay"),
            ("food", "paneer", "naan", "dining", "lassi"), ("legal", "lease", "agreement", "tenant", "laws")]


class ConceptEmbedding:
    """Maps words to concept axes so 'automobile' lands near 'vehicle' without any shared keyword."""

    def __init__(self, model_id="concept-v1", flip=False):
        self.model_id, self.dim, self.flip = model_id, len(CONCEPTS), flip
        self.calls = 0

    def embed(self, texts, *, kind):
        self.calls += 1
        out = []
        for t in texts:
            low = t.lower()
            v = [float(sum(low.count(w) for w in group)) for group in CONCEPTS]
            n = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / n for x in (reversed(v) if self.flip else v)])
        return out

    def health(self):
        return ProviderHealth(True, self.model_id)


from adstudio.storage.db import vec_available

# python.org builds of Python (used by setup-python on macOS) cannot load SQLite extensions.
requires_vec = pytest.mark.skipif(not vec_available(), reason="sqlite-vec cannot be loaded (Python built without extension loading)")


@pytest.fixture
def sem_client(config):
    emb = ConceptEmbedding()
    c = cmod.build_container(config, ocr=FakeOCR(), llm=FakeLLM(), embedding=emb, start_workers=False)
    with TestClient(create_app(c), base_url="http://127.0.0.1") as tc:
        tc.cookies.set(COOKIE_NAME, c.auth.session_token)
        tc.container, tc.emb = c, emb
        yield tc


@requires_vec
def test_semantic_finds_meaning_without_shared_keywords(sem_client):
    car = upload(sem_client, "garage.txt", b"The truck and the car were parked inside the garage overnight.")["document_id"]
    food = upload(sem_client, "dinner.txt", b"We ate paneer and naan while dining out with friends.")["document_id"]
    run_workers(sem_client)
    drain(sem_client)
    assert titles(search(sem_client, "automobile", mode="keyword")) == []  # no lexical overlap at all
    r = search(sem_client, "automobile", mode="meaning")
    assert r["semantic"]["available"] and titles(r)[0] == "garage"
    hybrid = search(sem_client, "automobile")  # best = hybrid
    assert titles(hybrid)[0] == "garage"
    assert sem_client.container.search.vectors.count() >= 2
    # metadata prefilter is applied before KNN
    assert search(sem_client, "automobile type:contract")["results"] == []
    assert food


@requires_vec
def test_hybrid_ranks_documents_found_by_both_lists_first(sem_client):
    upload(sem_client, "a.txt", b"rent payment for the lease agreement")
    upload(sem_client, "b.txt", b"rent was mentioned once in a long story about gardens and weather and trees " * 3)
    run_workers(sem_client)
    drain(sem_client)
    r = search(sem_client, "rent payment")
    assert titles(r)[0] == "a"


@requires_vec
def test_embedding_model_change_triggers_reembed(sem_client):
    upload(sem_client, "one.txt", b"the car was fast")
    upload(sem_client, "two.txt", b"payment due on the lease")
    run_workers(sem_client)
    drain(sem_client)
    svc = sem_client.container.search
    assert svc.vectors.active_model() == ("concept-v1", 4) and svc.vectors.count() == 2
    svc.embedding = ConceptEmbedding("concept-v2", flip=True)  # user switched model
    with sem_client.container.db.read() as c:
        first = c.execute("SELECT id FROM documents ORDER BY id LIMIT 1").fetchone()[0]
    sem_client.container.queue.enqueue("stage:embed", document_id=first)
    drain(sem_client)
    assert svc.vectors.active_model() == ("concept-v2", 4)
    assert svc.vectors.count() == 2  # the OTHER document was re-embedded too, no stale vectors


def test_no_embedding_model_degrades_to_keyword(client, corpus):
    r = search(client, "warehouse")
    assert r["semantic"] == {"available": False, "reason": "no embedding model configured"}
    assert titles(r)[0] == "lease"
    assert search(client, "warehouse", mode="meaning")["results"] == []


def test_reindex_endpoint(client, corpus):
    with client.container.db.write() as c:
        c.execute("DELETE FROM chunks")
    assert search(client, "warehouse")["results"] == []
    assert client.post("/api/v1/search/reindex").json()["queued"] == 3
    drain(client)
    assert titles(search(client, "warehouse"))[0] == "lease"


def test_recall_at_10_on_synthetic_queries(client):
    topics = {"solar": "solar panel installation warranty inverter", "visa": "visa application passport embassy interview",
              "mortgage": "mortgage loan interest rate amortization schedule", "yoga": "yoga class schedule instructor mat",
              "tax": "income tax return deduction section assessment", "lease": "lease tenant landlord deposit rent",
              "audit": "audit report internal controls findings", "cargo": "cargo shipment container port customs"}
    ids = {}
    for name, words in topics.items():
        ids[name] = upload(client, f"{name}.txt", (words + f" unique filler {name} text for document").encode())["document_id"]
    for i in range(20):  # distractors
        upload(client, f"noise{i}.txt", f"random unrelated notes number {i} about gardens and weather".encode())
    run_workers(client)
    drain(client, 60)
    hits = 0
    for name, words in topics.items():
        q = " ".join(words.split()[1:3])  # two words from the middle of the doc
        r = search(client, q, limit=10)
        hits += any(x["document"]["id"] == ids[name] for x in r["results"])
    assert hits / len(topics) >= 0.9  # recall@10


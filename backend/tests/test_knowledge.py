import json
import random
import time

import pytest

from adstudio.knowledge.service import KnowledgeService, normalize_name, split_parties
from adstudio.storage.db import Database

from .test_chat import ScriptedLLM, make_client
from .test_documents import run_workers, settle, upload
from adstudio.validation.builtin import gstin_check_char

from .test_extraction import GOOD_GSTIN, INVOICE_TEXT, pdf_with_lines
from .test_queue import wait_for
from .test_search import drain


# ── normalization & resolution ────────────────────────────────
@pytest.mark.parametrize("raw,norm", [
    ("ABC Private Limited", "abc"), ("ABC Pvt. Ltd.", "abc"), ("M/s ABC Pvt Ltd", "abc"), ("The Acme Trading Co.", "acme trading"),
    ("Tata & Sons Ltd", "tata and sons"), ("Limited", "limited"), ("Infosys Technologies LLP", "infosys technologies"), ("  Zeta   Corp ", "zeta"),
])
def test_normalize_name(raw, norm):
    assert normalize_name(raw) == norm


def test_split_parties():
    assert split_parties("Alpha Corp and Beta LLC") == ["Alpha Corp", "Beta LLC"]
    assert split_parties("Alpha Corp; Beta LLC & Gamma Inc") == ["Alpha Corp", "Beta LLC", "Gamma Inc"]


@pytest.fixture
def kn(tmp_path):
    db = Database(tmp_path / "k.sqlite")
    db.migrate()
    yield KnowledgeService(db)
    db.close()


def test_resolution_precision_on_org_name_variants(kn):
    same = ["ABC Private Limited", "ABC Pvt. Ltd.", "A.B.C. Pvt Ltd", "ABC PVT LTD", "M/s ABC Pvt Ltd", "abc"]
    ids = {kn.resolve("organization", n) for n in same}
    assert len(ids) == 1, ids  # every surface form of one company resolves to one entity
    distinct = ["ABC Traders", "ABC Textiles", "ABD Pvt Ltd", "Acme Trading", "Acme Training Institute", "Infosys", "Infosis Labs"]
    resolved = [kn.resolve("organization", n) for n in distinct]
    assert len(set(resolved)) == len(distinct) and not (set(resolved) & ids)  # similar-looking names stay separate
    assert kn.resolve("organization", "Acme Trading Co") == kn.resolve("organization", "Acme Trading")
    assert kn.resolve("organization", "Reliance Industires Limited") == kn.resolve("organization", "Reliance Industries Limited")  # typo
    assert kn.resolve("person", "ABC") not in ids  # kinds never mix
    assert kn.resolve("organization", "Xi") != kn.resolve("organization", "Xo")  # too short for fuzzy matching


def test_strong_identifier_beats_name_and_aliases_accumulate(kn):
    a = kn.resolve("organization", "Quantum Works Pvt Ltd", {"gstin": "27AAAAA0000A1Z5"})
    b = kn.resolve("organization", "QW Industries", {"gstin": "27AAAAA0000A1Z5"})  # totally different name, same GSTIN
    assert a == b
    e = kn.get(a)
    assert set(e["aliases"]) == {"Quantum Works Pvt Ltd", "QW Industries"} and e["identifiers"] == {"gstin": "27AAAAA0000A1Z5"}
    assert kn.resolve("organization", "Quantum Works") == a  # now also found by name
    assert kn.search("qw")[0]["id"] == a and kn.search("quantum", kind="person") == []


def test_merge_and_rename(kn):
    a, b, c = (kn.resolve("organization", n) for n in ("Alpha Foods", "Zenith Mills", "Omega Steel"))
    with kn.db.write() as conn:
        for s, d in ((a, b), (b, c), (c, a)):  # a triangle: merging a into b must not leave a self-loop
            conn.execute("INSERT INTO relationships(id,src_entity_id,dst_entity_id,kind,created_at) VALUES(?,?,?,?,?)",
                         (f"tri-{s}-{d}", s, d, "supplies", "t"))
    p = kn.resolve("person", "Ravi Kumar")
    with pytest.raises(Exception) as e:
        kn.merge(a, p)
    assert e.value.code == "kind_mismatch"
    merged = kn.merge(a, b)
    assert "Alpha Foods" in merged["aliases"] and kn.search("alpha")[0]["id"] == b
    assert all(r["other_id"] != b for r in merged["relationships"])  # no self-relationship
    assert kn.rename(b, "Zenith Foods & Mills")["name"] == "Zenith Foods & Mills"
    with pytest.raises(Exception):
        kn.merge(b, b)


# ── graph (recursive CTE) ─────────────────────────────────────
def chain(kn, n=6):
    ids = [kn.resolve("organization", f"Node{chr(65 + i)}zzz") for i in range(n)]
    with kn.db.write() as c:
        for i in range(n - 1):
            c.execute("INSERT INTO relationships(id,src_entity_id,dst_entity_id,kind,created_at) VALUES(?,?,?,?,?)",
                      (f"r{i}", ids[i], ids[i + 1], "supplies", "t"))
        c.execute("INSERT INTO relationships(id,src_entity_id,dst_entity_id,kind,created_at) VALUES('loop',?,?,?,?)", (ids[3], ids[0], "supplies", "t"))
    return ids


def test_graph_depth_limits_directions_and_cycles(kn):
    ids = chain(kn)
    names = lambda d, start=0: {n["name"]: n["depth"] for n in kn.graph(ids[start], d)["nodes"]}  # noqa: E731
    assert len(names(1)) == 3  # NodeA, its successor B and (via the loop edge, incoming) NodeD
    assert max(names(3).values()) == 3 and len(names(3)) == 6  # A, B, D(loop) / C, E / F
    assert len(kn.graph(ids[0], 99)["nodes"]) == len(kn.graph(ids[0], 3)["nodes"])  # depth is clamped to 3
    mid = kn.graph(ids[2], 1)
    assert {n["name"] for n in mid["nodes"]} == {"NodeCzzz", "NodeBzzz", "NodeDzzz"}  # walks both directions
    assert {(e["src"], e["dst"]) for e in mid["edges"]} == {(ids[1], ids[2]), (ids[2], ids[3])}
    assert kn.graph(ids[0], 3)["truncated"] is False


def test_graph_scales_to_many_entities(kn):
    rng = random.Random(3)
    n = 10_000
    now = "t"
    with kn.db.write() as c:
        c.executemany("INSERT INTO entities(id,kind,canonical_name,norm,nk,created_at) VALUES(?,?,?,?,?,?)",
                      [(f"e{i}", "organization", f"E{i}", f"e{i}", f"e{i}", now) for i in range(n)])
        c.executemany("INSERT OR IGNORE INTO relationships(id,src_entity_id,dst_entity_id,kind,created_at) VALUES(?,?,?,?,?)",
                      [(f"r{i}", f"e{rng.randrange(n)}", f"e{int(n * rng.random() ** 2)}", "supplies", now) for i in range(n * 3)])
    t = time.perf_counter()
    g = kn.graph("e0", 3)
    assert time.perf_counter() - t < 2.0 and len(g["nodes"]) <= 500 and g["nodes"][0]["depth"] == 0


# ── end to end from documents ─────────────────────────────────
def gstin_of(vendor: str) -> str:
    """A valid GSTIN that is stable per company (all spellings of one name share it), distinct across companies."""
    h = abs(hash(("gstin", normalize_name(vendor)))) % 26 ** 5
    letters = "".join(chr(65 + (h // 26 ** i) % 26) for i in range(5))
    first14 = f"27{letters}1234F1Z"
    return first14 + gstin_check_char(first14)


def inv(vendor, number, customer="Acme Traders", total="54,280.00"):
    text = (INVOICE_TEXT.replace("ABC Private Limited", vendor).replace("INV-20491", number).replace("Acme Traders", customer)
            .replace("54,280.00", total).replace(GOOD_GSTIN, gstin_of(vendor)))
    return pdf_with_lines(text)


def go(client, *docs):
    ids = [upload(client, name, data)["document_id"] for name, data in docs]
    run_workers(client)
    for i in ids:
        settle(client, i)
    drain(client)
    return ids


def test_invoices_link_vendors_customers_and_relationships(client):
    a, b, c = go(client, ("a.pdf", inv("ABC Private Limited", "INV-1")), ("b.pdf", inv("ABC Pvt. Ltd.", "INV-2")),
                 ("c.pdf", inv("Zenith Mills Limited", "INV-3", customer="Acme Trading Co")))
    ents = client.get("/api/v1/entities").json()["items"]
    names = {e["name"] for e in ents}
    # "ABC Pvt. Ltd." folded into the first; "Acme Traders" vs "Acme Trading Co" stay separate (conservative: 0.88 < 0.92)
    assert len(ents) == 4 and names == {"ABC Private Limited", "Zenith Mills Limited", "Acme Traders", "Acme Trading Co"}
    abc = next(e for e in ents if e["name"].startswith("ABC"))
    page = client.get(f"/api/v1/entities/{abc['id']}").json()
    assert {d["id"] for d in page["documents"]} == {a, b} and {d["role"] for d in page["documents"]} == {"vendor"}
    assert set(page["aliases"]) == {"ABC Private Limited", "ABC Pvt. Ltd."}
    assert page["relationships"][0]["kind"] == "supplies" and page["relationships"][0]["direction"] == "out"
    assert page["relationships"][0]["other_name"] == "Acme Traders" and page["relationships"][0]["evidence_documents"] == 2
    g = client.get(f"/api/v1/entities/{abc['id']}/graph?depth=2").json()
    assert {n["name"] for n in g["nodes"]} == {"ABC Private Limited", "Acme Traders"} and g["edges"][0]["evidence"] == 2
    rel = client.get(f"/api/v1/documents/{a}/related").json()
    assert [x["id"] for x in rel["related"]] == [b] and rel["related"][0]["via"].count(",") == 1  # shares vendor AND customer
    assert client.get("/api/v1/entities?q=zenith").json()["items"][0]["documents"] == 1


def test_duplicate_and_supersedes_detection(client):
    base = inv("ABC Private Limited", "INV-9")
    a, b = go(client, ("orig.pdf", base), ("copy.pdf", base + b"\n% resaved with different bytes"))  # same content, new sha
    dup = client.get(f"/api/v1/documents/{a}/related").json()["links"]
    assert [(l["kind"], l["other_id"]) for l in dup] == [("duplicate", b)]
    (c,) = go(client, ("fixed.pdf", inv("ABC Private Limited", "INV-9", total="60,000.00", )))
    links = client.get(f"/api/v1/documents/{c}/related").json()["links"]
    assert {(l["kind"], l["direction"]) for l in links} == {("supersedes", "out")}  # the newer, different-amount copy supersedes
    older = client.get(f"/api/v1/documents/{a}/related").json()["links"]
    assert ("supersedes", "in") in {(l["kind"], l["direction"]) for l in older}


def test_edits_deletes_and_restores_keep_the_graph_current(client):
    a, = go(client, ("a.pdf", inv("ABC Private Limited", "INV-1")))
    abc = client.get("/api/v1/entities?q=abc").json()["items"][0]["id"]
    client.patch(f"/api/v1/documents/{a}/fields/vendor", json={"value": "Globex Corporation"})  # human correction
    assert wait_for(lambda: client.get("/api/v1/entities?q=globex").json()["items"], 10)
    drain(client)
    roles = {d["id"] for d in client.get(f"/api/v1/entities/{abc}").json()["documents"]}
    assert a not in roles  # no longer linked to the old vendor
    client.patch(f"/api/v1/documents/{a}/fields/customer", json={"status": "rejected"})
    drain(client)
    assert client.get(f"/api/v1/documents/{a}/related").json()["entities"] == [
        {"id": client.get("/api/v1/entities?q=globex").json()["items"][0]["id"], "name": "Globex Corporation", "kind": "organization", "role": "vendor"}]
    client.delete(f"/api/v1/documents/{a}")
    assert wait_for(lambda: client.get("/api/v1/entities?q=globex").json()["items"][0]["documents"] == 0, 10)
    client.post(f"/api/v1/documents/{a}/restore")
    assert wait_for(lambda: client.get("/api/v1/entities?q=globex").json()["items"][0]["documents"] == 1, 10)
    assert client.get("/api/v1/entities/nope").status_code == 404


def test_unverified_values_do_not_pollute_the_graph(client):
    (a,) = go(client, ("a.pdf", inv("ABC Private Limited", "INV-1", total="60,000.00")))  # totals mismatch -> fields flagged
    with client.container.db.read() as c:
        flagged = {r["key"] for r in c.execute("SELECT key FROM extracted_fields WHERE document_id=? AND status='needs_review'", (a,))}
    assert "total" in flagged and "vendor" not in flagged
    assert client.get(f"/api/v1/documents/{a}/related").json()["entities"]  # verified fields still link
    with client.container.db.write() as c:
        c.execute("UPDATE extracted_fields SET status='needs_review' WHERE document_id=? AND key='customer'", (a,))
    client.container.knowledge.link_document(a)
    assert {e["role"] for e in client.get(f"/api/v1/documents/{a}/related").json()["entities"]} == {"vendor"}


def contract_llm(messages, schema):
    text = messages[0]["content"]
    if "classify" in text.lower()[:60]:
        return json.dumps({"type": "Contract", "confidence": 0.95})
    return json.dumps({"parties": "Alpha Corp and Beta LLC"} if "Alpha Corp" in text else {})


def test_contract_parties_and_supersession(config):
    tc = make_client(config, ScriptedLLM(fn=contract_llm))
    try:
        v1 = "This Agreement is made between Alpha Corp and Beta LLC (the parties). Effective Date: 01/01/2025. Termination requires notice. Governing law applies."
        v2 = v1.replace("01/01/2025", "01/01/2026")
        a = upload(tc, "c1.txt", v1.encode())["document_id"]
        b = upload(tc, "c2.txt", v2.encode())["document_id"]
        run_workers(tc)
        for d in (a, b):
            settle(tc, d)
        drain(tc)
        assert tc.get("/api/v1/entities").json()["items"] == []  # LLM-read parties are unverified: not linked yet
        for d in (a, b):
            assert tc.patch(f"/api/v1/documents/{d}/fields/parties", json={"status": "accepted"}).status_code == 200
        drain(tc)
        ents = {e["name"] for e in tc.get("/api/v1/entities").json()["items"]}
        assert ents == {"Alpha Corp", "Beta LLC"}
        alpha = tc.get("/api/v1/entities?q=alpha").json()["items"][0]["id"]
        rel = tc.get(f"/api/v1/entities/{alpha}").json()["relationships"]
        assert rel[0]["kind"] == "party_to" and rel[0]["evidence_documents"] == 2
        links = tc.get(f"/api/v1/documents/{b}/related").json()["links"]
        assert [(l["kind"], l["direction"], l["other_id"]) for l in links] == [("supersedes", "out", a)]  # later effective date wins
    finally:
        tc.__exit__(None, None, None)

import pytest

from adstudio.search.query_parser import parse_query

from .test_chat import LEASE, MENU, ScriptedLLM, ask, make_client, of
from .test_documents import run_workers, settle, upload
from .test_extraction import INVOICE_TEXT, pdf_with_lines
from .test_search import drain, search, titles
from .test_queue import wait_for


@pytest.fixture
def docs(client):
    ids = {"inv": upload(client, "inv.pdf", pdf_with_lines(INVOICE_TEXT))["document_id"],
           "lease": upload(client, "lease.txt", LEASE)["document_id"],
           "menu": upload(client, "menu.txt", MENU)["document_id"]}
    run_workers(client)
    for d in ids.values():
        settle(client, d)
    drain(client)
    return ids


def test_tag_parser_token():
    assert parse_query("tag:urgent invoices") == ({"tag": "urgent"}, "invoices")


def test_tags_crud_case_insensitive_and_counts(client, docs):
    t = client.post("/api/v1/tags", json={"name": "Finance", "color": "#3b5bdb"}).json()
    assert client.post("/api/v1/tags", json={"name": "finance"}).status_code == 409  # case-insensitive unique
    assert client.post("/api/v1/tags", json={"name": "  "}).json()["code"] == "invalid_name"
    r = client.post(f"/api/v1/documents/{docs['inv']}/tags", json={"add": ["finance", "Q1 2026"]}).json()
    assert r["tags"] == ["Finance", "Q1 2026"]  # "finance" reused the existing (differently cased) tag
    listing = {x["name"]: x["documents"] for x in client.get("/api/v1/tags").json()["items"]}
    assert listing == {"Finance": 1, "Q1 2026": 1}
    assert client.patch(f"/api/v1/tags/{t['id']}", json={"name": "Money"}).json()["name"] == "Money"
    assert client.get(f"/api/v1/documents/{docs['inv']}").json()["tags"] == ["Money", "Q1 2026"]
    client.post(f"/api/v1/documents/{docs['inv']}/tags", json={"remove": ["money"]})
    assert client.get(f"/api/v1/documents/{docs['inv']}").json()["tags"] == ["Q1 2026"]
    assert client.delete(f"/api/v1/tags/{t['id']}").status_code == 200
    assert client.delete(f"/api/v1/tags/{t['id']}").status_code == 404


def test_tag_filters_in_list_and_search(client, docs):
    client.post("/api/v1/documents/bulk", json={"ids": [docs["inv"], docs["lease"]], "action": "tag", "params": {"tags": ["review-me"]}})
    items = client.get("/api/v1/documents?tag=review-me").json()["items"]
    assert {i["title"] for i in items} == {"inv", "lease"}
    assert set(titles(search(client, "tag:review-me"))) == {"inv", "lease"}
    assert titles(search(client, "warehouse tag:review-me")) == ["lease"]
    assert search(client, "paneer tag:review-me")["results"] == []
    client.post("/api/v1/documents/bulk", json={"ids": [docs["lease"]], "action": "untag", "params": {"tags": ["review-me"]}})
    assert titles(search(client, "tag:review-me")) == ["inv"]
    assert client.post("/api/v1/documents/bulk", json={"ids": [docs["inv"]], "action": "tag", "params": {}}).json()["code"] == "invalid_body"


def test_manual_collection(client, docs):
    c = client.post("/api/v1/collections", json={"name": "Finance 2026"}).json()
    assert c["kind"] == "manual" and c["documents"] == 0
    assert client.post("/api/v1/collections", json={"name": "finance 2026"}).status_code == 409
    assert client.post(f"/api/v1/collections/{c['id']}/documents", json={"document_ids": [docs["inv"], docs["lease"], "nope"]}).json() == {"added": 2}
    assert client.post(f"/api/v1/collections/{c['id']}/documents", json={"document_ids": [docs["inv"]]}).json() == {"added": 0}  # idempotent
    items = client.get(f"/api/v1/collections/{c['id']}/documents").json()["items"]
    assert {i["title"] for i in items} == {"inv", "lease"}
    assert {i["title"] for i in client.get(f"/api/v1/documents?collection={c['id']}").json()["items"]} == {"inv", "lease"}
    client.delete(f"/api/v1/documents/{docs['lease']}")  # soft-deleted documents drop out
    assert client.get("/api/v1/collections").json()["items"][0]["documents"] == 1
    assert client.request("DELETE", f"/api/v1/collections/{c['id']}/documents", json={"document_ids": [docs["inv"]]}).json() == {"removed": 1}
    r = client.post("/api/v1/documents/bulk", json={"ids": [docs["menu"]], "action": "add_to_collection", "params": {"collection_id": c["id"]}})
    assert r.json()["changed"] == 1
    assert client.delete(f"/api/v1/collections/{c['id']}").status_code == 200


def test_smart_collection_is_a_live_saved_search(client, docs):
    sc = client.post("/api/v1/collections", json={"name": "Invoices", "kind": "smart", "query": {"q": "type:invoice"}}).json()
    assert sc["documents"] == 1
    assert client.post("/api/v1/collections", json={"name": "bad", "kind": "smart"}).json()["code"] == "invalid_query"
    assert client.post(f"/api/v1/collections/{sc['id']}/documents", json={"document_ids": [docs["menu"]]}).json()["code"] == "smart_collection"
    upload(client, "inv2.pdf", pdf_with_lines(INVOICE_TEXT.replace("INV-20491", "INV-777")))
    run_workers(client)
    drain(client)
    assert wait_for(lambda: client.get("/api/v1/collections").json()["items"][0]["documents"] == 2)  # picks up new matches
    assert {i["title"] for i in client.get(f"/api/v1/collections/{sc['id']}/documents").json()["items"]} == {"inv", "inv2"}


def test_chat_scope_by_collection(config):
    llm = ScriptedLLM(reply="NOT_FOUND")
    tc = make_client(config, llm)
    try:
        a = upload(tc, "lease.txt", LEASE)["document_id"]
        b = upload(tc, "menu.txt", MENU)["document_id"]
        run_workers(tc)
        for d in (a, b):
            settle(tc, d)
        drain(tc)
        col = tc.post("/api/v1/collections", json={"name": "Legal"}).json()
        tc.post(f"/api/v1/collections/{col['id']}/documents", json={"document_ids": [a]})
        ev = ask(tc, "warehouse rental notice", scope={"collection_id": col["id"]})
        assert {s["document_id"] for s in of(ev, "sources")[0]["sources"]} == {a}
        ev2 = ask(tc, "paneer naan lassi", scope={"collection_id": col["id"]})
        assert of(ev2, "done")[0]["not_found"]  # the menu is outside the collection
    finally:
        tc.__exit__(None, None, None)

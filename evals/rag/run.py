"""RAG eval against a REAL local model (Ollama): `python evals/rag/run.py [model]`.

Synthetic corpus, gold questions (answerable + unanswerable). Metrics: answer contains gold fact, citation points at the
right document, abstention on unanswerable, share of answers carrying post-check warnings. Skips cleanly if Ollama is down.
Thresholds are set from a baseline run (BLUEPRINT section 25), not invented.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

from adstudio.ai.providers.ollama import OllamaLLM
from adstudio.core.config import Config
from adstudio.core.container import build_container
from adstudio.core.fakes import FakeOCR

DOCS = {
    "lease": "Lease agreement. The tenant shall pay monthly rent of 45,000 on the first day. Termination requires ninety days "
             "written notice. This agreement is governed by the laws of Maharashtra.",
    "warranty": "Warranty terms. The product is covered for two years from purchase. Batteries are covered for six months. "
                "Damage caused by water is not covered.",
    "invoice": "Tax Invoice. Invoice No INV-20491. Vendor ABC Private Limited. Total amount payable 54,280.00 INR. "
               "Payment is due within thirty days.",
    "policy": "Leave policy. Employees receive twenty four days of paid leave per year. Unused leave up to ten days may be "
              "carried forward. Sick leave requires a medical certificate after three days.",
}
ANSWERABLE = [("How much notice is needed to end the lease?", "ninety", "lease"),
              ("How long is the product warranty?", "two years", "warranty"),
              ("What is the invoice total?", "54,280", "invoice"),
              ("How many days of paid leave do employees get?", "twenty four", "policy"),
              ("Which law governs the lease?", "maharashtra", "lease")]
UNANSWERABLE = ["What is the CEO's salary?", "When was the company founded?", "What is the penalty for late rent?"]


def main() -> None:
    model = sys.argv[1] if len(sys.argv) > 1 else "llama3.1:8b"
    llm = OllamaLLM(model=model)
    h = llm.health()
    if not h.ok:
        print(f"SKIPPED: {h.detail}")
        return
    cfg = Config.load(Path(tempfile.mkdtemp()), workers=2)
    c = build_container(cfg, ocr=FakeOCR(), llm=llm, start_workers=True, auto_llm=False)
    ids = {}
    for name, text in DOCS.items():
        import io
        ids[name] = c.docs.import_stream(f"{name}.txt", io.BytesIO(text.encode())).document_id
    while any(c.queue.list(s) for s in ("queued", "running")):
        time.sleep(0.5)
    correct = cited = abstained = warned = 0
    t0 = time.perf_counter()
    for q, fact, doc in ANSWERABLE:
        done = [d for e, d in c.rag.answer_stream(q) if e == "done"][0]
        correct += fact in done["answer"].lower()
        cited += any(x["document_id"] == ids[doc] for x in done["citations"])
        warned += bool(done["warnings"])
        print(f"[{'ok' if fact in done['answer'].lower() else 'MISS'}] {q} -> {done['answer'][:100]!r}")
    for q in UNANSWERABLE:
        done = [d for e, d in c.rag.answer_stream(q) if e == "done"][0]
        abstained += done["not_found"]
        print(f"[{'ok' if done['not_found'] else 'HALLUCINATED?'}] {q} -> {done['answer'][:100]!r}")
    n, m = len(ANSWERABLE), len(UNANSWERABLE)
    result = {"model": model, "answer_correct": correct / n, "citation_accuracy": cited / n,
              "abstention_rate": abstained / m, "warning_rate": warned / n, "seconds": round(time.perf_counter() - t0, 1)}
    print(json.dumps(result, indent=2))
    Path(__file__).with_name("results.json").write_text(json.dumps(result, indent=2))
    c.stop()


if __name__ == "__main__":
    main()

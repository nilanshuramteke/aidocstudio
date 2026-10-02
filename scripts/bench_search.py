"""Search latency benchmark on synthetic data: `python scripts/bench_search.py [n_docs] [dim]`.

Writes straight into a temp DB (no pipeline) so 10k documents build in seconds. Blueprint targets (mid-range laptop):
keyword < 100 ms, hybrid < 500 ms at 10k docs.
"""
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

from adstudio.core.ids import new_id
from adstudio.core.timeutil import now_iso
from adstudio.search.service import SearchService
from adstudio.search.vector import VectorIndex
from adstudio.storage.db import Database, load_sqlite_vec

N = int(sys.argv[1]) if len(sys.argv) > 1 else 10_000
DIM = int(sys.argv[2]) if len(sys.argv) > 2 else 768
CHUNKS_PER_DOC = 3
rng = random.Random(1)
VOCAB = [f"term{i}" for i in range(4000)] + ["invoice", "contract", "payment", "warranty", "shipment", "audit", "lease"]


class RandEmb:
    model_id, dim = "bench", DIM

    def embed(self, texts, *, kind):
        return [[rng.random() - 0.5 for _ in range(DIM)] for _ in texts]


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    db = Database(tmp / "bench.sqlite", on_connect=[load_sqlite_vec])
    db.migrate()
    vec = VectorIndex(db)
    vec.ensure("bench", DIM)
    t0 = time.perf_counter()
    import sqlite_vec
    with db.write() as c:
        for i in range(N):
            did, now = new_id(), now_iso()
            c.execute("INSERT INTO documents(id,title,original_name,mime,size_bytes,sha256,source,state,created_at,updated_at)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?)", (did, f"doc{i}", f"doc{i}.txt", "text/plain", 1, f"{i:064x}", "upload", "ready", now, now))
            for o in range(CHUNKS_PER_DOC):
                text = " ".join(rng.choices(VOCAB, k=120))
                rid = c.execute("INSERT INTO chunks(id,document_id,page_no,ord,text) VALUES(?,?,?,?,?)",
                                (new_id(), did, 1, o, text)).lastrowid
                c.execute("INSERT INTO chunk_vec(rowid, embedding) VALUES(?,?)",
                          (rid, sqlite_vec.serialize_float32([rng.random() - 0.5 for _ in range(DIM)])))
    print(f"built {N} docs / {N * CHUNKS_PER_DOC} chunks (dim {DIM}) in {time.perf_counter() - t0:.1f}s")
    svc = SearchService(db, vec, RandEmb())
    queries = ["invoice payment", "term12 term99", "warranty shipment audit", "lease contract term5"]
    for q in queries:  # warm-up: page cache, sqlite-vec first scan
        svc.search(q, mode="best", parse=False)
    for mode in ("keyword", "best"):
        times = []
        for _ in range(5):
            for q in queries:
                t = time.perf_counter()
                svc.search(q, mode=mode, parse=False)
                times.append((time.perf_counter() - t) * 1000)
        times.sort()
        print(f"{mode:<8} p50 {statistics.median(times):7.1f} ms   p95 {times[int(len(times) * 0.95) - 1]:7.1f} ms")


if __name__ == "__main__":
    main()

"""Graph query benchmark: `python scripts/bench_graph.py [n_entities] [edges_per_entity]` (blueprint: 100k entities, 3 hops).

Edges follow a skewed distribution (a few hub entities with many relationships, like real vendors).
"""
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

from adstudio.knowledge.service import KnowledgeService
from adstudio.storage.db import Database

N = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
EPE = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0


def main() -> None:
    db = Database(Path(tempfile.mkdtemp()) / "g.sqlite")
    db.migrate()
    rng = random.Random(5)
    t0 = time.perf_counter()
    with db.write() as c:
        c.executemany("INSERT INTO entities(id,kind,canonical_name,norm,nk,created_at) VALUES(?,?,?,?,?,?)",
                      ((f"e{i}", "organization", f"Entity {i}", f"entity {i}", f"entity{i}", "t") for i in range(N)))
        c.executemany("INSERT OR IGNORE INTO relationships(id,src_entity_id,dst_entity_id,kind,created_at) VALUES(?,?,?,?,?)",
                      ((f"r{i}", f"e{rng.randrange(N)}", f"e{int(N * rng.random() ** 3)}", "supplies", "t") for i in range(int(N * EPE))))
    print(f"built {N} entities / ~{int(N * EPE)} relationships in {time.perf_counter() - t0:.1f}s")
    kn = KnowledgeService(db)
    with db.read() as c:
        hub = c.execute("SELECT dst_entity_id FROM relationships GROUP BY dst_entity_id ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]
        deg = c.execute("SELECT COUNT(*) FROM relationships WHERE dst_entity_id=? OR src_entity_id=?", (hub, hub)).fetchone()[0]
    print(f"hub {hub} has {deg} relationships")
    for label, start in (("hub", hub), ("typical", "e12345")):
        for depth in (1, 2, 3):
            ts = []
            for _ in range(5):
                t = time.perf_counter()
                g = kn.graph(start, depth)
                ts.append((time.perf_counter() - t) * 1000)
            print(f"{label:<8} depth {depth}: {statistics.median(ts):8.1f} ms  nodes={len(g['nodes'])}{' (truncated)' if g['truncated'] else ''}")


if __name__ == "__main__":
    main()

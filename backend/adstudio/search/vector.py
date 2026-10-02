"""VectorIndex over sqlite-vec (brute-force KNN; fine to ~100k docs). Swap this class for ANN later (ADR-5)."""

from ..core.timeutil import now_iso
from ..storage.db import Database
from ..storage.errors import OperationalError


class VectorIndex:
    def __init__(self, db: Database):
        self.db = db

    def ensure(self, model_id: str, dim: int) -> bool:
        """Create the vec table for this model. Returns True if an existing index was reset (model/dim changed)."""
        with self.db.read() as c:
            row = c.execute("SELECT id, dim FROM embedding_models LIMIT 1").fetchone()
        if row and row["id"] == model_id and row["dim"] == dim:
            return False
        with self.db.write() as c:
            c.execute("DROP TABLE IF EXISTS chunk_vec")
            c.execute("DELETE FROM embedding_models")
            c.execute(f"CREATE VIRTUAL TABLE chunk_vec USING vec0(embedding float[{int(dim)}])")
            c.execute("INSERT INTO embedding_models(id,name,dim,created_at) VALUES(?,?,?,?)",
                      (model_id, model_id, dim, now_iso()))
        return row is not None

    def active_model(self) -> tuple[str, int] | None:
        with self.db.read() as c:
            row = c.execute("SELECT id, dim FROM embedding_models LIMIT 1").fetchone()
        return (row["id"], row["dim"]) if row else None

    def delete_chunks(self, rids: list[int]) -> None:
        if not rids or self.active_model() is None:
            return
        with self.db.write() as c:
            c.executemany("DELETE FROM chunk_vec WHERE rowid=?", [(r,) for r in rids])

    def add(self, items: list[tuple[int, list[float]]]) -> None:
        import sqlite_vec
        with self.db.write() as c:
            for rid, vec in items:
                c.execute("DELETE FROM chunk_vec WHERE rowid=?", (rid,))
                c.execute("INSERT INTO chunk_vec(rowid, embedding) VALUES(?,?)", (rid, sqlite_vec.serialize_float32(vec)))

    def search(self, query_vec: list[float], k: int, allowed: list[int] | None = None) -> list[tuple[int, float]]:
        """[(chunk rid, distance)] nearest first. `allowed` prefilters (metadata filters applied before KNN)."""
        import sqlite_vec
        if self.active_model() is None:
            return []
        sql = "SELECT rowid, distance FROM chunk_vec WHERE embedding MATCH ? AND k=?"
        params: list = [sqlite_vec.serialize_float32(query_vec), k]
        if allowed is not None:
            if not allowed:
                return []
            sql += f" AND rowid IN ({','.join('?' * len(allowed))})"
            params += allowed
        try:
            with self.db.read() as c:
                return [(r[0], r[1]) for r in c.execute(sql, params)]
        except OperationalError:
            return []  # vec table missing/mismatched dimension: semantic search degrades to keyword

    def count(self) -> int:
        if self.active_model() is None:
            return 0
        with self.db.read() as c:
            return c.execute("SELECT COUNT(*) FROM chunk_vec").fetchone()[0]

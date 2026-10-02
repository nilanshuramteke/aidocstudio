-- Phase 4: retrieval chunks + FTS5. Uses an INTEGER primary key (rid) as the FTS content rowid:
-- a TEXT-keyed table's implicit rowid can be renumbered by VACUUM and silently break the index (ADR 001 #1).
-- chunk_vec (sqlite-vec) is created lazily at runtime once the embedding dimension is known.
CREATE TABLE chunks (
  rid         INTEGER PRIMARY KEY,
  id          TEXT UNIQUE NOT NULL,
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  page_no     INTEGER NOT NULL,
  ord         INTEGER NOT NULL,
  text        TEXT NOT NULL,
  char_start  INTEGER,
  char_end    INTEGER,
  bbox_json   TEXT CHECK (bbox_json IS NULL OR json_valid(bbox_json))
) STRICT;
CREATE INDEX ix_chunks_doc ON chunks(document_id, page_no, ord);

CREATE VIRTUAL TABLE chunks_fts USING fts5(
  text, content='chunks', content_rowid='rid', tokenize='unicode61 remove_diacritics 2');

CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, text) VALUES (new.rid, new.text);
END;
CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.rid, old.text);
END;
CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.rid, old.text);
  INSERT INTO chunks_fts(rowid, text) VALUES (new.rid, new.text);
END;

-- Extracted field values are searchable too ("ABC Pvt Ltd" hits the vendor field directly).
CREATE VIRTUAL TABLE field_fts USING fts5(
  document_id UNINDEXED, key UNINDEXED, value, tokenize='unicode61 remove_diacritics 2');

CREATE TABLE embedding_models (
  id TEXT PRIMARY KEY,
  name TEXT,
  dim INTEGER NOT NULL,
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE saved_searches (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  query_json TEXT NOT NULL CHECK (json_valid(query_json)),
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE search_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  q TEXT NOT NULL,
  created_at TEXT NOT NULL
) STRICT;

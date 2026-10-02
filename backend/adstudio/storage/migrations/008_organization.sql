-- Phase 7a: tags and collections (manual or smart). Smart collections store a saved search in query_json.
CREATE TABLE tags (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  color TEXT
) STRICT;

CREATE TABLE document_tags (
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (document_id, tag_id)
) STRICT;
CREATE INDEX ix_document_tags_tag ON document_tags(tag_id);

CREATE TABLE collections (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  kind TEXT NOT NULL CHECK (kind IN ('manual','smart')),
  query_json TEXT CHECK (query_json IS NULL OR json_valid(query_json)),
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE collection_documents (
  collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  PRIMARY KEY (collection_id, document_id)
) STRICT;
CREATE INDEX ix_collection_documents_doc ON collection_documents(document_id);

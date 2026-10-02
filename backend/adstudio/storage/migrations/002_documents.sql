-- Phase 1: documents, versions, pages, metadata. Rebuilds `jobs` to add the documents FK.
-- (document_types arrives in Phase 3; doc_type_id is a plain column until then.)
CREATE TABLE documents (
  id            TEXT PRIMARY KEY,
  title         TEXT NOT NULL,
  original_name TEXT NOT NULL,
  mime          TEXT NOT NULL,
  size_bytes    INTEGER NOT NULL,
  sha256        TEXT NOT NULL,
  source        TEXT NOT NULL,
  parent_id     TEXT REFERENCES documents(id),
  state         TEXT NOT NULL DEFAULT 'imported',
  state_detail  TEXT CHECK (state_detail IS NULL OR json_valid(state_detail)),
  doc_type_id   TEXT,
  doc_type_conf REAL,
  language      TEXT,
  page_count    INTEGER,
  review_status TEXT NOT NULL DEFAULT 'none',
  current_version INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  deleted_at    TEXT
) STRICT;
CREATE UNIQUE INDEX ux_documents_sha_live ON documents(sha256) WHERE deleted_at IS NULL;
CREATE INDEX ix_documents_state ON documents(state);
CREATE INDEX ix_documents_type ON documents(doc_type_id);
CREATE INDEX ix_documents_review ON documents(review_status);
CREATE INDEX ix_documents_created ON documents(created_at);

CREATE TABLE document_versions (
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  version     INTEGER NOT NULL,
  file_path   TEXT NOT NULL,
  sha256      TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  PRIMARY KEY (document_id, version)
) STRICT;

CREATE TABLE document_pages (
  id           TEXT PRIMARY KEY,
  document_id  TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  page_no      INTEGER NOT NULL,
  width        REAL,
  height       REAL,
  image_path   TEXT,
  thumb_path   TEXT,
  text         TEXT,
  text_source  TEXT,
  ocr_conf     REAL,
  UNIQUE (document_id, page_no)
) STRICT;

CREATE TABLE document_metadata (
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  value TEXT,
  PRIMARY KEY (document_id, key)
) STRICT;

-- jobs.document_id gets its FK (SQLite cannot ALTER ADD CONSTRAINT).
CREATE TABLE jobs_new (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  document_id TEXT REFERENCES documents(id) ON DELETE CASCADE,
  payload_json TEXT,
  status TEXT NOT NULL DEFAULT 'queued',
  priority INTEGER NOT NULL DEFAULT 5,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  run_after TEXT,
  locked_by TEXT,
  locked_at TEXT,
  heartbeat_at TEXT,
  error TEXT,
  progress REAL,
  created_at TEXT NOT NULL,
  finished_at TEXT
) STRICT;
INSERT INTO jobs_new SELECT * FROM jobs;
DROP TABLE jobs;
ALTER TABLE jobs_new RENAME TO jobs;
CREATE INDEX ix_jobs_claim ON jobs(status, priority, run_after);
CREATE INDEX ix_jobs_document ON jobs(document_id);

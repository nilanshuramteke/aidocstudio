-- Phase 3: document types (= extraction templates), extracted fields, validation.
-- documents.doc_type_id stays a plain column: SQLite cannot add an FK to an existing column.
CREATE TABLE document_types (
  id TEXT PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  description TEXT,
  keywords_json TEXT CHECK (keywords_json IS NULL OR json_valid(keywords_json)),
  fields_json TEXT NOT NULL CHECK (json_valid(fields_json)),
  builtin INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE extracted_fields (
  id           TEXT PRIMARY KEY,
  document_id  TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  key          TEXT NOT NULL,
  raw_value    TEXT,
  value        TEXT,
  confidence   REAL,            -- final (after validation)
  base_confidence REAL,         -- before validation; lets the validate stage be idempotent
  page_no      INTEGER,
  bbox_json    TEXT CHECK (bbox_json IS NULL OR json_valid(bbox_json)),
  status       TEXT NOT NULL DEFAULT 'auto',   -- auto|needs_review|accepted|corrected|rejected
  flag_reason  TEXT,
  extractor    TEXT,
  updated_at   TEXT NOT NULL,
  UNIQUE (document_id, key)
) STRICT;
CREATE INDEX ix_fields_status ON extracted_fields(status);
CREATE INDEX ix_fields_kv ON extracted_fields(key, value);

CREATE TABLE extracted_rows (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  group_key TEXT NOT NULL,
  row_no INTEGER NOT NULL,
  cells_json TEXT NOT NULL CHECK (json_valid(cells_json)),
  page_no INTEGER,
  confidence REAL
) STRICT;

CREATE TABLE validation_rules (
  id TEXT PRIMARY KEY,
  doc_type_id TEXT REFERENCES document_types(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  config_json TEXT NOT NULL CHECK (json_valid(config_json)),
  severity TEXT NOT NULL DEFAULT 'warn',
  enabled INTEGER NOT NULL DEFAULT 1
) STRICT;

CREATE TABLE validation_results (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  rule_id TEXT NOT NULL REFERENCES validation_rules(id) ON DELETE CASCADE,
  field_key TEXT,
  passed INTEGER NOT NULL,
  message TEXT,
  created_at TEXT NOT NULL
) STRICT;
CREATE INDEX ix_validation_doc ON validation_results(document_id);

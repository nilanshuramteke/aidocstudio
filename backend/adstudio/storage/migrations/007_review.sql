-- Phase 6: human corrections. Feeds correction-rate metrics and few-shot examples for extraction prompts.
CREATE TABLE corrections (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  doc_type_id TEXT,
  field_key TEXT NOT NULL,
  before_value TEXT,
  after_value TEXT,
  context TEXT,            -- the document line the corrected value came from (few-shot grounding)
  created_at TEXT NOT NULL
) STRICT;
CREATE INDEX ix_corrections_type ON corrections(doc_type_id, field_key, created_at);

-- Phase 2: per-page word boxes (normalized 0..1) + engine bookkeeping for cache invalidation.
CREATE TABLE ocr_results (
  page_id    TEXT PRIMARY KEY REFERENCES document_pages(id) ON DELETE CASCADE,
  provider   TEXT NOT NULL,
  provider_version TEXT,
  lang       TEXT,
  words_json TEXT NOT NULL CHECK (json_valid(words_json)),
  created_at TEXT NOT NULL
) STRICT;

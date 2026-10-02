-- Phase 0: system tables only.
CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL CHECK (json_valid(value_json)),
  updated_at TEXT
) STRICT;

CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  action TEXT NOT NULL,
  target_type TEXT,
  target_id TEXT,
  before_json TEXT,
  after_json TEXT
) STRICT;

CREATE TABLE jobs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  document_id TEXT,  -- FK to documents added in the Phase 1 migration
  payload_json TEXT,
  status TEXT NOT NULL DEFAULT 'queued',  -- queued|running|done|failed|cancelled
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
CREATE INDEX ix_jobs_claim ON jobs(status, priority, run_after);

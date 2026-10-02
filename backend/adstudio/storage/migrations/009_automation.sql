-- Phase 7d: workflow rules, run log, watch folders.
CREATE TABLE workflows (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  definition_json TEXT NOT NULL CHECK (json_valid(definition_json)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE workflow_runs (
  id TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  document_id TEXT REFERENCES documents(id) ON DELETE SET NULL,
  event TEXT NOT NULL,
  status TEXT NOT NULL,                 -- ok | failed
  log_json TEXT CHECK (log_json IS NULL OR json_valid(log_json)),
  started_at TEXT,
  finished_at TEXT
) STRICT;
-- loop guard: a workflow runs at most once per (document, event)
CREATE UNIQUE INDEX ux_workflow_runs_once ON workflow_runs(workflow_id, document_id, event) WHERE document_id IS NOT NULL;

CREATE TABLE watch_folders (
  id TEXT PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  recursive INTEGER NOT NULL DEFAULT 1,
  enabled INTEGER NOT NULL DEFAULT 1,
  after_import TEXT NOT NULL DEFAULT 'leave' CHECK (after_import IN ('leave','move','delete')),
  last_scan_at TEXT,
  last_scan_json TEXT CHECK (last_scan_json IS NULL OR json_valid(last_scan_json))
) STRICT;

-- files already seen in a watch folder (so 'leave' mode does not re-import every scan)
CREATE TABLE watch_seen (
  folder_id TEXT NOT NULL REFERENCES watch_folders(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  mtime_ns INTEGER NOT NULL,
  size INTEGER NOT NULL,
  result TEXT,
  PRIMARY KEY (folder_id, path)
) STRICT;

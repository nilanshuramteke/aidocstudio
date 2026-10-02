-- Phase 5: Ask AI conversations. citations_json holds {"citations": [...], "warnings": [...]} for assistant messages.
CREATE TABLE ai_conversations (
  id TEXT PRIMARY KEY,
  title TEXT,
  scope_json TEXT NOT NULL CHECK (json_valid(scope_json)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE ai_messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES ai_conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL,                -- user | assistant
  content TEXT NOT NULL,
  citations_json TEXT CHECK (citations_json IS NULL OR json_valid(citations_json)),
  model TEXT,
  created_at TEXT NOT NULL
) STRICT;
CREATE INDEX ix_ai_messages_conv ON ai_messages(conversation_id, created_at);

-- Phase 8: entities, relationships, document links. Graph queries are recursive CTEs (no graph DB, ADR-14).
CREATE TABLE entities (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                    -- organization | person | ...
  canonical_name TEXT NOT NULL,
  norm TEXT NOT NULL,                    -- normalized name used for matching
  nk TEXT NOT NULL,                      -- norm without spaces: blocking/exact key (OCR drops spaces; "A.B.C." == "abc")
  attrs_json TEXT CHECK (attrs_json IS NULL OR json_valid(attrs_json)),
  created_at TEXT NOT NULL,
  UNIQUE (kind, canonical_name)
) STRICT;
CREATE INDEX ix_entities_norm ON entities(kind, norm);
CREATE INDEX ix_entities_nk ON entities(kind, nk);

CREATE TABLE entity_aliases (
  entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  alias TEXT NOT NULL COLLATE NOCASE,
  norm TEXT NOT NULL,
  nk TEXT NOT NULL,
  PRIMARY KEY (entity_id, alias)
) STRICT;
CREATE INDEX ix_entity_aliases_norm ON entity_aliases(norm);
CREATE INDEX ix_entity_aliases_nk ON entity_aliases(nk);

-- strong identifiers (GSTIN, PAN, ...): two entities with the same identifier are the same entity
CREATE TABLE entity_identifiers (
  entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  value TEXT NOT NULL,
  PRIMARY KEY (kind, value)
) STRICT;
CREATE INDEX ix_entity_identifiers_entity ON entity_identifiers(entity_id);

CREATE TABLE document_entities (
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  field_id TEXT REFERENCES extracted_fields(id) ON DELETE SET NULL,
  PRIMARY KEY (document_id, entity_id, role)
) STRICT;
CREATE INDEX ix_document_entities_entity ON document_entities(entity_id);

CREATE TABLE relationships (
  id TEXT PRIMARY KEY,
  src_entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  dst_entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,                    -- supplies | party_to | ...
  evidence_document_id TEXT REFERENCES documents(id) ON DELETE CASCADE,
  confidence REAL,
  created_at TEXT NOT NULL,
  UNIQUE (src_entity_id, dst_entity_id, kind, evidence_document_id)
) STRICT;
CREATE INDEX ix_rel_src ON relationships(src_entity_id, kind);
CREATE INDEX ix_rel_dst ON relationships(dst_entity_id, kind);
CREATE INDEX ix_rel_evidence ON relationships(evidence_document_id);

-- duplicate (symmetric, a < b) and supersedes (a replaces b)
CREATE TABLE document_links (
  a TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  b TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  score REAL,
  PRIMARY KEY (a, b, kind)
) STRICT;
CREATE INDEX ix_document_links_b ON document_links(b);

-- Regulatory ingestion schema v2. Initialize an EMPTY database only.
-- Requires SQLite >= 3.38 with JSON functions. No source or fixture seed data.
-- Every application connection must enable foreign_keys before transactions.
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TABLE sources (
    source_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(source_id)) > 0),
    source_name TEXT NOT NULL CHECK (length(trim(source_name)) > 0),
    jurisdiction TEXT NOT NULL CHECK (jurisdiction IN ('Hong Kong', 'Singapore', 'European Union', 'United Kingdom')),
    regulator TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type = 'primary_regulator'),
    discovery_url TEXT,
    verified_at TEXT,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    CHECK ((jurisdiction = 'Hong Kong' AND regulator = 'HKMA')
        OR (jurisdiction = 'Singapore' AND regulator = 'MAS')
        OR (jurisdiction = 'European Union' AND regulator = 'ESMA')
        OR (jurisdiction = 'United Kingdom' AND regulator = 'FCA')),
    CHECK (discovery_url IS NULL OR discovery_url GLOB 'https://?*'),
    CHECK (verified_at IS NULL OR (length(verified_at) = 20
        AND strftime('%Y-%m-%dT%H:%M:%SZ', verified_at, '+0 seconds') IS verified_at)),
    CHECK (enabled = 0 OR (discovery_url IS NOT NULL AND verified_at IS NOT NULL))
) STRICT;

CREATE TABLE documents (
    document_id TEXT PRIMARY KEY NOT NULL CHECK (length(document_id) = 64
        AND document_id NOT GLOB '*[^0-9a-f]*'),
    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE RESTRICT,
    source_url TEXT NOT NULL CHECK (source_url GLOB 'https://?*'),
    UNIQUE (source_id, source_url)
) STRICT;

CREATE TABLE document_versions (
    version_id TEXT PRIMARY KEY NOT NULL CHECK (length(version_id) = 64
        AND version_id NOT GLOB '*[^0-9a-f]*'),
    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE RESTRICT,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64
        AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
    downloaded_url TEXT NOT NULL CHECK (downloaded_url GLOB 'https://?*'),
    parent_url TEXT CHECK (parent_url IS NULL OR parent_url GLOB 'https://?*'),
    raw_path TEXT NOT NULL CHECK (raw_path GLOB 'data/raw/?*'),
    media_type TEXT NOT NULL CHECK (media_type IN ('text/html', 'application/pdf')),
    collected_at TEXT NOT NULL CHECK (length(collected_at) = 20
        AND strftime('%Y-%m-%dT%H:%M:%SZ', collected_at, '+0 seconds') IS collected_at),
    last_collected_at TEXT NOT NULL CHECK (length(last_collected_at) = 20
        AND strftime('%Y-%m-%dT%H:%M:%SZ', last_collected_at, '+0 seconds') IS last_collected_at),
    CHECK (last_collected_at >= collected_at),
    UNIQUE (document_id, content_sha256)
) STRICT;

CREATE TABLE extractions (
    extraction_id TEXT PRIMARY KEY NOT NULL CHECK (length(extraction_id) = 64
        AND extraction_id NOT GLOB '*[^0-9a-f]*'),
    version_id TEXT NOT NULL REFERENCES document_versions(version_id) ON DELETE RESTRICT,
    pipeline_version TEXT NOT NULL CHECK (length(pipeline_version) = 64
        AND pipeline_version NOT GLOB '*[^0-9a-f]*'),
    processed_at TEXT NOT NULL CHECK (length(processed_at) = 20
        AND strftime('%Y-%m-%dT%H:%M:%SZ', processed_at, '+0 seconds') IS processed_at),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    language TEXT CHECK (language IS NULL OR language IN ('en', 'zh-Hant', 'zh-Hans')),
    published_at TEXT,
    updated_at TEXT,
    effective_at TEXT,
    document_type TEXT NOT NULL DEFAULT 'other' CHECK (document_type IN (
        'press_release', 'consultation_paper', 'circular', 'guideline', 'speech',
        'legislation', 'policy_statement', 'enforcement_notice', 'market_update', 'other')),
    lifecycle_stage TEXT NOT NULL DEFAULT 'unknown' CHECK (lifecycle_stage IN (
        'proposal', 'consultation', 'enacted', 'implementation', 'enforcement', 'guidance', 'unknown')),
    raw_text TEXT NOT NULL CHECK (length(trim(raw_text)) > 0),
    clean_text TEXT NOT NULL CHECK (length(trim(clean_text)) > 0),
    metadata_evidence TEXT NOT NULL CHECK (CASE WHEN json_valid(metadata_evidence)
        THEN json_type(metadata_evidence) = 'object' ELSE 0 END),
    CHECK (published_at IS NULL OR (published_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        AND date(published_at, '+0 days') IS published_at)),
    CHECK (updated_at IS NULL OR (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        AND date(updated_at, '+0 days') IS updated_at)),
    CHECK (effective_at IS NULL OR (effective_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        AND date(effective_at, '+0 days') IS effective_at)),
    CHECK (published_at IS NULL OR updated_at IS NULL OR updated_at >= published_at),
    UNIQUE (version_id, pipeline_version)
) STRICT;

CREATE TABLE chunks (
    chunk_id TEXT PRIMARY KEY NOT NULL CHECK (length(chunk_id) = 64
        AND chunk_id NOT GLOB '*[^0-9a-f]*'),
    extraction_id TEXT NOT NULL REFERENCES extractions(extraction_id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    text TEXT NOT NULL CHECK (length(trim(text)) > 0),
    char_start INTEGER NOT NULL CHECK (char_start >= 0),
    char_end INTEGER NOT NULL CHECK (char_end > char_start),
    page_start INTEGER,
    page_end INTEGER,
    section TEXT CHECK (section IS NULL OR length(trim(section)) > 0),
    is_table INTEGER CHECK (is_table IS NULL OR is_table IN (0, 1)),
    CHECK ((page_start IS NULL AND page_end IS NULL)
        OR (page_start IS NOT NULL AND page_end IS NOT NULL
            AND page_start >= 1 AND page_end >= page_start)),
    CHECK (length(text) = char_end - char_start),
    UNIQUE (extraction_id, ordinal),
    UNIQUE (extraction_id, chunk_id)
) STRICT;

CREATE TABLE extraction_asset_classes (
    extraction_id TEXT NOT NULL REFERENCES extractions(extraction_id) ON DELETE RESTRICT,
    asset_class TEXT NOT NULL CHECK (asset_class IN (
        'stablecoin', 'cryptocurrency', 'tokenized_security', 'tokenized_deposit', 'rwa', 'cbdc')),
    rule_id TEXT NOT NULL CHECK (length(trim(rule_id)) > 0),
    evidence_chunk_id TEXT NOT NULL,
    PRIMARY KEY (extraction_id, asset_class),
    FOREIGN KEY (extraction_id, evidence_chunk_id)
        REFERENCES chunks(extraction_id, chunk_id) ON DELETE RESTRICT
) STRICT;

-- UNIQUE indexes already cover the source/document/version/extraction FK prefixes.
CREATE INDEX idx_versions_last_collected ON document_versions(document_id, last_collected_at);
CREATE INDEX idx_extractions_published ON extractions(published_at);
CREATE INDEX idx_asset_classes_lookup ON extraction_asset_classes(asset_class, extraction_id);
CREATE INDEX idx_asset_classes_evidence ON extraction_asset_classes(extraction_id, evidence_chunk_id);

-- Stored evidence must be an exact Unicode-code-point slice of clean_text.
CREATE TRIGGER chunks_check_slice BEFORE INSERT ON chunks
WHEN NOT EXISTS (
    SELECT 1 FROM extractions
    WHERE extraction_id = NEW.extraction_id
      AND NEW.char_end <= length(clean_text)
      AND substr(clean_text, NEW.char_start + 1, NEW.char_end - NEW.char_start) = NEW.text
)
BEGIN
    SELECT RAISE(ABORT, 'chunk does not match clean_text');
END;

CREATE TRIGGER documents_no_update BEFORE UPDATE ON documents
BEGIN SELECT RAISE(ABORT, 'document identity is immutable'); END;

CREATE TRIGGER versions_preserve_snapshot BEFORE UPDATE ON document_versions
WHEN NEW.version_id IS NOT OLD.version_id
  OR NEW.document_id IS NOT OLD.document_id
  OR NEW.content_sha256 IS NOT OLD.content_sha256
  OR NEW.downloaded_url IS NOT OLD.downloaded_url
  OR NEW.parent_url IS NOT OLD.parent_url
  OR NEW.raw_path IS NOT OLD.raw_path
  OR NEW.media_type IS NOT OLD.media_type
  OR NEW.collected_at IS NOT OLD.collected_at
  OR NEW.last_collected_at < OLD.last_collected_at
BEGIN SELECT RAISE(ABORT, 'raw snapshot is immutable; last_collected_at may only advance'); END;

CREATE TRIGGER extractions_no_update BEFORE UPDATE ON extractions
BEGIN SELECT RAISE(ABORT, 'create a new extraction for changed processing'); END;

CREATE TRIGGER chunks_no_update BEFORE UPDATE ON chunks
BEGIN SELECT RAISE(ABORT, 'evidence chunks are immutable'); END;

CREATE TRIGGER asset_classes_no_update BEFORE UPDATE ON extraction_asset_classes
BEGIN SELECT RAISE(ABORT, 'create a new extraction for changed classification'); END;

CREATE TABLE html_observations (
    observation_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE CHECK (length(trim(run_id)) > 0),
    extraction_id TEXT NOT NULL REFERENCES extractions(extraction_id) ON DELETE RESTRICT,
    collected_at TEXT NOT NULL CHECK (length(collected_at) = 20
        AND strftime('%Y-%m-%dT%H:%M:%SZ', collected_at, '+0 seconds') IS collected_at),
    downloaded_url TEXT NOT NULL CHECK (downloaded_url GLOB 'https://?*'),
    parent_url TEXT NOT NULL CHECK (parent_url GLOB 'https://?*'),
    raw_path TEXT NOT NULL CHECK (raw_path GLOB 'data/raw/*.html'),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
    response_fingerprint TEXT NOT NULL CHECK (length(response_fingerprint) = 64 AND response_fingerprint NOT GLOB '*[^0-9a-f]*'),
    article_fingerprint TEXT NOT NULL CHECK (length(article_fingerprint) = 64 AND article_fingerprint NOT GLOB '*[^0-9a-f]*'),
    rule_id TEXT NOT NULL CHECK (rule_id = 'hkma-html-csrf-v1'),
    change_type TEXT NOT NULL CHECK (change_type IN ('new', 'unchanged', 'content_changed', 'page_changed'))
) STRICT;
CREATE INDEX idx_html_observations_extraction ON html_observations(extraction_id, observation_id);
CREATE TRIGGER html_observations_no_update BEFORE UPDATE ON html_observations
BEGIN SELECT RAISE(ABORT, 'HTML observations are immutable'); END;

PRAGMA user_version = 2;
COMMIT;

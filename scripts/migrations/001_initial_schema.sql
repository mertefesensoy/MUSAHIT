-- MÜŞAHİT · initial schema · v1
-- Migration applied by scripts/init_db.py · tracked in schema_version.
-- Per ADR-006 storage decision.
--
-- NOTE: All CREATE statements use IF NOT EXISTS for idempotence alongside the
-- schema_version version check. IF NOT EXISTS is NOT a schema-repair mechanism:
-- if a previous partial run left tables with wrong columns, only a new migration
-- can fix them.

-- ── Source registry ────────────────────────────────────────────────────────--
-- Seeded from musahit/ingest/sources.py (FILE-PROTECTED).
CREATE TABLE IF NOT EXISTS sources (
    id                  TEXT    PRIMARY KEY,
    display_name        TEXT    NOT NULL,
    band                TEXT    NOT NULL,
    tier                TEXT    NOT NULL,
    kind                TEXT    NOT NULL,
    url                 TEXT    NOT NULL,
    rate_limit_seconds  INTEGER NOT NULL DEFAULT 5,
    fragility           TEXT    NOT NULL,
    notes               TEXT
);

-- ── Pipeline runs ──────────────────────────────────────────────────────────--
-- One row per nightly execution. stages_done and counts are JSON.
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id        TEXT      PRIMARY KEY,
    started_at    TIMESTAMP NOT NULL,
    completed_at  TIMESTAMP,
    status        TEXT      NOT NULL,
    stages_done   TEXT,
    counts        TEXT
);

-- ── Story arcs ─────────────────────────────────────────────────────────────--
-- Long-lived containers that group clusters reporting the same event thread
-- (ADR-008). entity_set is a JSON array.
CREATE TABLE IF NOT EXISTS arcs (
    id             TEXT      PRIMARY KEY,
    created_at     TIMESTAMP NOT NULL,
    headline       TEXT,
    summary        TEXT,
    state          TEXT      NOT NULL,
    last_update_at TIMESTAMP,
    category       TEXT,
    peak_defcon    INTEGER,
    entity_set     TEXT
);

-- bge-m3 centroid for arc-linking similarity (ADR-008).
CREATE TABLE IF NOT EXISTS arc_centroids (
    arc_id     TEXT         PRIMARY KEY REFERENCES arcs(id),
    centroid   FLOAT[1024]  NOT NULL,
    updated_at TIMESTAMP    NOT NULL
);

-- ── Raw fetched content ────────────────────────────────────────────────────--
-- Stored before normalization. Pruned after 90 days (ADR-012).
-- id = hash(source_id, url, fetched_at).
CREATE TABLE IF NOT EXISTS raw_articles (
    id                TEXT      PRIMARY KEY,
    source_id         TEXT      NOT NULL REFERENCES sources(id),
    url               TEXT      NOT NULL,
    fetched_at        TIMESTAMP NOT NULL,
    raw_content       BLOB,
    content_type      TEXT,
    headers           TEXT,
    fetch_status_code INTEGER
);

-- ── Normalized article content ─────────────────────────────────────────────--
-- entities is a JSON array of {type, text, span}.
CREATE TABLE IF NOT EXISTS articles (
    id           TEXT      PRIMARY KEY,
    source_id    TEXT      NOT NULL REFERENCES sources(id),
    url          TEXT      NOT NULL,
    fetched_at   TIMESTAMP NOT NULL,
    published_at TIMESTAMP,
    title        TEXT,
    lead         TEXT,
    body         TEXT,
    language     TEXT,
    entities     TEXT,
    word_count   INTEGER
);

-- bge-m3 produces 1024-dimensional embeddings (ADR-002).
CREATE TABLE IF NOT EXISTS article_embeddings (
    article_id  TEXT        PRIMARY KEY REFERENCES articles(id),
    embedding   FLOAT[1024] NOT NULL,
    embedded_at TIMESTAMP   NOT NULL
);

-- ── Clusters ───────────────────────────────────────────────────────────────--
-- Groups of articles reporting the same event (ADR-001).
-- bands_present and operator_override are JSON.
-- arc_id is nullable: a cluster may not yet be linked to an arc.
CREATE TABLE IF NOT EXISTS clusters (
    id                TEXT      PRIMARY KEY,
    created_at        TIMESTAMP NOT NULL,
    headline          TEXT,
    summary           TEXT,
    category          TEXT,
    raw_defcon        INTEGER,
    ceiling_defcon    INTEGER,
    final_defcon      INTEGER,
    confidence        TEXT,
    bands_present     TEXT,
    arc_id            TEXT      REFERENCES arcs(id),
    operator_override TEXT
);

CREATE TABLE IF NOT EXISTS cluster_articles (
    cluster_id TEXT NOT NULL REFERENCES clusters(id),
    article_id TEXT NOT NULL REFERENCES articles(id),
    PRIMARY KEY (cluster_id, article_id)
);

-- Centroid of member articles' embeddings, used for arc-linking.
CREATE TABLE IF NOT EXISTS cluster_embeddings (
    cluster_id  TEXT        PRIMARY KEY REFERENCES clusters(id),
    centroid    FLOAT[1024] NOT NULL,
    embedded_at TIMESTAMP   NOT NULL
);

-- ── Per-source ingest results ──────────────────────────────────────────────--
CREATE TABLE IF NOT EXISTS ingest_log (
    run_id           TEXT      NOT NULL REFERENCES pipeline_runs(run_id),
    source_id        TEXT      NOT NULL REFERENCES sources(id),
    started_at       TIMESTAMP,
    completed_at     TIMESTAMP,
    status           TEXT      NOT NULL,
    articles_fetched INTEGER,
    error_detail     TEXT,
    PRIMARY KEY (run_id, source_id)
);

-- ── Promotion audit log ────────────────────────────────────────────────────--
-- Every band-ceiling decision is recorded here for operator audit (ADR-005).
-- bands_present and sides_present are JSON arrays.
CREATE TABLE IF NOT EXISTS promotion_log (
    cluster_id     TEXT      PRIMARY KEY REFERENCES clusters(id),
    raw_defcon     INTEGER,
    ceiling_defcon INTEGER,
    final_defcon   INTEGER,
    bands_present  TEXT,
    sides_present  TEXT,
    confidence     TEXT,
    rule_applied   TEXT,
    computed_at    TIMESTAMP NOT NULL
);

-- ── Manual operator overrides ──────────────────────────────────────────────--
-- target_id references a cluster_id or arc_id depending on target_type.
CREATE TABLE IF NOT EXISTS manual_overrides (
    id          TEXT      PRIMARY KEY,
    target_type TEXT      NOT NULL,
    target_id   TEXT      NOT NULL,
    action      TEXT      NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    reason      TEXT,
    applied_at  TIMESTAMP NOT NULL
);

-- ── Briefings ─────────────────────────────────────────────────────────────--
-- One row per night · paths reference files in briefings/YYYY/MM/DD/.
CREATE TABLE IF NOT EXISTS briefings (
    date           DATE      PRIMARY KEY,
    generated_at   TIMESTAMP NOT NULL,
    markdown_path  TEXT      NOT NULL,
    html_path      TEXT      NOT NULL,
    audio_path     TEXT,
    peak_defcon    INTEGER,
    cluster_count  INTEGER,
    arc_count      INTEGER,
    open_arc_count INTEGER
);

-- ── Non-VSS indices ────────────────────────────────────────────────────────--
-- HNSW indices for article_embeddings and arc_centroids are created by
-- scripts/init_db.py when the VSS extension is available.
CREATE INDEX IF NOT EXISTS idx_articles_published    ON articles(published_at);
CREATE INDEX IF NOT EXISTS idx_articles_source       ON articles(source_id);
CREATE INDEX IF NOT EXISTS idx_clusters_final_defcon ON clusters(final_defcon);
CREATE INDEX IF NOT EXISTS idx_clusters_created      ON clusters(created_at);
CREATE INDEX IF NOT EXISTS idx_clusters_arc          ON clusters(arc_id);
CREATE INDEX IF NOT EXISTS idx_arcs_state            ON arcs(state);
CREATE INDEX IF NOT EXISTS idx_ingest_log_run        ON ingest_log(run_id);

# ADR-006 · Storage

**Status** · Accepted · 2026-05-22 · Amended 2026-05-23 by ADR-014 and ADR-015
**Author** · Mert Efe Şensoy
**Supersedes** · none
**Amended by** · ADR-014 (id formula) · ADR-015 (typed metadata columns)
**Cross-references** · ADR-001 · ADR-012 · ADR-014 · ADR-015

> **Amendment notes** (2026-05-23)
>
> - The `raw_articles.id` / `articles.id` schema comment originally read
>   `hash(source_id, url, fetched_at)`. **ADR-014** supersedes that formula:
>   the canonical id is `sha256(source_id|url)`, computed by the shared
>   helper `musahit.common.ids.article_id`. Including `fetched_at` would
>   break inter-fetch dedup and the foreign-key contract used by
>   `cluster_articles`, `promotion_log`, and the arc-linking stage.
> - The `raw_articles` table gains two typed metadata columns under
>   **ADR-015**: `feed_entry_id TEXT NULL` and
>   `canonical_timestamp TIMESTAMP NULL`, plus the supporting index
>   `idx_raw_articles_canonical_ts`. The columns are added by
>   `scripts/migrations/002_add_article_metadata.sql`. Ingester-specific
>   metadata still lives in the loose `headers` JSON column.
>
> The schema block below documents the original v1 layout. The actual
> on-disk schema reflects both amendments after migration 002 applies.

---

## ❯ Context

MÜŞAHİT is a single-operator local system. The storage layer must support analytical
queries over months of historical articles and arcs (the operator will want to ask "show
me all DEFCON 2+ events in Q3 across the JUDICIAL category"), high-volume inserts during
ingestion (200-500 articles per night), and efficient embedding vector storage and
similarity search.

The operator already uses DuckDB in the BTC/USDT trading project and is familiar with it.

## ❯ Decision

**DuckDB** as the single storage backend · one file at `data/musahit.duckdb`.

DuckDB handles all of: raw articles, normalized articles, clusters, embeddings,
classifications, arcs, briefings, ingestion logs, promotion logs, and manual overrides.
The `VSS` extension (Vector Similarity Search) handles embedding queries.

### Schema (v1)

```sql
-- Source registry · seeded from src/ingest/sources.py
CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    band TEXT NOT NULL,
    tier TEXT NOT NULL,
    kind TEXT NOT NULL,
    url TEXT NOT NULL,
    rate_limit_seconds INTEGER NOT NULL DEFAULT 5,
    fragility TEXT NOT NULL,
    notes TEXT
);

-- Raw fetched content · before normalization
-- After ADR-015 this table also has feed_entry_id and canonical_timestamp
-- columns; see scripts/migrations/002_add_article_metadata.sql.
CREATE TABLE raw_articles (
    id TEXT PRIMARY KEY,                -- sha256(source_id|url) · see ADR-014
    source_id TEXT NOT NULL REFERENCES sources(id),
    url TEXT NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    raw_content BLOB,                   -- raw HTML or PDF bytes
    content_type TEXT,                  -- mime
    headers TEXT,                       -- JSON
    fetch_status_code INTEGER
);

-- Normalized article content
CREATE TABLE articles (
    id TEXT PRIMARY KEY,                -- same as raw_articles.id · sha256(source_id|url) · ADR-014
    source_id TEXT NOT NULL REFERENCES sources(id),
    url TEXT NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    published_at TIMESTAMP,
    title TEXT,
    lead TEXT,                          -- first ~500 chars of body
    body TEXT,
    language TEXT,
    entities TEXT,                      -- JSON array of {type, text, span}
    word_count INTEGER
);

-- Article embeddings · bge-m3 produces 1024-dim vectors
CREATE TABLE article_embeddings (
    article_id TEXT PRIMARY KEY REFERENCES articles(id),
    embedding FLOAT[1024] NOT NULL,
    embedded_at TIMESTAMP NOT NULL
);

-- Clusters · groups of articles reporting the same event
CREATE TABLE clusters (
    id TEXT PRIMARY KEY,                -- cl_YYYYMMDD_NNNN
    created_at TIMESTAMP NOT NULL,
    headline TEXT,                      -- worker-generated cluster headline
    summary TEXT,                       -- worker-generated 1-2 sentence summary
    category TEXT,                      -- POLİTİKA · EKONOMİ · etc.
    raw_defcon INTEGER,                 -- worker's raw severity
    ceiling_defcon INTEGER,             -- promotion ceiling
    final_defcon INTEGER,               -- min of raw and ceiling
    confidence TEXT,                    -- YÜKSEK · ORTA · DÜŞÜK
    bands_present TEXT,                 -- JSON array
    arc_id TEXT,                        -- nullable · references arcs(id)
    operator_override TEXT              -- nullable · JSON of any manual change
);

-- Cluster membership
CREATE TABLE cluster_articles (
    cluster_id TEXT NOT NULL REFERENCES clusters(id),
    article_id TEXT NOT NULL REFERENCES articles(id),
    PRIMARY KEY (cluster_id, article_id)
);

-- Cluster embeddings · centroid of member articles
CREATE TABLE cluster_embeddings (
    cluster_id TEXT PRIMARY KEY REFERENCES clusters(id),
    centroid FLOAT[1024] NOT NULL,
    embedded_at TIMESTAMP NOT NULL
);

-- Story arcs · ongoing event threads
CREATE TABLE arcs (
    id TEXT PRIMARY KEY,                -- arc_YYYYMMDD_NNNN
    created_at TIMESTAMP NOT NULL,
    headline TEXT,                      -- arc title
    summary TEXT,                       -- rolling summary, updated by writer
    state TEXT NOT NULL,                -- OPEN · WATCH · RESOLVED
    last_update_at TIMESTAMP,
    category TEXT,
    peak_defcon INTEGER,                -- highest DEFCON ever seen in this arc
    entity_set TEXT                     -- JSON · entities defining the arc
);

CREATE TABLE arc_centroids (
    arc_id TEXT PRIMARY KEY REFERENCES arcs(id),
    centroid FLOAT[1024] NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

-- Pipeline runs · per-night execution log
CREATE TABLE pipeline_runs (
    run_id TEXT PRIMARY KEY,            -- run_YYYYMMDD
    started_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP,
    status TEXT NOT NULL,               -- RUNNING · COMPLETED · FAILED
    stages_done TEXT,                   -- JSON array of stage names
    counts TEXT                         -- JSON · {articles, clusters, arcs}
);

-- Per-source ingest results
CREATE TABLE ingest_log (
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    status TEXT NOT NULL,               -- OK · TIMEOUT · HTTP_ERROR · PARSE_ERROR · SKIPPED
    articles_fetched INTEGER,
    error_detail TEXT,
    PRIMARY KEY (run_id, source_id)
);

-- Promotion audit log · ADR-005
CREATE TABLE promotion_log (
    cluster_id TEXT PRIMARY KEY REFERENCES clusters(id),
    raw_defcon INTEGER,
    ceiling_defcon INTEGER,
    final_defcon INTEGER,
    bands_present TEXT,
    sides_present TEXT,
    confidence TEXT,
    rule_applied TEXT,
    computed_at TIMESTAMP NOT NULL
);

-- Manual operator overrides via dashboard
CREATE TABLE manual_overrides (
    id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,          -- CLUSTER · ARC
    target_id TEXT NOT NULL,
    action TEXT NOT NULL,               -- PROMOTE · DEMOTE · RESOLVE · DISMISS
    old_value TEXT,
    new_value TEXT,
    reason TEXT,
    applied_at TIMESTAMP NOT NULL
);

-- Briefings · one row per night
CREATE TABLE briefings (
    date DATE PRIMARY KEY,
    generated_at TIMESTAMP NOT NULL,
    markdown_path TEXT NOT NULL,
    html_path TEXT NOT NULL,
    audio_path TEXT,
    peak_defcon INTEGER,
    cluster_count INTEGER,
    arc_count INTEGER,
    open_arc_count INTEGER
);
```

### Indices

```sql
CREATE INDEX idx_articles_published ON articles(published_at);
CREATE INDEX idx_articles_source ON articles(source_id);
CREATE INDEX idx_clusters_final_defcon ON clusters(final_defcon);
CREATE INDEX idx_clusters_created ON clusters(created_at);
CREATE INDEX idx_clusters_arc ON clusters(arc_id);
CREATE INDEX idx_arcs_state ON arcs(state);
CREATE INDEX idx_ingest_log_run ON ingest_log(run_id);
```

### Vector search

`VSS` extension provides HNSW indices for cosine similarity:

```sql
INSTALL vss;
LOAD vss;

CREATE INDEX idx_article_emb_hnsw
ON article_embeddings
USING HNSW (embedding)
WITH (metric = 'cosine');

CREATE INDEX idx_arc_centroid_hnsw
ON arc_centroids
USING HNSW (centroid)
WITH (metric = 'cosine');
```

### Migrations

Schema migrations live in `scripts/migrations/`. Each migration is a numbered SQL file:

```
scripts/migrations/
  001_initial_schema.sql
  002_add_promotion_log.sql
  003_...
```

The `scripts/init_db.py` script applies migrations idempotently. The applied migration
version is stored in a `schema_version` table.

### File-system artifacts

Some content is stored on disk rather than in DuckDB:

- `briefings/YYYY/MM/DD/briefing.md` · the rendered briefing
- `briefings/YYYY/MM/DD/briefing.html` · the dashboard-rendered version
- `briefings/YYYY/MM/DD/briefing.mp3` · the Piper audio
- `data/backups/musahit-YYYYMMDD.duckdb` · nightly backup

The `briefings` table references these paths but does not store the content. This keeps
the DB file lean and allows direct file access for the dashboard and audio player.

### Backups

After each successful run, the pipeline copies `data/musahit.duckdb` to
`data/backups/musahit-YYYYMMDD.duckdb`. Backups older than 30 days are pruned (the
backups themselves are not redundancy-stored elsewhere · the operator owns disaster
recovery).

## ❯ Consequences

**Positive**
- Single-file database · easy to back up · easy to inspect with DuckDB CLI
- DuckDB's analytical query performance handles the operator's ad-hoc "show me all..."
  queries trivially
- VSS extension keeps vector search in-database · no separate Qdrant or Chroma instance
- Familiar to operator from BTC/USDT trading project

**Negative**
- DuckDB is not designed for high-concurrency writes · single-process pipeline avoids
  this · the dashboard reads only · no contention
- VSS index rebuild required if embeddings change · acceptable at our volume
- BLOB storage of raw HTML in `raw_articles` may bloat the DB · monitor and consider
  external storage if it exceeds ~5 GB

## ❯ Alternatives considered

- **SQLite** · rejected · analytical queries on 6 months of data would be slow · operator
  already prefers DuckDB
- **PostgreSQL with pgvector** · rejected · operational overhead for single-user system ·
  network service · backup story more complex
- **Qdrant + SQLite split** · rejected · two systems instead of one · added complexity

## ❯ Open questions

- BLOB storage of raw HTML may need an external blob store if DB grows past ~10 GB ·
  revisit after 3 months of operation
- Whether `cluster_embeddings` is needed or whether on-demand centroid computation from
  member embeddings is acceptable · revisit if arc-linking is slow

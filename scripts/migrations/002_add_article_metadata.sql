-- MÜŞAHİT · migration 002 · add typed metadata columns to raw_articles
-- Applied by scripts/init_db.py · tracked in schema_version.
-- Per ADR-015 (article metadata typed columns).
--
-- Two values that every ingester plausibly produces · `feed_entry_id` and
-- `canonical_timestamp` · are promoted out of the loose `headers` JSON column
-- and into typed nullable columns so the normalize/cluster/arc-link stages
-- have a stable contract to read against. Ingester-specific quirks (feed-
-- claimed author, Reddit score, KAP disclosure code, etc.) remain in
-- `raw_articles.headers` JSON per each ingester's implementation doc.

-- ── New columns ────────────────────────────────────────────────────────────
-- Both NULL because not every source provides both values (HTML scrapes
-- often lack a `feed_entry_id`; some primary feeds lack a usable timestamp).
ALTER TABLE raw_articles ADD COLUMN IF NOT EXISTS feed_entry_id       TEXT;
ALTER TABLE raw_articles ADD COLUMN IF NOT EXISTS canonical_timestamp TIMESTAMP;

-- ── Supporting index ───────────────────────────────────────────────────────
-- The cluster stage queries by canonical_timestamp inside a 24-hour window.
-- Per ADR-015 the index pays back immediately.
CREATE INDEX IF NOT EXISTS idx_raw_articles_canonical_ts
    ON raw_articles(canonical_timestamp);

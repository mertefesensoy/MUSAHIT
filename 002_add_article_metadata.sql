-- 002_add_article_metadata.sql
-- Adds typed columns for universal article metadata per ADR-015.
-- See ADR-014 for article id formula context.
-- Idempotent via IF NOT EXISTS.

ALTER TABLE raw_articles ADD COLUMN IF NOT EXISTS feed_entry_id TEXT;
ALTER TABLE raw_articles ADD COLUMN IF NOT EXISTS canonical_timestamp TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_raw_articles_canonical_ts
    ON raw_articles(canonical_timestamp);

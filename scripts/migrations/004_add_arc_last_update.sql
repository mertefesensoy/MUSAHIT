-- MÜŞAHİT · migration 004 · arc last_update_* triplet
-- Per arc-evolution design discussion 2026-05-25.
--
-- Adds three columns to `arcs` that capture what the most-recent joining
-- cluster contributed. The existing `arcs.last_update_at` column (from
-- migration 001) already records WHEN the arc was last touched · these
-- three columns add WHAT changed:
--
--   * last_update_summary  · the most recent cluster's summary
--   * last_update_headline · the most recent cluster's headline (stored
--                            even though briefings keep the seed headline
--                            stable across days · audit + future use)
--   * last_update_cluster_id · id of that cluster (audit trail · lets a
--                              future operator look up the source cluster
--                              that drove the update)
--
-- These let the briefing renderer differentiate active-today arcs (which
-- get a `**Güncelleme** · {last_update_summary}` body) from stalled arcs
-- (which keep their original {summary} + an italic stalled marker, plus
-- a `**Son güncelleme** · X gün önce` line in the header).
--
-- Backfill: existing arcs are treated as if their most recent update is
-- whatever the seed write recorded. last_update_summary/headline copy
-- from the existing summary/headline columns. last_update_cluster_id
-- is set to the most-recently-created linked cluster, or NULL if no
-- cluster is linked yet (the renderer handles NULL safely).
--
-- Additive only. NULL-safe: the payload + renderer fall back to the
-- original summary/headline when last_update_* is NULL.

ALTER TABLE arcs ADD COLUMN IF NOT EXISTS last_update_summary TEXT;
ALTER TABLE arcs ADD COLUMN IF NOT EXISTS last_update_headline TEXT;
ALTER TABLE arcs ADD COLUMN IF NOT EXISTS last_update_cluster_id TEXT;

-- Backfill: seed-time headline/summary become the initial last_update_*.
UPDATE arcs
   SET last_update_summary = summary
 WHERE last_update_summary IS NULL;

UPDATE arcs
   SET last_update_headline = headline
 WHERE last_update_headline IS NULL;

-- Backfill last_update_cluster_id from the most recently-created cluster
-- linked to each arc. DuckDB supports correlated subqueries in UPDATE;
-- the LIMIT 1 + ORDER BY DESC selects the freshest one. If no cluster is
-- linked the subquery returns NULL · the column stays NULL · safe.
UPDATE arcs
   SET last_update_cluster_id = (
       SELECT c.id
         FROM clusters c
        WHERE c.arc_id = arcs.id
        ORDER BY c.created_at DESC
        LIMIT 1
   )
 WHERE last_update_cluster_id IS NULL;

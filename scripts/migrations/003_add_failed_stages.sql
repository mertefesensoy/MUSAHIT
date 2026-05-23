-- MÜŞAHİT · migration 003 · failed_stages column
-- Per build-order step 15 (pipeline orchestrator).
--
-- The orchestrator records per-stage exceptions on pipeline_runs so the
-- briefing's SİSTEM LOG footer and the dashboard's run-history view can
-- show which stages failed without re-parsing logs.
--
-- Additive only · existing rows get NULL. Backward compatible with the
-- ingest poller's pipeline_runs UPSERTs which don't reference the column.

ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS failed_stages TEXT;

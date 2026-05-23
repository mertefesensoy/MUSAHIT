# ADR-015 · Article metadata typed columns

**Status** · Accepted · 2026-05-23
**Author** · Mert Efe Şensoy
**Amends** · ADR-006 (`raw_articles` schema)
**Cross-references** · ADR-006 · ADR-014

---

## ❯ Context

During step 4 (RSS ingester), two values that all ingesters will plausibly produce
were stashed in `raw_articles.headers` JSON to avoid a schema change:

- `feed_entry_id` · the source-provided id (RSS guid · Reddit post id · KAP
  submission id · etc.) used for intra-fetch deduplication
- `canonical_timestamp` · the best ingest-time timestamp per ADR-008's arc-linking
  intent · computed as `min(published_at, updated_at)` for RSS · will be derived
  differently per ingester (HTML scrape extracts from JSON-LD or meta tags · Resmi
  Gazete uses the gazette publication date · etc.)

The pattern worked for one ingester. Step 5 (HTML scrape) is the second of four
ingesters · without a typed contract, each ingester will key these values
differently in the headers JSON and downstream stages (normalize · cluster ·
arc-link) will have to read defensively against a moving target.

Of four paths considered (kitchen drawer · everything typed · hybrid · defer), the
operator chose **promote universal metadata to typed columns · keep
ingester-specific quirks in headers JSON**.

## ❯ Decision

Two new nullable columns on `raw_articles`:

- `feed_entry_id TEXT NULL` · source-provided id when available · NULL for sources
  without one (HTML scrape · some primary feeds)
- `canonical_timestamp TIMESTAMP NULL` · best timestamp at ingest time · NULL when
  the source provides no usable timestamp

Both nullable because not every source provides both values. The normalize stage
(step 9) reads `canonical_timestamp` and populates `articles.published_at` from it.

An additive migration `scripts/migrations/002_add_article_metadata.sql` introduces
the columns. Idempotent via `IF NOT EXISTS`.

### Convention for `raw_articles.headers` JSON

`headers` remains a loose JSON column for ingester-specific metadata. Each ingester
documents its keys in its implementation document. The normalize stage reads only
what it needs from `headers` per-ingester · the typed columns are the universal
contract.

Examples of what stays in headers JSON:

| Ingester | Ingester-specific keys |
|---|---|
| RSS | feed-claimed author · category tags |
| HTML scrape | listing-page URL · og:image · JSON-LD extraction method |
| Resmi Gazete | decree number · decree type · gazette edition number |
| KAP | disclosure type · company ticker · disclosure category code |
| Reddit | subreddit · score · comment count · post flair · OP username |

What is **not** in headers JSON (anymore):

- `feed_entry_id` · now a column
- `canonical_timestamp` · now a column

### Index

```sql
CREATE INDEX IF NOT EXISTS idx_raw_articles_canonical_ts
    ON raw_articles(canonical_timestamp);
```

The cluster stage queries by `canonical_timestamp` within a 24-hour window · the
index pays back immediately.

## ❯ Required follow-up edits in the same commit

1. `scripts/migrations/002_add_article_metadata.sql` · the migration itself
2. `musahit/ingest/rss.py` · stop stashing `feed_entry_id` and `canonical_timestamp`
   in headers JSON · write them to the new columns
3. `tests/test_rss.py` · update assertions to check the new columns instead of
   headers JSON
4. `docs/implementations/2026-05-23-rss-ingest.md` · append a note that the
   headers JSON storage pattern was superseded by ADR-015 the same day
5. `adr/ADR-006-storage.md` · add a cross-reference note pointing to ADR-015

No data migration needed · step 4 left only test data in `data/musahit.duckdb`
(if anything) · the new columns default to NULL on existing rows · the operator
can drop the local DB file before step 5 if they want a clean slate.

## ❯ Consequences

**Positive**
- All four ingesters write the universal metadata to the same place · normalize
  stage has a typed contract to read against
- `canonical_timestamp` index makes the cluster stage's 24-hour window query cheap
- Ingester-specific quirks (Reddit score · KAP disclosure type · etc.) stay
  flexible in headers JSON · no schema change for each new ingester quirk
- The typed/loose split is honest about what generalizes and what does not

**Negative**
- One more migration to track · 002 is cheap but it counts
- `headers` JSON is now smaller and the operator might wonder what belongs there ·
  mitigated by per-ingester docs
- HTML scrape's `canonical_timestamp` extraction fallback chain (JSON-LD · meta
  tags · regex · `fetched_at`) all funnel into the same column · the
  fallback-method-used metadata goes to `headers` for debugging

## ❯ Alternatives considered

Documented in the elicitation round prior to this ADR:

- **Path A · Kitchen drawer** · keep headers JSON loose · per-ingester convention
  in implementation docs · rejected as fragile across four ingesters
- **Path B · This ADR** · accepted
- **Path C · Hybrid (canonical_timestamp only)** · promote only the most-used field
  · rejected because `feed_entry_id` is also universal-shaped (every feed-style
  source has some form of native id) and splitting the promotion is arbitrary
- **Path D · Defer** · revisit after step 7 · rejected because step 5 starts now
  and would inherit the kitchen-drawer pattern · cleaning up later is more work
  than getting it right now

## ❯ Open questions

- Whether `articles` (the normalized table) needs its own `canonical_timestamp`
  column or whether `articles.published_at` suffices · current answer: the
  normalize stage copies `raw_articles.canonical_timestamp` into
  `articles.published_at` · no second column needed · revisit if normalize finds
  a reason to distinguish the two values
- Whether HTML scrape's "which method extracted the timestamp" telemetry warrants
  promotion to a column · deferred · acceptable in headers JSON for v0.1

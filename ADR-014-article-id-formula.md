# ADR-014 · Article id formula amendment

**Status** · Accepted · 2026-05-23
**Author** · Mert Efe Şensoy
**Amends** · ADR-006 (schema comment on `raw_articles.id` and `articles.id`)
**Cross-references** · ADR-001 · ADR-005 · ADR-006 · ADR-008 · ADR-012

---

## ❯ Context

ADR-006 defined the schema for `raw_articles` and `articles` with an inline comment on
the primary key: `id TEXT PRIMARY KEY -- hash(source_id, url, fetched_at)`. During
step 4 (RSS ingester) implementation, this formula was found to be incompatible with
the deduplication intent of the same ADR.

Including `fetched_at` in the hash produces a new id every time MÜŞAHİT fetches the
same article from the same source. That defeats the `INSERT OR IGNORE` cross-fetch
deduplication strategy that the rest of the pipeline depends on.

This is a contract problem. Article id stability across fetches is required by:

- The dedup discipline in stage 1 (ingest)
- The cluster member lookup in stage 3 (cluster)
- The arc-linked cluster history in stage 5 (arc-link)
- The promotion audit log in stage 4 (score · ADR-005)
- Foreign key relationships in `cluster_articles` (ADR-006)

The discovery happened during step 4 and was logged in
`docs/implementations/2026-05-23-rss-ingest.md` as a deliberate deviation. This ADR
amendment promotes that deviation to canonical record so steps 5-7 do not re-derive
different formulas.

## ❯ Decision

Canonical article id formula across all ingesters:

```python
import hashlib

def article_id(source_id: str, url: str) -> str:
    return hashlib.sha256(f"{source_id}|{url}".encode("utf-8")).hexdigest()
```

Properties:

- **Stable across fetches** · same `(source_id, url)` produces the same id forever
- **Source-scoped** · two sources syndicating the same URL retain separate article
  rows · required for cross-band corroboration in ADR-005
- **Deterministic** · no time component · no content component · no environment
  dependence
- **Hex-encoded SHA-256** · 64 characters · TEXT-safe

This formula applies to:

- All current Ingester Protocol implementations (`musahit/ingest/rss.py` already
  conforms · step 4)
- All future ingesters (steps 5-7 · HTML · PDF · KAP · Reddit)
- Any tooling that needs to compute article ids outside the ingest path
  (re-analysis scripts · debugging · backfill)

## ❯ Required follow-up edits

This ADR amendment requires three corrective edits in the same commit it lands:

1. `scripts/migrations/001_initial_schema.sql` · update the inline comment on
   `raw_articles.id` and `articles.id` to read
   `-- sha256(source_id|url) · see ADR-014`
   (SQL comments are documentation · safe to amend without a new migration)
2. `adr/ADR-006-storage.md` · add a note under the `raw_articles` schema block
   pointing to ADR-014
3. A helper `musahit/common/ids.py` exposing `article_id(source_id, url) -> str`
   so steps 5-7 import the function rather than re-implementing the formula

The helper is the load-bearing piece. Re-implementations are how formulas drift.

## ❯ Consequences

**Positive**
- Cross-fetch dedup works as the rest of the pipeline assumes
- Single formula across all ingesters · no per-ingester drift
- Foreign key references in `cluster_articles` and the audit logs remain valid
  even when articles are re-fetched
- The helper module gives future ingesters one obvious place to look

**Negative**
- Article *updates* (same URL · edited content) produce no new article row · the
  `INSERT OR IGNORE` path ignores the second fetch entirely · the operator never
  sees that an outlet edited a piece
- For v0.1 this is acceptable · most edits are minor typo fixes · revisiting an
  edit is not the operator's morning-coffee priority
- If article-version tracking becomes important later, an additive
  `articles_revisions` table or `content_hash` column can be introduced without
  breaking the id formula · the formula's stability is the foundation that allows
  versioning to be layered on top

## ❯ Alternatives considered

- **Compound primary key `(source_id, url)`** · technically equivalent · rejected
  because the rest of the schema uses single-column `TEXT PRIMARY KEY` consistently
  · changing the pattern in one table is noise
- **Hash including `content_hash`** · would catch edits · rejected because parsing
  content at the ingest stage is exactly what stage 2 (normalize) exists to avoid ·
  also content varies (full body · summary only · fetched-later HTML) so a
  content hash at ingest time is unstable
- **Hash including `fetched_at`** · the original ADR-006 specification · rejected
  by use case · documented here as the original incorrect specification
- **Hash on URL only** · rejected because two sources syndicating the same URL
  must retain separate rows for cross-band corroboration

## ❯ Open questions

- Article-version tracking · deferred · revisit if operator finds value in
  seeing edits over time
- Whether to add a non-key `content_hash` column on `articles` for later edit
  detection without restructuring · deferred · not blocking

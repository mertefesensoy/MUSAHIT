# Implementation: empty-headline arc bug fix · Option A + B + cleanup

**Date** · 2026-05-25
**Author** · MERT EFE ŞENSOY
**Investigation** · `docs/investigations/2026-05-25-empty-headlines.md`
**ADR refs** · ADR-005 (promotion + score stage), ADR-008 (arc model), ADR-012 (failure isolation)

---

## ❯ Problem / Motivation

Two arcs (`arc_20260523_0146`, `arc_20260523_0036`) rendered with the
placeholder `(başlıksız)` ("untitled") in the briefing. The 2026-05-25
investigation traced the symptom to the score stage's fallback path:
when the worker LLM (Qwen 2.5 7B) returns malformed JSON
`max_retries + 1` times, `Classifier._classify_one` returns
`_FALLBACK_RESPONSE` whose `headline` and `summary` were literal empty
strings. The arc-link stage (`_seed_arc`) seeded an arc directly from
those empty fields with no guard. The renderer
(`fallback._render_arc`) substituted `(başlıksız)` for the missing
headline and dropped the body entirely. With the 2026-05-25 arc-
evolution change, the active-today sort tier promoted
`arc_20260523_0146` into the voiced *Öne Çıkanlar* section — meaning
Piper would have read out a section header followed by no content.

Fix per the investigation document's recommendation: **Option A + B
combined plus a one-off repair script** for the two existing affected
arcs.

---

## ❯ What Changed

| File | Description |
|---|---|
| `musahit/score/classifier.py` | `_FALLBACK_RESPONSE.headline` = `"(sınıflandırılamadı)"`, `summary` = "Skorlama modeli bu kümede geçerli yanıt üretemedi. Operatör incelemesi bekliyor." (was both empty strings). `confidence_self="low"` retained with explanatory comment (pydantic-required, not consumed by `_persist`). |
| `musahit/arcs/linker.py` | `_select_pending` SQL adds `AND coalesce(trim(c.headline), '') != ''` so clusters with NULL or spaces-only headline never become arc seeds. FILE-PROTECTED edit authorised by `/goal`; scope strictly limited to the one WHERE-clause line. |
| `scripts/maintenance/repair_2026_05_25_empty_arcs.py` | New one-off operator script. Idempotent. Repairs `arc_20260523_0146` from `cl_20260525_0076`'s real Bilgi Üniversitesi content and writes the placeholder text into `arc_20260523_0036`. Only touches the four text columns (`headline`, `summary`, `last_update_headline`, `last_update_summary`) — `last_update_at` and `last_update_cluster_id` stay put per tripwire. |
| `tests/test_score/test_fallback.py` | New file · 9 tests pinning the placeholder text + end-to-end integration that the cluster row carries the placeholder after a fallback fires. |
| `tests/test_linker.py` | 4 new tests covering empty-string headline, spaces-only headline, real-headline cluster still seeds, and placeholder-headline cluster (from the matching score fix) passes the filter. |
| `tests/test_writer/test_fallback.py` | 3 new tests: active-today arc with placeholder renders without `(başlıksız)`, placeholder summary appears under the `**Güncelleme**` prefix, stalled placeholder arc in overflow bullet form. |

---

## ❯ Implementation Approach

### Option B · classifier placeholder

`_FALLBACK_RESPONSE` is the canned `WorkerResponse` the classifier
returns after `max_retries` failed parses. The two text fields were
previously empty strings; this change makes them non-empty Turkish
placeholders the operator can both hear and grep. The literal text:

* **headline**: `"(sınıflandırılamadı)"` — fits the 200-char pydantic
  bound on `WorkerResponse.headline`; reads naturally in Turkish as
  "could not be classified" (parenthetical signal).
* **summary**: `"Skorlama modeli bu kümede geçerli yanıt üretemedi.
  Operatör incelemesi bekliyor."` — fits the 500-char bound; explains
  the situation and flags operator action.

`confidence_self="low"` is retained: `WorkerResponse.confidence_self`
is a required `Literal["high","medium","low"]` field on the pydantic
schema, so the assignment is structurally necessary. The value isn't
consumed by `Classifier._persist` (the persisted `confidence` column
is recomputed deterministically from band count via
`promotion.confidence`), so removing the assignment from
`_FALLBACK_RESPONSE` would have required either making the schema
field optional (a wider change with downstream effects on
`parse_worker_response`) or hardcoding a placeholder elsewhere. The
inline comment documents the asymmetry so future editors don't try
to "clean up" the dead field.

### Option A · arc-link empty-headline filter

`musahit/arcs/linker.py` is FILE-PROTECTED. The `/goal` authorises the
specific change: add one `AND` clause to the WHERE in
`_select_pending`. No other modifications.

```sql
WHERE c.arc_id IS NULL
  AND c.final_defcon IS NOT NULL
  AND coalesce(trim(c.headline), '') != ''
```

`trim()` in DuckDB strips spaces only (not tabs/newlines); the filter
catches the historically-observed shape (literal empty strings)
plus spaces-only. Tab/newline-only headlines are not part of the
observed bug and would require a more aggressive filter
(`regexp_replace(c.headline, '\s', '')`) which we deferred — the
classifier-side placeholder is the primary defence; this filter is
defence-in-depth for anything that slips through.

### One-off repair script

`scripts/maintenance/repair_2026_05_25_empty_arcs.py` is a
single-purpose operator tool. Two updates:

* **`arc_20260523_0146`** — recoverable. `cl_20260525_0076` (which
  joined this arc on 2026-05-25) has real Bilgi Üniversitesi content
  (headline + summary). Copy those values into the arc's
  `headline`, `summary`, `last_update_headline`, and
  `last_update_summary` columns.
* **`arc_20260523_0036`** — not recoverable. Its only linked cluster
  (`cl_20260523_0011`) also has empty headline + summary. Write the
  same Turkish placeholder text the classifier fallback now produces.

Idempotence: each arc's current state is read before any UPDATE; if
it already matches the desired state, the script skips the write.
Running the script twice does nothing the second time.

Tripwire compliance: the script touches ONLY the four text columns.
`last_update_at` and `last_update_cluster_id` are explicitly left
unchanged; a post-condition check at the end of the script logs
their values so the operator can verify by eye.

DuckDB FK workaround: UPDATEs on `arcs` fire FK checks for
`arc_centroids` and `clusters` (via `clusters.arc_id`) even when the
updated columns aren't referenced. The repair script implements the
same snapshot → DELETE → UPDATE → re-INSERT dance used by
`ArcLinker._update_arc` (line ~523), with the cluster-level nested
workaround for cluster_articles/cluster_embeddings/promotion_log
mirroring `ArcLinker._update_cluster_arc_id_to_value`. The dance is
explicit (no helper reuse) because the repair script must remain a
self-contained one-off · it doesn't depend on the linker class
internals beyond the SQL shape.

---

## ❯ Mathematical / Statistical Details

No algorithmic content. The filter predicate is a string operation:
`coalesce(trim(headline), '') != ''` — true iff the trimmed value is
non-empty. The repair script's idempotence check is structural
equality between current and desired column values.

---

## ❯ Design Decisions

**Why Option A AND Option B (not just one)?** The investigation
recommended B as the root-cause fix. The matching Option A filter is
defence-in-depth: even with B in place, any historical row with an
empty headline (loaded from a pre-2026-05-25 fallback) would still
land in `_select_pending`'s result set and seed an arc on the next
run. The filter catches those legacy rows. Both fixes are cheap; the
combined defence is strictly more robust than either alone.

**Why placeholder text instead of skipping the fallback persistence
entirely?** The fallback exists precisely so the pipeline never
stalls. Skipping persistence would leave a cluster with
`final_defcon IS NULL`, which the score stage would re-process on
every run (potentially looping on the same LLM bug). The placeholder
text makes the row legitimate-but-flagged; the operator can grep
`category = 'SINIFLANDIRILMADI'` (or hear `"sınıflandırılamadı"` in
the audio) to find these rows for manual review.

**Why a separate repair script instead of a migration?** Migrations
are append-only and apply to every DB the project's `init_db()`
touches. The repair is targeted at two specific arc ids that exist
only in the operator's production DB; a migration would carry no-op
overhead for every fresh DB and the test fixtures. The script is
also more transparent — it prints what it's about to change before
applying, which a migration can't do cleanly.

**Why the FK-workaround dance in the repair script even for non-FK
columns?** DuckDB 1.4.2 fires FK checks on `UPDATE arcs` for every
referencing table (arc_centroids, clusters) regardless of which
column is updated. The dance is the project's established pattern
(see `_update_arc` docstring) and matches the `memory/MEMORY.md` §
"DuckDB FK Update Pattern" convention. Skipping the dance and
relying on a transaction wouldn't help: the FK check fires inside
the transaction too (per DuckDB docs).

**Why not use the linker's helpers from the repair script?** The
linker's `_update_arc` requires `state`, `peak_defcon`, `entity_set`,
and `new_centroid` parameters — values the repair script
deliberately preserves rather than rewrites. Replicating the
workaround inline with a tighter scope (only the headline/summary
column UPDATE) keeps the script understandable in isolation and
matches the "one-off" framing.

**Why the placeholder uses parentheses?** The Turkish parenthetical
signals "machine-generated marker, not a real headline" the same
way `(başlıksız)` did — but the meaning is now
"could-not-be-classified" rather than the generic "untitled".
Operator audio-recognition stays consistent (any
`(parenthetical-thing)` heading = a marker, not a story).

---

## ❯ Verification

```powershell
# Targeted suite: classifier fallback + linker filter + writer renderer
python -m pytest tests/test_score/test_fallback.py tests/test_linker.py `
  tests/test_writer/test_fallback.py tests/test_classifier.py -q
# Expect 53 passed

# Full suite
python -m pytest tests/ -q
# Expect 682 prior + 16 new = 698 passed, 2 skipped (zero regressions)

# Ruff clean on touched files
python -m ruff check musahit/score/classifier.py musahit/arcs/linker.py `
  scripts/maintenance/repair_2026_05_25_empty_arcs.py `
  tests/test_score/test_fallback.py tests/test_linker.py `
  tests/test_writer/test_fallback.py
# Expect "All checks passed!"

# Dry-run the repair script against the production DB
python scripts/maintenance/repair_2026_05_25_empty_arcs.py --dry-run
# Expect: prints current + desired state for both arcs · no DB writes
```

Operator post-deploy steps:

1. Run the repair script in apply mode:
   `python scripts/maintenance/repair_2026_05_25_empty_arcs.py`
2. Verify the two arcs:
   `SELECT id, headline FROM arcs WHERE id IN ('arc_20260523_0036','arc_20260523_0146')`
3. Re-run the writer stage (or wait for the next nightly) so the
   briefing.md and briefing.mp3 reflect the repaired arcs:
   `python -m musahit.pipeline run --date today --stage write --force`
4. Confirm the new briefing has no `(başlıksız)` placeholder for
   these arc ids; voiced Öne Çıkanlar reads the real Bilgi Üniversitesi
   headline for `arc_20260523_0146`.

---

## ❯ Related Docs

- `docs/investigations/2026-05-25-empty-headlines.md` · root cause
  analysis · diagnostic queries · all four fix options with trade-offs
- ADR-005 · score stage promotion (unchanged)
- ADR-008 · story arc model (unchanged)
- ADR-012 · failure isolation (unchanged · placeholder fits the
  graceful-degradation invariant)
- `musahit/score/classifier.py::_persist` · how worker.headline
  flows into clusters.headline
- `musahit/arcs/linker.py::_seed_arc` · how cluster.headline flows
  into arcs.headline
- `musahit/writer/fallback.py::_render_arc` · `or '(başlıksız)'`
  substitution (line 312)

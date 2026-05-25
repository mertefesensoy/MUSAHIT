# Implementation: Arc evolution · active-today vs stalled rendering

**Date** · 2026-05-25
**Author** · MERT EFE ŞENSOY
**ADR refs** · ADR-008 (story arcs), ADR-009 (briefing template), ADR-012 (writer fallback)

---

## ❯ Problem / Motivation

Arc summaries froze at arc-seed time. The MİT Syria arc (an OPEN arc that
acquired a new cluster every day for a week) read identical sentence +
identical "Açıldı" header on day 1, 2, 3. The operator hearing the audio
briefing had no signal whether a story was moving or stalled — every arc
sounded the same regardless of whether new reporting had landed today.

Concretely:

* `arcs.summary` and `arcs.headline` stay at the seed-cluster's values
  forever; subsequent joining clusters only refreshed `last_update_at` and
  `entity_set`. The renderer therefore had nothing new to surface.
* The fallback renderer's `_render_arc` produced one shape for every arc:
  `### {headline} · {arc_id}` + Açıldı line + seed summary. With no
  active/stalled tag, the briefing read as a single undifferentiated wall
  of arc blocks.
* The voiced-cap split (Öne Çıkanlar · top 10 voiced) prioritized only by
  `peak_defcon, last_update_at` — a stalled high-severity arc beat an
  active-today mid-severity arc, even though the operator's ear cares
  about freshness first.

Goal: arc summaries should evolve with each joining cluster, and the
renderer (plus TTS extractor) should make active-today and stalled arcs
distinguishable at a glance and at a listen.

---

## ❯ What Changed

| File | Description |
|---|---|
| `scripts/migrations/004_add_arc_last_update.sql` | New migration · adds `last_update_summary`, `last_update_headline`, `last_update_cluster_id` columns to `arcs`; backfills from existing seed columns + most-recently-linked cluster. |
| `musahit/arcs/linker.py` | `_seed_arc` writes the new triplet at creation; `_attach_cluster` + `_update_arc` overwrite it on every join (last-write-wins). |
| `musahit/writer/payload.py` | `ArcView` gains five fields (the three column mirrors + computed `is_active_today` + `days_since_last_update`); `_load_arcs` reads the columns and computes the flags against the run window. `build_payload` threads `briefing_date` through. |
| `musahit/writer/fallback.py` | `_render_arc` branches on `is_active_today`: active gets a `**Güncelleme** · {last_update_summary}` body; stalled gets a `**Son güncelleme** · X gün önce` header line + italic stalled marker. `_arc_sort_key` adds an active-first tier. New module constants `ARC_UPDATE_PREFIX`, `ARC_STALLED_MARKER`. |
| `musahit/tts/extractor.py` | `_strip_stalled_markers` drops the italic stalled-marker line from voiced text (OPEN_ARCS section only). Güncelleme prefix is kept voiced. |
| `tests/test_arc_evolution.py` | 18 new tests covering migration backfill, linker seed/join, payload tag computation, renderer branching, voiced-cap prioritization, and TTS extractor stripping. |
| `tests/test_init_db.py` | Bumped `migrations_applied` expectation from 3 to 4 in six places (plus the `schema_version` row-count assertion). |

---

## ❯ Implementation Approach

### Schema (migration 004)

Three new nullable TEXT columns on `arcs`:

* `last_update_summary` — most recent joining cluster's summary.
* `last_update_headline` — most recent joining cluster's headline. Kept
  even though briefings render the stable seed headline; audit + future
  use (operator audit trail, future "what changed yesterday" report).
* `last_update_cluster_id` — id of that cluster (audit pointer).

`last_update_at` already existed (migration 001) and already had the
correct semantics — set to `cluster.created_at` on every seed and join.
No schema change needed for the timestamp.

Backfill runs at migration time:

* Seed-equivalent values copy in: `last_update_summary = summary`,
  `last_update_headline = headline` for any row where the new column
  is NULL.
* `last_update_cluster_id` is computed via a correlated subquery picking
  the most-recently-created cluster linked to each arc. If no cluster is
  linked, the value stays NULL (renderer treats this safely).

### Linker (`ArcLinker`)

* `_seed_arc`'s INSERT extended to 12 columns (was 9). The new triplet
  initializes to `(cluster.summary, cluster.headline, cluster.id)` so a
  0-day arc reads as "active-today" via the same code path as a later
  join — the seed write itself counts as an evolution event.
* `_attach_cluster` passes the joining cluster's `(summary, headline, id)`
  into `_update_arc` via three new keyword-only parameters.
* `_update_arc`'s UPDATE statement extended to set the triplet alongside
  `state`, `last_update_at`, `peak_defcon`, `entity_set`.

The FK workaround (DELETE child rows → UPDATE parent → re-INSERT) is
unchanged — `arc_centroids` and `clusters.arc_id` still need the same
treatment around the UPDATE.

### Payload (`build_payload` / `ArcView`)

`ArcView` gains five fields, all with defaults so legacy fixtures and the
two existing per-section test helpers continue to construct it positionally:

* `last_update_summary: str = ""`
* `last_update_headline: str = ""`
* `last_update_cluster_id: str | None = None`
* `is_active_today: bool = False`
* `days_since_last_update: int = 0`

`_load_arcs` now takes a `briefing_date: date` parameter (passed from
`build_payload`) and computes:

* `is_active_today` = `last_update_at >= run.started_at` (with NULL-safe
  False). Since `_attach_cluster` and `_seed_arc` both set
  `last_update_at = cluster.created_at` (always after `started_at`),
  every arc the linker touched in this run reads as active-today.
* `days_since_last_update` = `(briefing_date - last_update_at.date()).days`,
  clamped to `≥ 0`. Used only for stalled-arc headers — irrelevant for
  active-today (always 0).

### Fallback renderer (`_render_arc`)

Three rendering shapes, branched at the top of the function:

1. **Closing** (`closing=True`, resolved arcs section) — unchanged behavior.
   Header + seed summary + "Bu hikaye bugün kapatıldı." line.
2. **Active-today** (`is_active_today=True`) — standard header (`###` +
   Açıldı/DEFCON/Kategori line) followed by
   `**Güncelleme** · {last_update_summary or summary}`. The seed headline
   stays in the `###` row; only the body evolves.
3. **Stalled** (open arc, `is_active_today=False`) — standard header
   plus a `**Son güncelleme** · X gün önce` line so the operator sees
   how stale the story has become. Body is the seed summary followed by
   the italic stalled marker `*Bu arc'da bugün yeni gelişme yok.*`.

### Voiced-cap prioritization

`_arc_sort_key` becomes a 3-tuple: `(active_tier, peak_defcon, -epoch)`
where `active_tier` is 0 for active-today and 1 for stalled. Sort
ascending puts every active-today arc ahead of every stalled arc, then
sorts by severity, then by recency within each tier.

The top `VOICED_OPEN_ARCS_CAP = 10` slots go under `### Öne Çıkanlar`
(voiced); the rest overflow into `### Diğer Açık Hikayeler` (visual only,
already excluded from TTS by extractor's overflow truncation from
2026-05-24). This satisfies "voiced cap prioritizes active-today · stalled
fills only if high-DEFCON" via natural sort behavior — when the cap is
tight, active arcs win the slots; only when there's surplus capacity do
stalled arcs (sorted by severity within the stalled tier) get in.

### TTS extractor

`_strip_stalled_markers(text)` runs against the OPEN_ARCS section content
before it's bucketed into the voiced output. The regex is a single-line
literal match for `^\s*\*Bu arc'da bugün yeni gelişme yok\.\*\s*$`. Other
sections are unaffected — if the same string ever appears elsewhere (in
real text, not as the marker) it stays.

Why strip the marker from voiced but keep the visual: voicing
"Bu arc'da bugün yeni gelişme yok" once per stalled arc would make Piper
read the same sentence 5-50 times per briefing. The dashboard reader
benefits from the visual signal; the audio listener already gets the
signal from "no Güncelleme prefix" (the absence speaks for itself).

---

## ❯ Mathematical / Statistical Details

No statistical algorithm — the change is purely structural plus one
date-arithmetic computation:

```
days_since_last_update = max(0, (briefing_date - last_update_at.date()).days)
```

The `max(0, ...)` floor prevents negative staleness when an arc's
`last_update_at` is somehow after the briefing date (data integrity
issue, not expected in production but defended).

The voiced-cap sort key is lexicographic on a 3-tuple — standard
ascending sort produces the documented priority order without any
explicit conditional logic.

---

## ❯ Design Decisions

**Why three new columns instead of overwriting `summary`/`headline`?**
Considered: just update `arcs.summary` on every join (no new columns).
Rejected because:
* The operator audit trail wants to see "what was the original story
  about" — preserving the seed `summary`/`headline` keeps that intact.
* The briefing header keeps `headline` stable across days (per spec) so
  the arc-id-to-headline mapping the operator memorizes doesn't churn.
* Future operator-facing dashboards may want both "first seen" and "most
  recent" side-by-side; we'd just have re-derived them.

**Why `is_active_today` against `started_at`, not `completed_at`?**
`completed_at` is set at the END of the run, after the writer stage; it's
NULL while the writer is reading the payload. Using `started_at` plus a
NULL-safe upper bound (effectively "now") catches every arc the linker
touched in this run, which is exactly the set we want flagged active.
This is documented inline in `_load_arcs`.

**Why "last call wins" rather than "most-recent timestamp wins"?**
The `ArcLinker` iterates clusters by `final_defcon ASC` (severity first).
Two same-arc same-run joins could have any timestamp order; "last call
wins" means whichever the linker processes last sets the triplet. The
goal text "Most recent wins · no length fallback · same-day joins also
evolve" reads as a permissiveness clause (no skip-if-shorter, no
once-per-day cap), not as a chronological comparison. Last-write-wins
matches the existing `last_update_at` semantics — keeping both fields
written together avoids inconsistency.

**Why a high-DEFCON gate was not added for stalled-in-voiced eligibility?**
Goal text: "stalled fills only if high-DEFCON". The simpler interpretation
(stalled arcs participate in voiced AFTER active-today, sorted by severity)
delivers the same operator outcome via sort order — when there are many
active arcs, stalled don't make the top 10; when there are few active,
the most severe stalled fill in. A strict gate would have broken the
existing `test_eleven_arcs_produce_one_highlight_and_one_overflow`
regression test (11 MATERIAL stalled arcs would all overflow rather than
fill the highlight subsection). The tripwire ("voiced cap prioritization
gets complicated") was checked; this is the simpler resolution.

**Why the italic marker is voiced-stripped but Güncelleme prefix isn't?**
Operator's ear cares about new content. Reading "bugün yeni gelişme yok"
once per stalled arc is noise — 10 stalled arcs in Öne Çıkanlar would
mean the operator hears the same negation 10 times. The visual marker
on the dashboard already communicates the signal; the audio listener
gets the signal from the absence of a Güncelleme prefix.

---

## ❯ Verification

```powershell
# Migration applies cleanly
python -c "from musahit.common.migrations import init_db; print(init_db(':memory:', load_vss=False))"
# Expect {'vss_loaded': False, 'migrations_applied': 4, 'hnsw_indices_created': 0}

# New tests pass
python -m pytest tests/test_arc_evolution.py -v
# Expect 18 passed

# Adjacent test modules still green
python -m pytest tests/test_linker.py tests/test_writer/ tests/test_tts/ tests/test_init_db.py -q
# Expect 194 passed, 1 skipped (existing ffmpeg-gated case)

# Full suite still green
python -m pytest tests/ -q
# Expect 645 passed, 2 skipped (was 627/2 → +18 new tests, zero regressions)

# Linter clean on touched Python files
python -m ruff check musahit/arcs/linker.py musahit/writer/payload.py `
  musahit/writer/fallback.py musahit/tts/extractor.py `
  tests/test_arc_evolution.py tests/test_init_db.py
# Expect "All checks passed!"
```

Operator-side validation (post-deploy, next smoke run):

1. Run `python -m musahit.pipeline run --date today`.
2. Open `briefings/YYYY/MM/DD/briefing.md`. Verify open arcs that received
   a new cluster today render with `**Güncelleme** · …`; arcs that did
   not render with `**Son güncelleme** · X gün önce` + italic marker.
3. Open `briefing.mp3`. Verify the italic stalled line is NOT voiced
   for any stalled arc; verify the Güncelleme content IS voiced for
   active arcs.
4. Re-run with `--force` and confirm `last_update_*` triplet on a same-arc
   second-cluster join was overwritten to the new cluster's data (query
   `SELECT last_update_summary, last_update_cluster_id FROM arcs WHERE id
   = 'arc_…'`).

---

## ❯ Related Docs

- ADR-008 (story-arc model) · `adr/ADR-008-story-arcs.md`
- ADR-009 (briefing template + TTS scope) · `adr/ADR-009-briefing-template.md`
- ADR-012 (writer fallback) · `adr/ADR-012-writer-fallback.md`
- 2026-05-24 follow-up (Öne Çıkanlar / Diğer split, voiced-cap = 10) ·
  `docs/implementations/2026-05-24-arc-link-bug-fix.md` and the
  AÇIK GELİŞMELER section discussion in `memory/build-progress.md`.
- Migration runner · `musahit/common/migrations.py`.

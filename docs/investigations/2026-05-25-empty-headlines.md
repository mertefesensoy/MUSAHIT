# Investigation: empty-headline arcs surfacing in the voiced briefing

**Date** · 2026-05-25
**Author** · diagnostic investigation per /goal
**Mode** · READ-ONLY · no code changed, no migrations run, no pipeline executed.

---

## ❯ Summary

Two arcs (`arc_20260523_0036`, `arc_20260523_0146`) render with the
placeholder `(başlıksız)` headline because the clusters that seeded them
hit the **score stage's fallback path** on the original 2026-05-23 smoke
run — when the worker LLM returns malformed JSON `max_retries + 1` times,
`musahit/score/classifier.py::_FALLBACK_RESPONSE` writes the cluster with
`headline=""` / `summary=""` (literal empty strings, not NULL). The
arc-link stage then seeded an arc from those empty fields, and migration
004's backfill copied the same empty strings into the new
`last_update_*` columns. `arc_20260523_0146` leaks into voiced
*Öne Çıkanlar* today because a second cluster (`cl_20260525_0076`,
re-classified successfully today) joined the arc and pushed
`last_update_at` into today's run window, flipping `is_active_today=True`
— but the linker code that copied `cluster.headline/summary` into the
arc's `last_update_*` triplet only landed in this very session and has
not yet run against the production DB. The fault is in the score stage
(empty fallback content), with the visibility hop via the renderer that
treats empty strings as falsy and substitutes `(başlıksız)`.

**Scope is contained**: only 2 empty-headline arcs (both with empty
summary too); 0 empty-category arcs; 4 empty-headline clusters upstream
total. Categories are properly defaulted to `SINIFLANDIRILMADI`. No
hidden pattern of broader corruption — the bug is exactly what the
symptom describes.

---

## ❯ DB State

### arcs with empty/NULL headline

Total arcs in DB: **431**. Empty/NULL headline: **2**. Empty/NULL
summary: **2** (same rows). Empty/NULL category: **0**.

| field | `arc_20260523_0036` | `arc_20260523_0146` |
|---|---|---|
| `headline` | `''` (empty string) | `''` (empty string) |
| `summary` | `''` | `''` |
| `last_update_headline` | `''` | `''` |
| `last_update_summary` | `''` | `''` |
| `state` | `OPEN` | `OPEN` |
| `peak_defcon` | `5` (AMBIENT) | `4` (GÜNDEM) |
| `category` | `SINIFLANDIRILMADI` | `SINIFLANDIRILMADI` |
| `created_at` | `2026-05-23 21:48:52.640558` | `2026-05-23 21:49:00.405842` |
| `last_update_at` | `2026-05-23 21:32:41.535894` | **`2026-05-25 07:20:46.878349`** |
| `last_update_cluster_id` | `cl_20260523_0011` | `cl_20260525_0076` |
| `entity_set` | `[]` (empty JSON array) | `["AYM","Recep Tayyip Erdoğan","Resmî Gazete"]` |

The `last_update_at` for `arc_20260523_0146` falls inside the current
run window (`run_20260525.started_at = 2026-05-25 07:17:11`), so
`payload.is_active_today` evaluates to `True` for it. That single flag
is what promotes it into the active-today sort tier in the voiced cap.

### schema state

`schema_version`:
- v1 `initial_schema` applied 2026-05-24 00:28:22
- v2 `add_article_metadata` applied 2026-05-24 00:28:22
- v3 `add_failed_stages` applied 2026-05-24 00:28:22
- **v4 `add_arc_last_update` applied 2026-05-25 13:15:56**

The v4 migration applied AFTER today's pipeline run (which completed at
09:17:02). Confirmed by the diagnostic detector
`last_update_* == seed values` → **431 of 431 arcs** match. Zero arcs
show the linker-overwritten shape. The new linker code path has not yet
executed against production data.

### upstream clusters

Total clusters in DB: **487**. Empty/NULL headline: **4**. The 4:

| cluster_id | arc_id | category | confidence | seed/join |
|---|---|---|---|---|
| `cl_20260523_0011` | `arc_20260523_0036` | SINIFLANDIRILMADI | ORTA | seed (the only cluster on that arc) |
| `cl_20260523_0049` | `arc_20260523_0146` | SINIFLANDIRILMADI | YÜKSEK | seed (later joined by `cl_20260525_0076`) |
| `cl_20260523_0043` | `arc_20260523_0002` | SINIFLANDIRILMADI | YÜKSEK | join (arc was seeded by a different, non-empty cluster) |
| `cl_20260525_0010` | `arc_20260523_0067` | SINIFLANDIRILMADI | YÜKSEK | join (same) |

The other 2 empty-headline clusters (`0043`, `0010`) joined arcs whose
seed cluster had a real headline, so the arc.headline shows the seed
value, not the empty join value. The arc-link's existing
`_attach_cluster` behavior (headline/summary stable across joins) hides
those empty-cluster joins from the briefing — only the **seed-time**
empties surface.

### second-cluster on the active-today arc

`arc_20260523_0146`'s second cluster, `cl_20260525_0076`, was scored
successfully:

- `headline`: `"Erdoğan İstanbul Bilgi Üniversitesi'ni yeniden açtı"`
- `summary`: `"Başkan Erdoğan, İstanbul Bilgi Üniversitesi'nin faaliyet izninin kaldırılmasına dair kararı geri çekti ve okul yeniden açıldı."`
- `category`: `POLİTİKA`
- `final_defcon`: `4`
- `confidence`: `YÜKSEK`
- 11 member articles across 5 bands (gov_aligned, opposition,
  independent, international, centrist).

Yet none of this content reaches the arc. The arc still reads as
empty because `_attach_cluster` (the OLD version that was deployed when
run_20260525 actually ran) only updated `last_update_at`, not the
`last_update_*` triplet (those columns didn't exist yet — migration 004
applied 4 hours later). Migration 004's backfill then set
`last_update_summary = arc.summary` (the empty string), not the
`cl_20260525_0076.summary` value the rule would have produced if the
linker had run after the migration.

---

## ❯ Code Path Trace

### 1) Score stage · the empty-fallback origin

`musahit/score/classifier.py:362-371`:

```python
_FALLBACK_RESPONSE: WorkerResponse = WorkerResponse(
    defcon=int(DEFCON.AMBIENT),
    category=Category.UNCLASSIFIED,
    confidence_self="low",
    entities=[],
    summary="",       # ← empty string, not None
    headline="",      # ← empty string, not None
)
```

`_classify_one` (line 110) calls `parse_worker_response(raw)`; on
`ValidationError` it retries up to `max_retries` (default 2 →
3 attempts total). After exhaustion it returns
`(_FALLBACK_RESPONSE, True)` and `_persist` (line 199) writes the
empty `headline` and `summary` into the `clusters` row via the
`UPDATE clusters SET headline = ?, summary = ?, ...` statement at
line 250-262.

The `confidence` column on the affected clusters is **not** `"DÜŞÜK"`
(the fallback's `confidence_self="low"`) because `_persist` recomputes
confidence deterministically from band count via
`promotion.confidence(bands_set, ...)` at line 215. That's why
`cl_20260523_0011` shows `confidence=ORTA` (1 band, 16 sources) and
`cl_20260523_0049` shows `confidence=YÜKSEK` (4 bands) despite being
fallback rows. **The `_FALLBACK_RESPONSE.confidence_self` value is
effectively dead code** — the deterministic recomputation always
overrides it. This is consistent with the design but is the reason the
empty rows are otherwise indistinguishable from real scored clusters.

### 2) Arc-link · seed and join paths

`musahit/arcs/linker.py::_seed_arc` (line 359) INSERTs:

```python
INSERT INTO arcs (..., headline, summary, ...) VALUES (?, ?, ..., ?, ?, ...)
# values: cluster.headline, cluster.summary, ...
```

No NULL guard, no fallback heuristic. Whatever the cluster's headline
is, the arc gets. Empty string in → empty string stored. The new
arc-evolution code (added today, this session) extends the INSERT to
also write `last_update_summary`, `last_update_headline`,
`last_update_cluster_id` — also straight from the cluster fields, also
without a NULL guard.

`_attach_cluster` (line 303): does **not** modify `headline` or
`summary` of an existing arc — the seed values are stable across days
by design (per the 2026-05-25 arc-evolution spec). Empty seeds therefore
stay empty forever, even when a later non-empty cluster joins.

`_select_pending` (line 221) selects all clusters with `c.arc_id IS NULL`
and `c.final_defcon IS NOT NULL` — fallback clusters have a
`final_defcon` (5 = AMBIENT) so they are NOT skipped. There is no
filter on `c.headline != ''`.

### 3) Migration 004 · backfill that froze the bug

`scripts/migrations/004_add_arc_last_update.sql`:

```sql
UPDATE arcs SET last_update_summary = summary WHERE last_update_summary IS NULL;
UPDATE arcs SET last_update_headline = headline WHERE last_update_headline IS NULL;
UPDATE arcs SET last_update_cluster_id = (
    SELECT c.id FROM clusters c WHERE c.arc_id = arcs.id
    ORDER BY c.created_at DESC LIMIT 1
) WHERE last_update_cluster_id IS NULL;
```

For the affected arcs the backfill ran today (after the run) and set:
- `last_update_summary = ''` (from `arcs.summary = ''`).
- `last_update_headline = ''` (from `arcs.headline = ''`).
- `last_update_cluster_id = cl_20260525_0076` (correct most-recent
  cluster), but the summary/headline came from `arcs.*`, NOT from
  `clusters.*` — so the cluster's real content was never copied.

This is consistent with the migration's documented behavior ("seed-time
headline/summary become the initial last_update_*"), but it means
arcs whose seed was a score-stage fallback have **no path** to
acquiring real content via backfill alone. Only a subsequent
post-migration arc-link run (using the new linker code that writes the
triplet from the joining cluster) would fix this.

### 4) Writer · payload + fallback rendering

`musahit/writer/payload.py::_load_arcs` (today's version) computes:

```python
is_active_today = last_update_at is not None and last_update_at >= run_started
days_since_last_update = max((briefing_date - last_update_at.date()).days, 0)
```

`arc_20260523_0146`: `last_update_at = 2026-05-25 07:20:46.878349`,
`run_started = 2026-05-25 07:17:11.538443` → `is_active_today=True`.

`arc_20260523_0036`: `last_update_at = 2026-05-23` →
`is_active_today=False`. days_since_last_update = ~2.

**No headline guard** at the payload layer · the ArcView is created
regardless of `headline == ''`.

`musahit/writer/fallback.py::_arc_sort_key` (today's version):

```python
def _arc_sort_key(arc):
    active_tier = 0 if arc.is_active_today else 1
    dt = arc.last_update_at or arc.created_at
    epoch = dt.timestamp() if dt is not None else 0.0
    return (active_tier, int(arc.peak_defcon), -epoch)
```

`arc_20260523_0146` sort key: `(0, 4, -<epoch_today>)`. With
`active_tier=0`, it sorts AHEAD of every stalled arc regardless of
severity. The voiced-cap split (`VOICED_OPEN_ARCS_CAP = 10`) puts
the first 10 sorted arcs into Öne Çıkanlar; today's briefing.md shows
0146 at position 9 in that subsection (visible directly in
`briefings/2026/05/25/briefing.md`).

`_render_arc` (line 286) at the active-today branch:

```python
if arc.is_active_today:
    update_body = arc.last_update_summary or arc.summary
    if update_body:
        lines.append("")
        lines.append(f"{ARC_UPDATE_PREFIX} {update_body}")
```

For 0146 both `last_update_summary` and `summary` are `''`, so
`update_body == ''` (falsy) and **no body line is appended**. The
rendered block is just the header (`### (başlıksız) · arc_20260523_0146`)
plus the metadata line (`**Açıldı** · 23 Mayıs 2026 · **Zirve DEFCON** ·
GÜNDEM · **Kategori** · SINIFLANDIRILMADI`). The placeholder substitution
at line 312 fires: `arc.headline or '(başlıksız)'`.

`arc_20260523_0036` (stalled): `_arc_sort_key` returns
`(1, 5, -<epoch_2026-05-23>)` — far down the sort. It does NOT make
the top-10 cap and lands in `### Diğer Açık Hikayeler` as the one-line
bullet form `- (başlıksız) · AMBİYANS · SINIFLANDIRILMADI ·
\`arc_20260523_0036\`` (rendered by `_render_arc_overflow_bullet` at
line 167, which also uses `arc.headline or '(başlıksız)'`).

### 5) TTS extractor

`musahit/tts/extractor.py::extract_voiced_briefing` only filters out:
1. Lines matching the `**Kaynaklar** · …` regex (DEFCON 3 section).
2. The `### Diğer Açık Hikayeler` subsection and everything after
   inside the OPEN_ARCS section.
3. The italic stalled marker `*Bu arc'da bugün yeni gelişme yok.*`.

No filter for `(başlıksız)` text or empty-headline content. The
`### (başlıksız) · arc_20260523_0146` header line and its
`**Açıldı** · …` metadata line **will flow into Piper's TTS scope**.
The preprocessor strips `###` markdown and may pronounce "başlıksız"
literally (Turkish: ~"headline-less"). Confirmed by inspecting
today's `briefing.md`: 0146's block sits at index 9 of Öne Çıkanlar,
above the `### Diğer Açık Hikayeler` line, so the extractor's overflow
truncation does NOT save it.

### Today's briefing.md state (excerpt)

```
### Sakarya'da kayıp kadının cesedi bulunma · arc_20260525_0006
...

### (başlıksız) · arc_20260523_0146
**Açıldı** · 23 Mayıs 2026 · **Zirve DEFCON** · GÜNDEM · **Kategori** · SINIFLANDIRILMADI

### Bilgi Üniversitesi Protestosunda Biber Gazlı Polis İhtiyaçları · arc_20260525_0007
...

### Diğer Açık Hikayeler

- ...
- (başlıksız) · AMBİYANS · SINIFLANDIRILMADI · `arc_20260523_0036`
- ...
```

The 0146 block is voiced, the 0036 bullet is not. Note that 0146 has
no body content at all in today's briefing — the OLDER renderer that
shipped on disk for run_20260525 (pre-arc-evolution) had a different
shape, but the empty-headline + empty-body output is the same under both
old and new renderers.

---

## ❯ Upstream Check

The score stage's worker LLM (Qwen2.5 7B) failed to produce valid JSON
for these 4 clusters on the 2026-05-23 smoke run (`run_20260525.counts`
shows `clusters_score_fallbacks: 1` for today; the 2026-05-23 run isn't
in `pipeline_runs` history but the cluster created_at timestamps
(2026-05-23 21:32) confirm they came from the original smoke run).

The fallback fired and wrote empty content. The clusters look
otherwise healthy:

- `cl_20260523_0011` · 16 NTV "Son dakika deprem mi oldu?" articles,
  all 200 OK, word_count 100-150 each. Daily auto-generated "did an
  earthquake happen today" listings — high-volume, low-information,
  templated headlines. The worker model likely produced a non-JSON
  reply or a schema-violating reply (e.g. summary > 500 chars,
  forbidden category, garbled Turkish character).
- `cl_20260523_0049` · 7 articles across 4 bands about the Bilgi
  Üniversitesi closure. Content is rich and clearly news-worthy
  (one article is 501 words). The worker's failure is more
  surprising; could be hitting prompt token limits or a specific
  Turkish-character validation issue.

The DB doesn't preserve the worker's failed raw responses (no
`worker_log` table; `score_worker_invalid_response` is a structured
debug log only). Reproducing the exact LLM failure would require
re-running the score stage with the original cluster inputs and
inspecting the live LLM outputs. Out of scope for read-only
investigation.

**Categorical observation**: every empty-headline cluster has
`category = SINIFLANDIRILMADI` (Turkish for UNCLASSIFIED · the
fallback's `category=Category.UNCLASSIFIED.value`). The category
column is fine — it's a populated string, just the placeholder.
This means a hypothetical UI/operator filter `WHERE category =
'SINIFLANDIRILMADI'` would catch every fallback row, including
empty-headline ones, and could surface them for manual review.

---

## ❯ Fix Options

All four options below are described as proposals only · no code has
been changed. Each scope statement is explicit about which file(s)
would be touched.

### Option A · Defensive arc-link · skip seeding when cluster has no headline

**Scope**: `musahit/arcs/linker.py::_select_pending` adds
`AND coalesce(trim(c.headline), '') != ''` to the WHERE clause (or a
post-fetch Python filter). New 1-line filter; no schema change.

**Effect**: clusters with empty headlines never become arc seeds. They
sit forever in `clusters` with `arc_id IS NULL`. The two existing
empty-headline arcs persist unless cleaned up by a separate operator
script.

**Trade-offs**: simple, minimal blast radius. Loses signal: a cluster
with empty headline can still represent real content (16 NTV articles
about a real seismic period IS real news, just badly-described by the
LLM). The fallback-content arc would be lost forever rather than
recoverable by future re-scoring.

### Option B · Score-stage fallback writes a placeholder headline/summary

**Scope**: `musahit/score/classifier.py::_FALLBACK_RESPONSE` changes
the literal empty strings to descriptive placeholders, e.g.
`headline="(otomatik sınıflandırma başarısız)"`,
`summary="Skorlayıcı modeli bu kümede geçerli yanıt üretemedi. Operatör incelemesi gerekli."`.
Also potentially: feed the worker's failure reason into the placeholder
text for operator audit.

**Effect**: every fallback row has a recognizable Turkish marker in
the briefing instead of `(başlıksız)`. Operator can grep
"otomatik sınıflandırma" in nightly briefings to find affected arcs.
Arc-link still seeds normally; the renderer never substitutes the
generic placeholder.

**Trade-offs**: a placeholder header in a Turkish briefing is visible
to the listener (a value judgment about whether voicing it is better
than voicing "başlıksız" — at least it's an honest signal). Risks
the LLM "learning" the placeholder as a valid headline if any future
fine-tune mixes training data from these briefings. Doesn't address
the existing 2 empty arcs (those need a separate fix or backfill).

### Option C · Writer-side filter · drop empty-headline arcs from voiced cap

**Scope**: `musahit/writer/fallback.py::_render_open_arcs` adds a
filter before the sort: skip any arc where
`coalesce(arc.headline.strip(), arc.last_update_headline.strip(), '') == ''`.
Optionally relegate those arcs to the Diğer overflow with a different
bullet shape, or drop them from the briefing entirely.

**Effect**: empty-headline arcs no longer appear in voiced Öne Çıkanlar.
Either they vanish from the briefing OR they appear in the Diğer
bulleted tail (which the TTS extractor already drops from voiced text).
The DB stays as-is; the score-stage bug is masked at the render layer.

**Trade-offs**: cleanest TTS outcome — the operator never hears
"başlıksız". But the empty-headline arc still consumes a voiced-cap
slot if the filter happens AFTER sort-and-slice, OR the cap fills with
one more real arc if filtered BEFORE. Hiding the symptom doesn't fix
the score-stage fallback; future runs keep producing empty arcs until
the upstream is addressed. Visible only in markdown audit, not in
the audio.

### Option D · Operator-curated re-score path · resurface empties for manual triage

**Scope**: new `scripts/triage/list_unclassified_arcs.py` (operator
tool, NOT a code change to the pipeline). Lists every arc whose
`category = 'SINIFLANDIRILMADI'` with cluster headlines / article
titles + member URLs, so the operator can hand-classify or feed a
fresh classify call. Optionally a `manual_overrides` row updates the
arc's headline + category.

**Effect**: nothing changes automatically. Operator audits the
fallback set after each nightly run, decides which arcs are real news
and need a real headline, applies overrides manually. Two existing
arcs get fixed by the next operator review.

**Trade-offs**: zero risk of breaking other code paths. Requires
operator labor every run. Doesn't prevent the next empty-headline
arc; doesn't change voiced behavior for THIS run. Works in concert
with one of A/B/C; useful as a longer-term remediation backbone but
not a standalone solution.

### Recommendation

**Option B + a one-off operator cleanup**. Reasoning:

1. The score stage IS where the bug originates · fixing it there
   stops new empty arcs from being created without changing arc-link's
   semantics (which the arc-evolution work just stabilized).
2. A placeholder like "(otomatik sınıflandırma başarısız)" is a
   honest, audible signal for the operator listening to the briefing
   · the marker tells them "this is a known classification gap, not
   a real headline-less story". Voiced "otomatik sınıflandırma
   başarısız" reads naturally in Turkish.
3. The two existing empty arcs can be repaired with a one-off
   `scripts/triage/repair_empty_headline_arcs.py` that either:
   - Re-runs the score stage on `cl_20260523_0011` and
     `cl_20260523_0049` (operator-driven, opportunistic);
   - OR sets the arc.headline + summary to the same placeholder so
     today's briefing isn't pinned to "(başlıksız)" forever.
4. Option A is too aggressive · 16 real NTV articles shouldn't be
   thrown away because the LLM had a bad day.
5. Option C masks the bug at the wrong layer · the briefing would
   look clean but the underlying clusters would still pile up as
   ghosts in the DB, gradually inflating the empty-arc count.

The cleanup script (point 3) would also bridge any gap before the
new arc-link's `last_update_*` triplet write fires on a fresh run ·
it can directly set arc.summary or last_update_summary from
`cl_20260525_0076.summary` for `arc_20260523_0146`, restoring the
Bilgi Üniversitesi content the briefing should be voicing today.

A FUTURE Option D-style operator audit script is a good companion
once the immediate voiced-briefing issue is resolved, but doesn't
need to land in the same change as B.

---

## ❯ Tripwires Status

- **Fix attempts during investigation** · NOT TRIGGERED · no code
  changed, no migrations run, no pipeline executed.
- **Code changes proposed inline** · NOT TRIGGERED · options are
  scoped per file with no diffs.
- **Investigation scope expanded** · NOT TRIGGERED · stayed within
  the empty-headline arcs (2 arcs, 4 upstream clusters). The category
  defaults to `SINIFLANDIRILMADI` correctly · no empty-category bug.
  Confidence values match the deterministic-recomputation contract ·
  not a separate bug.
- **Bigger pattern suspected** · NOT TRIGGERED · the empty fallback
  is the single root cause. 0 empty categories. The fact that
  `_FALLBACK_RESPONSE.confidence_self="low"` is overridden by the
  deterministic confidence recompute is a latent dead-code smell
  (Option B would also clean that up by removing the now-meaningless
  `confidence_self` field assignment), but it's not its own bug.

---

## ❯ Related Code Locations

- `musahit/score/classifier.py:362-371` · `_FALLBACK_RESPONSE`
- `musahit/score/classifier.py:110-135` · retry + fallback path
- `musahit/score/classifier.py:249-262` · UPDATE clusters with worker
  values (including the empty fallback)
- `musahit/arcs/linker.py:359-435` · `_seed_arc` (writes empty
  headline/summary if cluster has them)
- `musahit/writer/payload.py:_load_arcs` · `is_active_today`
  computation (~line 240)
- `musahit/writer/fallback.py:139-167` · `_arc_sort_key` + voiced-cap
  split
- `musahit/writer/fallback.py:286-360` · `_render_arc` placeholder
  substitution (line 312)
- `musahit/tts/extractor.py:127-152` · voiced-section filters (no
  empty-headline guard)
- `scripts/migrations/004_add_arc_last_update.sql` · backfill that
  copies arc empty values into `last_update_*`

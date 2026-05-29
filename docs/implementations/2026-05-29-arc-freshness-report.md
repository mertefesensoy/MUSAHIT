# Implementation: Arc Freshness & Lifecycle (Group A)

**Date** · 2026-05-29
**Author** · autonomous cycle (per `2026-05-29-arc-freshness-brief.md`)
**ADR refs** · ADR-004 (DEFCON), ADR-008 (arc lifecycle), ADR-009 (briefing template), ADR-012 (writer/TTS)

---

## ❯ Problem / Motivation

The 2026-05-29 briefing surfaced stale-but-real arcs as if current and fabricated two narrative sections:

1. **No freshness axis.** DEFCON encodes *severity* (correct — a serious story stays serious) but nothing tracked that an arc had received no new source for days. The MİT-Suriye arc (DEFCON 3) looked identical to fresh news on day 4.
2. **No recency markers.** DEFCON 4 listed arcs seeded May 23-24 with no age indication — multi-day-old developments presented as today's agenda.
3. **Open-arc backlog of 875.** The `open → watch → resolved` lifecycle never pruned dormant threads.
4. **Mode-4 narrative hallucination.** AÇIK GELİŞMELER fabricated an İş Bankası loan story; AMBİYANS produced a Guantanamo essay — neither grounded in payload data.
5. **`transitions.py` FK bug.** `tests/test_linker.py::TestStopwordOnlyOverlap` failed with a DuckDB foreign-key `ConstraintException`; the lifecycle cleanup pass crashed, which is *why* arcs never resolved (#3).

The fix adds a **freshness axis** (separate from DEFCON severity) driven by each arc's `last_update_at`, makes the itemized narrative sections **deterministic**, **skips dormant arcs from the voice briefing**, and **fixes the transitions FK bug** so the lifecycle can resolve expired arcs.

---

## ❯ What Changed

| File | Description |
|---|---|
| `musahit/arcs/freshness.py` | **New.** Pure `FRESH/DORMANT/EXPIRED` classifier, calendar-day math, Turkish recency labels (`bugün`/`dün`/`N gün önce`), and the tunable `DORMANCY_DAYS=2` / `EXPIRE_DAYS=7` constants — the single source of truth for the surfacing signal. |
| `musahit/arcs/transitions.py` | Rewritten: fixed the FK bug (root cause documented below) and replaced the 7d/30d ladder with calendar-day **auto-resolution at `EXPIRE_DAYS`** (OPEN/WATCH idle ≥7d → RESOLVED) so the backlog drains. |
| `musahit/writer/payload.py` | `ArcView`/`ClusterView` now carry `freshness` + `days_since_last_update`; `_load_arcs` loads **all OPEN arcs** (not just this-run ones) so DORMANT arcs surface; `_load_clusters` computes per-cluster recency from the linked arc. |
| `musahit/writer/render.py` | **New.** Deterministic renderers for the five itemized sections (open arcs, DEFCON 4, social-only, ambient, resolved) with recency suffixes, freshest-first sort, EXPIRED excluded, and an Öne Çıkanlar/Diğer voiced-cap split. |
| `musahit/writer/briefer.py` | Only DEFCON 1-2 (0) and DEFCON 3 (1) call the LLM; sections 2-6 render deterministically (no LLM → no Mode-4 fabrication). Empty/all-EXPIRED short-circuit preserved. |
| `musahit/tts/preprocessor.py` | Drops DORMANT (`N gün önce`, N≥2) arc lines from the spoken text, keeps FRESH (`bugün`/`dün`); all-dormant block → brief spoken note; strips backticks; arc-id rewrite untouched. |
| `tests/test_arcs_freshness.py` | **New.** Unit tests for the classifier + boundary table + recency labels. |
| `tests/test_transitions.py` | Rewritten for expiry-based resolution + the FK regression (arc with `arc_centroids` + referencing cluster resolves without an FK error). |
| `tests/test_writer/test_render.py` | **New.** Recency, freshest-first sort, EXPIRED exclusion, all-EXPIRED → None, voiced-cap split, extractor-coupling guard. |
| `tests/test_writer/test_payload.py` | Updated: all-OPEN surfacing + per-arc freshness classification. |
| `tests/test_writer/test_briefer.py` | Updated: 2 LLM sections (0,1); new no-LLM-leak proof for the itemized sections. |
| `tests/test_tts/test_preprocessor.py` | New dormancy-skip tests (drop/keep/note/line-end-anchored/arc-id). |
| `tests/test_arc_freshness_integration.py` | **New (Tier 2).** Real arcs lifecycle + real writer + real TTS over arcs at today/1d/3d/8d ages. |

**FILE-PROTECTED, untouched:** `musahit/score/defcon.py`, `musahit/ingest/sources.py`, `musahit/ingest/poller.py`.

---

## ❯ Implementation Approach

**Part 1 · Arcs.** `arc_last_update` (the schema-001 `arcs.last_update_at` column, extended by migration 004's `add_arc_last_update`) was already bumped on every join by the linker; verified and regression-tested. `freshness.py` adds a pure classifier. `transitions.py` now resolves any OPEN/WATCH arc idle ≥ `EXPIRE_DAYS` calendar days to RESOLVED, draining the backlog through the normal lifecycle.

**Part 2 · Writer.** The payload carries per-arc freshness; the open-arc query was broadened from "arcs touched this run" to **all OPEN arcs** so dormant threads surface. `render.py` renders the five itemized sections deterministically (recency suffix on every line, freshest-first, EXPIRED excluded, DORMANT kept). The briefer routes only DEFCON 1-2/3 through the LLM.

**Part 3 · TTS.** The preprocessor drops dormant lines from the Piper-bound text only; the on-disk markdown is never rewritten. An all-dormant block collapses to one spoken note. A freshest-first **Öne Çıkanlar/Diğer** voiced cap (reusing the extractor's existing truncation marker) bounds the voiced set so Piper isn't handed hundreds of lines at once.

---

## ❯ Mathematical / Statistical Details

**Freshness classification (calendar-day based).** Let `d = (now.date() − last_update.date()).days`, clamped to `d ≥ 0` (a `NULL` last-update is treated as `d = 0`). With locked `dormancy = 2` and tunable `expire = 7`:

```
d < dormancy            → FRESH      (d ∈ {0, 1})
dormancy ≤ d < expire   → DORMANT    (d ∈ {2, 3, 4, 5, 6})
d ≥ expire              → EXPIRED    (d ≥ 7)
```

Boundary tests pin `1→FRESH, 2→DORMANT, 6→DORMANT, 7→EXPIRED`.

**Recency label** `r(d)`: `d ≤ 0 → "bugün"`, `d = 1 → "dün"`, `d ≥ 2 → "{d} gün önce"`. Because `d = 1` always maps to "dün", the string `"N gün önce"` is emitted only for `N ≥ 2` — so the TTS rule "drop any line ending in `N gün önce`" drops exactly the DORMANT lines and never a FRESH one.

**Lifecycle ↔ freshness alignment.** Auto-resolution uses the *same* calendar-day cutoff (`last_update.date() ≤ now.date() − expire`) as the classifier, so an arc is resolved by the lifecycle **iff** the writer would classify it EXPIRED — no elapsed-hours skew between the two.

**FK-bug root cause (audited empirically).** `idx_arcs_state` is a b-tree index on the `arcs.state` column. DuckDB implements an `UPDATE` that touches an **indexed** column as *delete-then-insert* of the row; during that window the incoming foreign keys from `arc_centroids` and `clusters` (both `REFERENCES arcs(id)`) momentarily observe a missing parent and raise `ConstraintException` — even though `arcs.id` never changes. Verified directly: with the index present a bare `UPDATE arcs SET state` raises; after `DROP INDEX idx_arcs_state` the same update succeeds with both child tables referencing the row; updating a *non-indexed* column (`peak_defcon`) never trips it. The fix drops `idx_arcs_state` for the duration of the single bulk UPDATE and recreates it in a `finally` (so a mid-update failure can never leave the perf-only index missing). The test previously failed only because its `NOW=2026-05-23` fixture arc crossed the 7-day threshold relative to the real clock.

---

## ❯ Design Decisions

- **Freshness is a display axis, DEFCON is severity (D2).** They are orthogonal. `defcon.py` is untouched; freshness lives only in `arcs/`, `writer/`, `tts/`.
- **Lifecycle resolves at expiry, not the old 7d→30d ladder.** The brief wants the backlog to drain; resolving at `EXPIRE_DAYS` aligns the lifecycle with the freshness axis. DORMANT arcs (2-6d) stay OPEN so the writer keeps surfacing them with a recency marker; EXPIRED arcs resolve.
- **Auto-resolution is silent (no KAPATILAN flood, no `last_update_at` bump).** Dumping a multi-hundred-arc backlog into KAPATILAN HİKAYELER would be absurd and bumping the timestamp would corrupt the recency truth. The backlog drains by the open-count dropping; KAPATILAN keeps the existing operator/same-day-resolved rule. *Chosen over* adding a `resolved_at` migration (out of the arcs/writer/tts scope and unnecessary).
- **FK fix via index drop/recreate, not a per-arc child-snapshot.** It addresses the root cause directly and is a single fast statement — the right shape for draining hundreds of arcs — rather than replicating the linker's fragile per-cluster delete/reinsert workaround a third time.
- **Sections 2-6 fully deterministic (D4).** "A section that makes no LLM call cannot hallucinate." Only DEFCON 1-2/3 (faithful structured cluster prose) remain LLM-driven.
- **All-OPEN open-arc query.** *Chosen over* the prior this-run filter, which structurally hid DORMANT arcs — the exact recency the brief wants shown.
- **Voiced Öne Çıkanlar/Diğer cap (added after the live run).** Surfacing all open arcs in the markdown made the voiced AÇIK GELİŞMELER chunk 356 lines / 33 KB, which overran Piper's per-chunk timeout. Re-introducing the freshest-first voiced cap (reusing the extractor's existing `### Diğer Açık Hikayeler` truncation marker — no out-of-scope extractor edit) keeps every arc in the markdown while bounding the voiced set; the dormancy skip still applies within the highlight block.

---

## ❯ Verification

### Tier 1 · Unit (fakes) — GREEN
- `tests/test_arcs_freshness.py` — classifier boundary table + recency labels.
- `tests/test_transitions.py` — expiry resolution + FK regression (centroid + referencing cluster resolves cleanly; `idx_arcs_state` recreated).
- `tests/test_writer/test_render.py` + `test_briefer.py` — recency, freshest-first, EXPIRED excluded, all-EXPIRED → empty note, **no LLM call for itemized sections** (fabrication marker never lands in them), voiced-cap split.
- `tests/test_tts/test_preprocessor.py` — drops `N gün önce` (N≥2), keeps `bugün`/`dün`, all-dormant → note, line-end-anchored, arc-id rewrite + backtick strip.

### Tier 2 · Integration (real code, seeded DB) — GREEN
`tests/test_arc_freshness_integration.py`: arcs at today/1d/3d/8d → the 8-day arc resolves (FK-safe) and drains the open count; the 3-day arc is DORMANT (markdown "· 3 gün önce", **absent from spoken text**); today/1-day FRESH in both; AÇIK GELİŞMELER/AMBİYANS/DEFCON 4 are itemized data with the fabrication marker absent; all-dormant open arcs → spoken note.

### Tier 3 · Live smoke (real Ollama + real data) — VERIFIED
Ran on `run_20260529` (Ollama reachable · trendyol writer + bge-m3):
```
python -m musahit.pipeline run --date 2026-05-29 --stage arc-link --force
python -m musahit.pipeline run --date 2026-05-29 --stage write --force
python -m musahit.pipeline run --date 2026-05-29 --stage tts --force
```
Confirmed in `briefings/2026/05/29/briefing.md`:
- **AÇIK GELİŞMELER** — itemized open-arc list, 875 lines, recency distribution `dün: 356 · 3 gün önce: 271 · 4: 127 · 5: 78 · 6: 43`. DORMANT arcs (519 lines) show **true age**. Öne Çıkanlar (10, voiced) / Diğer Açık Hikayeler (865, markdown-only). **No İş Bankası fabrication.**
- **AMBİYANS** — itemized DEFCON-5 cluster list (`- başlık · (N kaynak) · dün`). **No Guantanamo essay.**
- **DEFCON 4** — every line shows recency (`… · arc_20260523_0002 · dün`).
- **MİT-Suriye arc** (problem #1) — `arc_20260523_0001` now reads `· 4 gün önce` in AÇIK GELİŞMELER (true age surfaced); still MATERYAL in DEFCON 3 (severity unchanged — correct).
- **Fabrication scan (whole file):** `İş Bankası: 0 · Guantanamo: 0 · kredi paketi: 0`.
- **Voice briefing:** the voiced AÇIK GELİŞMELER (post extract+preprocess) is the 10 Öne Çıkanlar arcs, all FRESH ("dün"), **974 chars** (was 33 502 — the un-capped chunk overran Piper). No `gün önce` in the spoken text (dormancy skip), arc-ids rewritten to `hikaye N`, backticks stripped, acronyms expanded. TTS completed with all 5 chunks synthesised, no placeholder.

**Open-arc count — honest finding.** The SİSTEM LOG shows **875** (unchanged). On 2026-05-29 the oldest open arc is from 2026-05-23 (6 days), so **zero arcs are ≥7 days old** and auto-resolution correctly resolves **0** this run (`expired_to_resolved: 0`). This is correct behavior: the freshness model legitimately keeps all ≤6-day arcs OPEN (FRESH/DORMANT), and the brief's own example confirms a May-23 arc should read "6 gün önce", not resolve. The lifecycle is **fixed** (it now runs to completion instead of crashing on the FK bug) and drains the backlog as arcs cross 7 days — e.g. the 43-arc May-23 cohort resolves on 2026-05-30, the May-24 cohort on 2026-05-31, and so on. The previously-reported "875 → drops" expectation assumed ≥7-day arcs existed on this date; they do not. No arcs were force-resolved to manufacture a drop.

### Full suite + lint — GREEN
- `python -m pytest` → **841 passed, 1 skipped, 0 failed** (baseline was 1 failed / 773 passed; the FK `test_linker` is fixed and no new failures).
- `python -m ruff check musahit/ tests/` → All checks passed.

---

## ❯ Post-implementation adversarial review

After the first commit, a multi-agent adversarial review of the diff (correctness, FK safety, brief faithfulness) surfaced **5 confirmed issues**, all fixed in the same commit (amended). Verification was re-run green afterward (841 passed; live write+tts clean).

| # | Sev | Issue | Fix |
|---|---|---|---|
| 1 | med | **Calendar-anchor mismatch.** `transition_states` anchored resolution on the UTC date (`utcnow().date()`) while the writer classifies freshness against the TR-local briefing date — so for the 21:00-24:00 UTC nightly window an arc the writer hid as EXPIRED was left OPEN. | Anchor the resolution cutoff on the **TR-local** date (`(current_time + 3h).date() − expire_days`), exactly matching the writer's `_days_between(briefing_date, …)`. Pinned by `TestTrLocalAnchor`. |
| 2 | low | **Index loss on hard kill.** A SIGKILL between `DROP INDEX` and the `finally` `CREATE` would leave `idx_arcs_state` permanently missing (a transaction wrap does *not* help — the FK still trips inside a txn, verified). | Self-heal: `CREATE INDEX IF NOT EXISTS idx_arcs_state` at the top of `transition_states`, so the next arc-link run repairs a crashed prior run. |
| 4 | med | **Dormancy filter dropped LLM prose.** The filter ran over the whole voiced text; an LLM DEFCON-1-2/3 line ending in "· N gün önce" was silently dropped (and could inject a false all-dormant note). | Anchor the filter to the deterministic arc-bullet shape (leading `- ` + backtick-wrapped arc id) via `_ARC_BULLET_RE`; LLM prose is never touched. Pinned by `test_llm_prose_ending_in_gun_once_not_dropped`. |
| 5 | low | **Orphaned highlight header.** When >10 arcs surface and the voiced top-10 are all dormant, the spoken output read "Öne Çıkanlar" then the all-dormant note. | Drop an immediately-preceding lone subheader when a block collapses to the note. Pinned by `test_orphaned_highlight_header_dropped_when_all_dormant`. |
| 3 | low | **Duplicate all-dormant notes** (latent) when dormant arc lines are split by a blank line within one chunk. | De-duplicate consecutive notes against the last non-blank entry. Pinned by `test_two_dormant_blocks_split_by_blank_emit_single_note`. |

---

## ❯ Acceptance checklist

- [x] `arc_last_update` advances on joins; freshness classifier implemented + unit-tested.
- [x] transitions FK bug diagnosed (`idx_arcs_state` delete+insert) and fixed; `test_linker` passes; expired arcs resolve without FK violation.
- [x] Auto-resolution at expiry (7d, tunable) integrated with the lifecycle. *(Drains 0 on 2026-05-29 because no arc is ≥7d yet; mechanism verified by Tiers 1-2 and runs cleanly live.)*
- [x] DEFCON stays severity; `defcon.py` untouched.
- [x] AÇIK GELİŞMELER + AMBİYANS deterministic (no LLM call); Mode-4 impossible.
- [x] Recency suffix on all itemized arc lines; freshest-first; EXPIRED excluded; DORMANT kept in markdown.
- [x] TTS skips dormant lines, keeps fresh, all-dormant → note, arc-id rewrite intact, markdown untouched.
- [x] Empty-section short-circuit preserved.
- [x] Tier 1 + Tier 2 + Tier 3 (deterministic parts) verified; LLM parts ran (Ollama reachable).
- [x] Full suite green · ruff clean.
- [x] One local commit · no push.

---

## ❯ Related Docs

- `docs/implementations/2026-05-29-arc-freshness-brief.md` (the authoritative spec for this cycle)
- `docs/implementations/2026-05-25-arc-evolution.md` (the `last_update_*` triplet this builds on)
- `docs/implementations/2026-05-24-arc-link-bug-fix.md` (the DuckDB FK-update pattern)
- ADR-004 (DEFCON), ADR-008 (arc lifecycle), ADR-009 (briefing template), ADR-012 (writer/TTS)

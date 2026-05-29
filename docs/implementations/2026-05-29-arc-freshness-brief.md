# MÜŞAHİT · Arc Freshness & Lifecycle Brief (Group A) · 2026-05-29

> *Date · 2026-05-29 (Fri)*
> *Components · `musahit/arcs/` · `musahit/writer/` · `musahit/tts/`*
> *Decision · Add a freshness axis (separate from DEFCON severity) that
> governs how arcs surface · make narrative sections deterministic data
> lists · skip dormant arcs in the voice briefing · fix the arc
> resolution lifecycle so the open-arc backlog drains*
> *Verification · MANDATORY three tiers · unit + integration + live*

This is the authoritative spec for the "Group A" autonomous cycle. The
operator is studying for finals and is unreachable · work autonomously,
do not wait for input, follow the locked decisions and decision trees
below, verify through all three tiers, document blockers, never leave
the suite worse than baseline.

---

## ❯ The problem (motivating observations · solved, do not re-litigate)

The 2026-05-29 briefing surfaced multiple stale-but-real arcs as if they
were current, with no recency indication, and the two narrative sections
fabricated content:

1. The MİT Suriye story (DEFCON 3 · MATERYAL) first broke ~May 26 and on
   May 29 still sits at DEFCON 3 looking identical to fresh news. The
   pipeline has **no freshness axis** · DEFCON encodes severity (correct ·
   a serious story stays serious) but nothing tracks that an arc has had
   no new sources for days and is therefore dormant.
2. DEFCON 4 · GÜNDEM listed arcs seeded May 23-24 (arc_20260523_*,
   arc_20260524_*) with no recency marker · 5-6 day old developments
   presented as today's agenda.
3. SİSTEM LOG showed 875 open arcs · the open→watch→resolved lifecycle
   is not pruning dormant threads (likely linked to the FK bug below).
4. AÇIK GELİŞMELER fabricated an İş Bankası loan-package story; AMBİYANS
   produced a multi-paragraph Guantanamo Bay essay · neither grounded in
   payload data (Mode 4 narrative hallucination).
5. `tests/test_linker.py::TestStopwordOnlyOverlap` fails with a DuckDB FK
   constraint error in `musahit/arcs/transitions.py` · the arc transition
   logic has a bug, plausibly the reason arcs never resolve (see #3).

These cohere into ONE subsystem fix · the arc freshness/lifecycle and how
the writer + TTS surface arcs.

---

## ❯ Locked decisions (made by the operator · authoritative)

**D1 · Scope** · Group A only · arc lifecycle + freshness + narrative
sections + TTS dormancy. Source bring-up and commit-cleanup are OUT of
scope (separate future cycles). Do not touch them.

**D2 · Freshness model** · DEFCON stays as **severity** and is NOT
mutated by age. A separate **freshness** signal, derived from each arc's
last-update timestamp, governs **surfacing** (prominence / inclusion /
voice). `musahit/score/defcon.py` is FILE-PROTECTED and MUST NOT be
edited · the freshness logic lives in `arcs/` (lifecycle + last_update)
and `writer/` (surfacing) and `tts/` (voice), never in defcon scoring.

**D3 · Three surfacing states** (driven by `arc_last_update`):

| State | Condition | Markdown briefing | Voice (TTS) briefing |
|---|---|---|---|
| FRESH | new source within last **2 days** | yes, full prominence, recency shown | yes |
| DORMANT | no new source for **2+ days**, still within lifespan | yes, with recency marker | **SKIPPED** |
| RESOLVED / EXPIRED | resolved, or no new source for **7 days** (auto-resolve, TUNABLE default) | no (appears once in KAPATILAN HİKAYELER on the day it resolves, then drops) | no |

The **2-day dormancy threshold** is locked. The **7-day auto-resolve**
is a flagged DEFAULT · integrate with any existing lifecycle timing
rather than inventing a parallel one; if no resolution/aging exists
(consistent with 875 open arcs), add it at 7 days and mark the constant
clearly tunable.

**D4 · Narrative sections become deterministic data lists** · AÇIK
GELİŞMELER and AMBİYANS are rendered as itemized arc lists (NOT via the
LLM), mirroring how DEFCON 4 renders its arc list. A section that makes
no LLM call cannot hallucinate · this kills Mode 4 by construction for
these sections and removes two LLM calls per run. If DEFCON 4 is itself
currently LLM-generated, convert all three to deterministic rendering
since the content is purely itemized arc data. The only LLM-narrative
sections that remain are DEFCON 1-2 and DEFCON 3 (structured cluster
data · they have stayed faithful).

---

## ❯ The fix contract

### Part 1 · Arc freshness data (`musahit/arcs/`)

1. **Ensure `arc_last_update` is maintained** · every time an arc gains a
   new source/cluster (a "join"), its last-update timestamp must advance.
   Schema v4 (`add_arc_last_update`) provides the column(s); verify the
   linker/transition path actually writes them on every join. If joins
   are not updating last_update, fix that · it is the signal everything
   else depends on.

2. **Freshness classification** · add a helper (e.g.
   `arc_freshness(last_update, now, dormancy_days=2, expire_days=7)`)
   returning `FRESH | DORMANT | EXPIRED`. Pure function, fully unit-
   testable, no DB. Day math is calendar-day based and tz-aware
   (reuse the project's existing tz-aware `utcnow`/time helpers · the
   calibration loader precedent used tz-aware datetimes).

3. **Fix the transitions FK bug** · diagnose the DuckDB FK constraint
   error in `musahit/arcs/transitions.py` surfaced by
   `tests/test_linker.py::TestStopwordOnlyOverlap`. Report the root cause
   in the run report. Fix it so arc transitions (open→watch→resolved /
   aging) execute without violating the constraint.

4. **Auto-resolution / aging** · arcs that reach EXPIRED (no new source
   for `expire_days`, default 7) transition to resolved via the
   (now-fixed) transition logic, so they leave the active set and the
   open-arc backlog drains. They surface once in KAPATILAN HİKAYELER on
   resolution day. Integrate with existing lifecycle semantics; do not
   bolt on a parallel mechanism.

### Part 2 · Writer surfacing (`musahit/writer/`)

1. **Payload carries freshness** · `build_payload` (payload.py) exposes,
   per arc, its `last_update` and computed freshness state and a
   day-count since last update. Open-arc and cluster-arc data structures
   gain these fields. (payload.py was previously declared stable in the
   per-section work, but this cycle legitimately requires freshness data
   in the payload · editing payload.py here is in scope.)

2. **Recency display in arc lists** · every arc line in the itemized
   sections (DEFCON 4, AÇIK GELİŞMELER, AMBİYANS) shows recency in a
   consistent, parseable Turkish format appended to the line:
   - 0 days → ` · bugün`
   - 1 day → ` · dün`
   - N≥2 days → ` · N gün önce`
   Example line:
   `Karakoç'un gizli arşivi ortaya çıktı · MEVZUAT · (3 kaynak) · arc_20260523_0006 · 6 gün önce`
   This both informs the human reader AND is the signal TTS uses to skip
   dormant lines (Part 3). Keep the existing line fields and order; the
   recency suffix is additive at the end.

3. **AÇIK GELİŞMELER + AMBİYANS deterministic** · render these as
   itemized arc lists (per D4), recency-sorted (freshest first), with
   DORMANT arcs included (markdown) but visibly marked by their recency
   suffix. EXPIRED arcs excluded. No LLM call for these sections.

4. **Sort + filter** · arc lists are sorted freshest-first. EXPIRED arcs
   are excluded from active sections. DORMANT arcs are included in
   markdown. (DEFCON 1-2 and DEFCON 3 remain LLM-narrative over their
   cluster data · unchanged in generation, but if they reference arcs,
   they should prefer fresh ones.)

5. **Empty-section short-circuit preserved** · the existing honest
   "Bugün bu bölümde öğe yok." behavior for genuinely empty sections
   stays. A section that is all-EXPIRED (nothing fresh or dormant) is
   empty → short-circuit note.

### Part 3 · Voice briefing skip (`musahit/tts/preprocessor.py`)

1. **Skip dormant arc lines in the voice briefing** · the TTS
   preprocessor, when processing itemized arc-list sections, drops lines
   whose recency suffix indicates dormancy (`N gün önce` with N ≥ 2).
   Lines marked ` · bugün` or ` · dün` are retained. This is additive to
   the existing arc-id→"hikaye N" rewrite (commit 7009730); keep that
   working.

2. **All-dormant section in voice** · if every arc line in a section is
   dormant (so the voice version of the section would be empty), emit a
   short spoken note (e.g. "Bu bölümde bugüne ait güncel gelişme yok.")
   rather than a confusingly empty section. Keep it brief.

3. **Markdown is unchanged by TTS** · the preprocessor operates on the
   text it feeds to Piper; it must not rewrite or truncate the on-disk
   `briefing.md`. Markdown keeps all dormant arcs; only the spoken text
   drops them.

---

## ❯ MANDATORY verification · three tiers (all are acceptance gates)

Unit-only verification has burned this project before (the per-section
loss, the got:0 discard). All three tiers must pass before declaring
done. Tiers 2 and 3 are not optional.

### Tier 1 · Unit tests (fakes)

Arcs (`tests/test_arcs*` / `tests/test_linker.py`):
- `arc_freshness` returns FRESH ≤2d, DORMANT at exactly 2d and beyond
  (within expiry), EXPIRED at ≥7d. Boundary tests at 1/2/6/7 days.
- last_update advances on a join (a fresh source bumps the timestamp).
- transitions FK bug · the failing `test_linker` test now PASSES; add a
  regression test that an arc with no activity for `expire_days` resolves
  without an FK violation.

Writer (`tests/test_writer/`):
- recency formatting · 0→"bugün", 1→"dün", N→"N gün önce".
- AÇIK GELİŞMELER + AMBİYANS render deterministically (NO LLM call ·
  assert the fake LLM's call_count excludes these sections).
- arc lists sorted freshest-first; EXPIRED excluded; DORMANT included.
- a section that is all-EXPIRED short-circuits to the empty note.

TTS (`tests/test_tts/`):
- preprocessor drops lines with "N gün önce" (N≥2) from the spoken text.
- preprocessor keeps "bugün"/"dün" lines.
- all-dormant section → the short spoken "no fresh updates" note.
- the existing arc-id→"hikaye N" rewrite still works.

### Tier 2 · Integration test (real code, seeded DB) · new file

Wire the REAL arcs lifecycle + REAL writer + REAL TTS preprocessor over a
seeded DB with arcs at varied last_update ages (e.g. one updated today,
one 1 day ago, one 3 days ago, one 8 days ago). Run the write stage, then
the TTS preprocessing step, and assert end-to-end:
- the 8-day arc is EXPIRED → absent from active sections (or resolved into
  KAPATILAN HİKAYELER per the lifecycle).
- the 3-day arc is DORMANT → present in the markdown with " · 3 gün önce",
  ABSENT from the spoken text.
- the today/1-day arcs are FRESH → present in both, marked "bugün"/"dün".
- AÇIK GELİŞMELER + AMBİYANS contain ONLY itemized arc data (no free
  prose, no fabricated entities) · assert no LLM call was made for them.
- the open-arc count after the run reflects resolution of expired arcs
  (backlog drains).
This is the test that proves the freshness signal flows arcs → writer →
TTS coherently against production code.

### Tier 3 · Live smoke (real Ollama + real data) · the agent runs this

run_20260529 now has real clusters (recovered by Solution B). Run the
downstream stages:

```powershell
python -m musahit.pipeline run --date 2026-05-29 --stage arc-link --force
python -m musahit.pipeline run --date 2026-05-29 --stage write --force
python -m musahit.pipeline run --date 2026-05-29 --stage tts --force
```

Then read `briefings/2026/05/29/briefing.md` and confirm:
- AÇIK GELİŞMELER is now an itemized open-arc list with recency suffixes
  (NOT the İş Bankası fabrication).
- AMBİYANS is now an itemized DEFCON-5 arc list (NOT the Guantanamo essay).
- DEFCON 4 lines now show recency (` · N gün önce` / `bugün` / `dün`).
- arcs seeded May 23-24 either show their true age (e.g. "6 gün önce") or
  are gone if they hit expiry · they are NOT presented as fresh.
- the open-arc count in SİSTEM LOG has dropped from 875 as expired arcs
  resolve.
Capture the relevant briefing sections and the open-arc count into the
run report. If Ollama is unreachable, the deterministic sections + TTS +
arc resolution still verify via Tiers 1-2; mark only the LLM-dependent
parts of Tier 3 BLOCKED-with-reason. (DEFCON 1-2 / DEFCON 3 still need the
LLM, but AÇIK GELİŞMELER / AMBİYANS / DEFCON 4 / recency / resolution are
deterministic and must verify regardless.)

---

## ❯ Acceptance checklist

- [ ] `arc_last_update` advances on every join; freshness classifier (FRESH/DORMANT/EXPIRED) implemented and unit-tested.
- [ ] transitions.py FK bug diagnosed (root cause in report) and fixed; `test_linker` baseline test passes; expired arcs resolve without FK violation.
- [ ] Auto-resolution at expiry (7d default, tunable, integrated with existing lifecycle) drains the open-arc backlog.
- [ ] DEFCON stays severity; defcon.py UNTOUCHED. Freshness lives in arcs/writer/tts only.
- [ ] AÇIK GELİŞMELER + AMBİYANS render deterministically as itemized arc lists (no LLM call for them); Mode 4 fabrication impossible for these sections.
- [ ] Recency suffix (bugün / dün / N gün önce) on all itemized arc lines; lists sorted freshest-first; EXPIRED excluded; DORMANT included in markdown.
- [ ] TTS preprocessor skips dormant (N≥2) lines in the spoken text; keeps fresh; all-dormant section → brief spoken note; arc-id rewrite still works; on-disk markdown untouched by TTS.
- [ ] Empty-section short-circuit preserved.
- [ ] Tier 1 unit green · Tier 2 integration green · Tier 3 live verified (deterministic parts mandatory; LLM parts BLOCKED-only-if-Ollama-down).
- [ ] FULL pytest suite green · ruff clean (`musahit/` + `tests/`).
- [ ] One commit · local only · no push.
- [ ] Run report: `docs/implementations/2026-05-29-arc-freshness-report.md`.

---

## ❯ Scope & safety

- **Edit only** · `musahit/arcs/*` (transitions, linker, freshness),
  `musahit/writer/*` (payload, prompt, briefer, template as needed),
  `musahit/tts/preprocessor.py`, and their tests + the new integration
  test + the report.
- **FILE-PROTECTED · never edit** · `musahit/sources.py`,
  `musahit/ingest/poller.py`, `musahit/score/defcon.py`. The freshness
  axis must NOT touch defcon.py.
- **Out of scope** · source bring-up (the 16 failed sources),
  commit-tangle cleanup, the score/cluster/ingest/normalize stages
  (cluster is done via Solution B; do not revisit).
- **No destructive ops** · no `git reset --hard`, no force, no push, no
  dropping DB tables. The live smoke writes arcs/briefings/clusters
  (expected · it recovers the 29th's briefing); it deletes nothing.
  Auto-resolution moves arcs to resolved state via normal lifecycle
  transitions · that is intended, not destructive.
- **No new dependencies.**
- **Commit message** · `feat(arcs): freshness axis · dormant-arc surfacing · deterministic narrative sections · TTS dormancy skip · transitions fix (Group A)`.
- **If blocked** after ~5 honest retries on any one part, document the
  blocker in the report, commit what is green, continue the other parts.
  Never stall the whole cycle on one stuck piece.

---

## ❯ Recommended order of operations

1. Read this brief fully. Read `arcs/transitions.py`, the linker, the
   arc schema, `writer/payload.py`, `writer/prompt.py`, `writer/briefer.py`,
   `writer/template.py`, `tts/preprocessor.py`, and confirm which sections
   are LLM vs deterministic today.
2. Part 1 · arc freshness + transitions fix + auto-resolution → Tier 1
   arc tests → commit-worthy checkpoint.
3. Part 2 · writer surfacing (payload freshness, recency display,
   deterministic narrative sections, sort/filter) → Tier 1 writer tests.
4. Part 3 · TTS dormancy skip → Tier 1 TTS tests.
5. Tier 2 integration test (arcs → writer → TTS end-to-end).
6. FULL suite + ruff.
7. Tier 3 live smoke (arc-link → write → tts for 2026-05-29); capture
   results.
8. Commit once. Write the run report. List blockers/deferred clearly.

---

## ❯ One-line takeaway

> *DEFCON is severity; a separate freshness axis (from arc_last_update)
> decides surfacing · dormant arcs (2d+) stay in the markdown with their
> age shown but drop from the voice briefing · expired arcs (7d) resolve
> and drain the backlog · AÇIK GELİŞMELER + AMBİYANS become deterministic
> arc lists so they cannot hallucinate · fix the transitions FK bug ·
> proven unit + integration + live.*

---

*End of brief. The operator is studying for finals. Work autonomously,
verify through all three tiers, leave the repo greener than you found it,
and write the report so the operator can review it cold.*

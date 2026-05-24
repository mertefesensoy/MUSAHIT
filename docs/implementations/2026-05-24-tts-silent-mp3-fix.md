# Implementation: TTS silent-MP3 fix · AÇIK GELİŞMELER cap + per-chunk resilience

**Date** · 2026-05-24
**Author** · Mert Efe Şensoy
**ADR refs** · ADR-009 (amended 2026-05-24) · ADR-010 · ADR-012

---

## ❯ Problem / Motivation

The first end-to-end smoke run on 2026-05-23 shipped a 44 KB
silent placeholder MP3 instead of a real briefing.mp3. The
`pipeline_runs.counts` row showed
`tts_used_placeholder: true`. The operator opened
`briefings/2026/05/23/briefing.mp3` expecting a 4-6 minute Turkish
briefing and heard one second of silence.

The visible symptom (silent MP3) hid two interacting bugs in the
TTS path. Both have to be fixed · either alone leaves a known
failure mode for the next smoke run.

---

## ❯ Root cause

### (a) Fallback writer dumps all open arcs into one chunk

`musahit/writer/fallback.py::_render_open_arcs` rendered every
arc in `payload.open_arc_updates` as a full `_render_arc` block
(headline, açıldı, peak DEFCON, category, paragraph summary).
The 2026-05-23 payload carried 222 open arcs · the section came
out at **64,517 characters** (≈ 12K tokens). The TTS extractor
faithfully bucketed every line into the AÇIK GELİŞMELER voiced
chunk.

### (b) Synthesizer fails the whole stage on any chunk failure

`musahit/tts/synthesizer.py::_synthesise_chunks` looped through
chunks and called `await self._piper.synthesize(chunk)` directly
· any exception bubbled up to the outer `try/except` in
`Synthesizer.run`. That exception path goes to the placeholder
write per the ADR-012 always-ships invariant.

The 64K AÇIK GELİŞMELER chunk hit Piper's 60s per-chunk timeout
(ADR-010). The TimeoutError propagated up the call chain. Every
other chunk (header, DEFCON 1-2, DEFCON 3, closing) · which would
have synthesized in 3-8 seconds each · never got a chance to run.

The silent placeholder is the correct ADR-012 behavior when EVERY
chunk fails. It's the wrong behavior when ONE oversized chunk
fails and the others would have succeeded.

---

## ❯ What Changed

| File | Description |
|---|---|
| `ADR-009-briefing-template.md` | Amended block at top + TTS scope section rewritten. AÇIK GELİŞMELER now split into `### Öne Çıkanlar` (top `VOICED_OPEN_ARCS_CAP = 10` arcs · voiced) and `### Diğer Açık Hikayeler` (overflow · visual-only). Audio target dropped to 3-4 min. |
| `musahit/writer/fallback.py` | `_render_open_arcs` produces the Öne Çıkanlar / Diğer split. `_arc_sort_key` sorts by `(peak_defcon ASC, -timestamp)` with `last_update_at` → `created_at` → epoch 0 fallback. `_render_arc_overflow_bullet` produces the one-line bullet shape. `VOICED_OPEN_ARCS_CAP = 10` exported. |
| `musahit/writer/template.py` | AÇIK GELİŞMELER section's `prompt_instruction` rewritten to instruct Trendyol-LLM to produce the same Öne Çıkanlar / Diğer split. Empty-state phrase preserved. |
| `musahit/tts/extractor.py` | `_split_into_sections` tracks `inside_open_arcs_overflow` flag · stops bucketing once `### Diğer Açık Hikayeler` line encountered inside MARKER_OPEN_ARCS. Diğer marker line dropped. Other sections unaffected. Old-shape briefings (no Diğer marker) work unchanged. |
| `musahit/tts/synthesizer.py` | `_synthesise_chunks` wraps each Piper call in try/except. Per-chunk failures log `tts_chunk_failed` (index, chars, error) and continue. Returns partial WAV list on any success. Raises `ValueError("all chunks failed synthesis") from last_exc` only when EVERY chunk fails · `from last_exc` preserves the original exception in the chained traceback for stderr diagnostics. |
| `tests/test_writer/test_fallback.py` | `TestOpenArcsSubsectionSplit` (6) covering: 11-arc → highlight 10 + overflow 1; 10-arc → only highlight; severity-then-recency sort; None-date safety; overflow bullet shape; validator still passes. |
| `tests/test_tts/test_extractor.py` | `TestOpenArcsSubsectionTruncation` (5) covering: highlight body kept; Diğer marker dropped; overflow bullets excluded; decoy Diğer line in other sections doesn't truncate; old-shape briefings (no Diğer) still extract all open-arcs content. |
| `tests/test_tts/test_synthesizer.py` | `TestPerChunkResilience` (2) covering: one-failing-chunk doesn't poison rest · whole-stage real MP3 still ships; FailingPiper is called once per chunk before raising (proves per-chunk loop shape). |
| `memory/build-progress.md` | "Step 16 follow-up · TTS silent-MP3 fix · 2026-05-24" entry. |
| `docs/implementations/2026-05-24-tts-silent-mp3-fix.md` | This file. |

---

## ❯ Implementation Approach

### Layer 1 · Cap the voiced AÇIK GELİŞMELER content

The fix is to render the section in two subsections at the
fallback layer. The fallback writer already knew the arc count;
it just rendered all of them with full blocks. The new shape:

```python
ordered = sorted(payload.open_arc_updates, key=_arc_sort_key)
highlighted = ordered[:VOICED_OPEN_ARCS_CAP]
overflow = ordered[VOICED_OPEN_ARCS_CAP:]

parts = [_HIGHLIGHT_SUBSECTION_MARKER, ""]
parts.append("\n\n".join(_render_arc(a) for a in highlighted))
if overflow:
    parts.extend(["", _OTHER_SUBSECTION_MARKER, ""])
    for a in overflow:
        parts.append(_render_arc_overflow_bullet(a))
```

Sort key:

```python
def _arc_sort_key(arc):
    dt = arc.last_update_at or arc.created_at
    epoch = dt.timestamp() if dt is not None else 0.0
    return (int(arc.peak_defcon), -epoch)
```

`peak_defcon` ascending puts the most severe arcs first (lower
int = more severe per IntEnum per ADR-005). Within each severity
tier, the most-recently-updated arc wins (negative epoch). Arcs
with no `last_update_at` fall back to `created_at`; arcs with
neither tiebreak to epoch 0 (sorted last in their tier ·
deterministic).

The overflow bullet:

```
- {headline} · {DEFCON_LABEL_TR} · {category} · `{arc_id}`
```

Roughly 80-120 characters per bullet. Even with 200+ overflow
arcs, that's 16-24K characters · still visible in the markdown
briefing for the dashboard, but the TTS extractor's truncation
keeps it out of the voiced text. The dashboard renders both
subsections so the operator can scroll the full list.

### Layer 2 · Per-chunk resilience in the synthesizer

```python
async def _synthesise_chunks(self, chunks, *, log):
    out = []
    last_exc = None
    for i, chunk in enumerate(chunks):
        log.debug("tts_chunk_start", index=i, chars=len(chunk))
        try:
            wav = await self._piper.synthesize(chunk)
        except Exception as exc:
            log.warning(
                "tts_chunk_failed",
                index=i, chars=len(chunk),
                error=f"{type(exc).__name__}: {exc}",
            )
            last_exc = exc
            continue
        out.append(wav)
    if not out:
        raise ValueError("all chunks failed synthesis") from last_exc
    return out
```

Three properties this gives us:

- **Per-chunk failures don't poison the stage.** One oversized
  chunk failing produces a structured warning and the loop
  continues. The rest of the briefing still synthesizes.
- **Empty-result is still a stage failure.** When every chunk
  fails the outer try/except in `run()` still catches a
  `ValueError` and writes the silent placeholder · the ADR-012
  always-ships invariant is preserved.
- **Diagnostic chain is preserved.** `from last_exc` carries the
  most recent per-chunk exception in the chained traceback ·
  `traceback.print_exc()` in `run()` prints both the
  `ValueError` AND the original (e.g. `RuntimeError: simulated
  piper failure`) so manual / smoke-test runs see the root
  cause without needing to grep the structured log.

### Layer 3 · TTS extractor truncation

The extractor needs to stop bucketing voiced content once it
hits the Diğer marker · without that, the overflow bullets would
still go to Piper:

```python
inside_open_arcs_overflow = False
for line in briefing_md.splitlines():
    stripped = line.strip()
    if stripped in ALL_MARKERS:
        current_key = stripped
        inside_open_arcs_overflow = False
        buckets.setdefault(current_key, [])
        continue
    if current_key == MARKER_OPEN_ARCS and stripped == _DIGER_MARKER:
        inside_open_arcs_overflow = True
        continue
    if inside_open_arcs_overflow:
        continue
    buckets[current_key].append(line)
```

Two important invariants:

- The flag resets whenever we encounter ANY `## ❯` marker. A
  later section's content is not affected by the OPEN_ARCS
  truncation.
- The check `current_key == MARKER_OPEN_ARCS` scopes the
  truncation. A decoy `### Diğer Açık Hikayeler` line that
  somehow appears in DEFCON 1-2 (improbable but not impossible)
  doesn't truncate that section.

Old-shape briefings (rendered before this fix) have no Diğer
marker · the truncation simply never fires and the whole AÇIK
GELİŞMELER section is bucketed as before. Backward compatibility
is automatic.

---

## ❯ Mathematical / Statistical Details

Not applicable · sort key + control-flow change.

---

## ❯ Design Decisions

### Why two layers and not just one?

Each layer alone leaves a known regression path:

- **Cap alone** stops the visible bug (next smoke run produces a
  small AÇIK GELİŞMELER chunk · synthesis succeeds). But any
  future per-chunk failure mode (a different oversized section, a
  Piper bug, a transient timeout) still fails the whole stage.
- **Per-chunk resilience alone** lets the stage produce some
  audio even when one chunk fails. But that audio would be
  missing the AÇIK GELİŞMELER content entirely · the operator
  gets a briefing that's structurally complete but semantically
  hollow on a critical section. Better than silence, worse than
  a briefing with the top 10 arcs voiced.

Together: the cap makes the AÇIK GELİŞMELER chunk small enough to
synthesize reliably, AND the per-chunk resilience absorbs any
future regression of this class.

### Why cap at 10 arcs, not 5 or 20?

5 is too few · on busy days the operator's priority queue can
have 5-8 arcs at SEVERE or above, and the briefing would cut
off mid-priority. 20 is too many · audio length scales roughly
linearly with arc count, and 20 arcs of full content would push
toward 8-10 minutes which exceeds the briefing's skim-and-stop
discipline. 10 is the largest number that keeps audio under 4
minutes on heavy days while still surfacing the full priority
queue (which is bounded by the arc-link stage's severity
ordering · most days produce 3-7 high-severity arcs).

### Why sort by `(peak_defcon ASC, last_update_at DESC)`?

`peak_defcon ASC` is the severity-first ordering · the operator
hears the most severe arcs first, matching the briefing's
top-down skim discipline. `last_update_at DESC` as the tiebreak
puts recently-active arcs ahead of dormant ones at the same
severity · "this story is still moving" matters more than "this
story was severe a week ago and hasn't updated."

The `(peak_defcon, -epoch)` tuple form (as a Python sort key) is
clean and avoids the double-sort idiom. Negation of float epoch
seconds is safe for all dates in our range (modern timestamps
fit well within float precision).

### Why fall back to `created_at` then to 0?

ArcView has `last_update_at: datetime | None` and
`created_at: datetime | None` (the /goal spec named
`last_seen_at` which doesn't exist · this was an explicit
tripwire and the fallback was the documented resolution).

The fallback chain `last_update_at → created_at → epoch 0` gives
a deterministic order even for arcs with no timestamps at all
(treated as "very old · sorts last in their tier"). Tests pin
this so the chain stays stable across refactors.

### Why `raise ValueError(...) from last_exc`?

Three alternatives considered:

1. **Bare `raise ValueError(...)`** · the `traceback.print_exc()`
   in `run()` would only show the `ValueError`, hiding the
   original Piper exception. The existing
   `test_piper_crash_prints_traceback_to_stderr` test asserts
   the original `RuntimeError` appears in stderr · bare raise
   would break that contract.
2. **Re-raise the original exception (`raise last_exc`)** · this
   would preserve the original but lose the "all chunks failed"
   semantic at the top of the chain · operators reading the
   stderr traceback wouldn't see that the failure was
   stage-wide rather than chunk-specific.
3. **`raise ValueError(...) from last_exc`** · keeps both. The
   chained traceback shows "During handling of the above
   exception, another exception occurred:" between the two ·
   operators see the root cause AND the stage-level summary in
   one read.

Option 3 wins.

### Why no ADR amendment for the synthesizer change?

The per-chunk wrapping is implementation discipline · it doesn't
change ADR-010's contract (Piper synthesizes individual chunks
with a per-call timeout). The behavior change is "we now keep
trying after one fails" rather than "we now use a different
budget." ADR-012 already mandates the always-ships invariant ·
the new code is more faithful to that invariant, not a departure
from it.

The ADR-009 amendment is necessary because the briefing structure
(section subsections) IS part of the contract that downstream
consumers (extractor, dashboard renderer, future operator-tooling)
key off.

### Why no re-render of the 2026-05-23 briefing?

The bad output on disk is part of the operator's audit trail of
the first smoke run. Re-rendering would erase the evidence that
the bug existed and that this fix was triggered by it. The next
smoke run will produce a clean briefing; the 2026-05-23 artifact
stays as-is.

---

## ❯ Verification

```powershell
# Linter clean.
python -m ruff check .

# Targeted subsuites · expected 152 passed, 1 skipped (139 prior + 13 new).
$env:PYTHONIOENCODING = "utf-8"
python -m pytest tests/test_writer/ tests/test_tts/ -q

# Full suite · expected 613 passed, 2 skipped (600 prior + 13 new).
python -m pytest tests/ -q
```

The 13 new tests pin:

- `TestOpenArcsSubsectionSplit` (6) · 11 arcs produce highlight
  10 + overflow 1; 10 arcs produce only highlight (no Diğer);
  sort follows severity-then-recency; None-date arcs don't
  crash; overflow bullet shape exactly matches the spec; split
  briefings still pass the markdown validator.
- `TestOpenArcsSubsectionTruncation` (5) · highlight body kept;
  Diğer marker line dropped; overflow bullets excluded; decoy
  Diğer line in DEFCON 1-2 does NOT truncate (scope is correct);
  old-shape briefings without the Diğer marker continue to
  extract all open-arcs content (backward compat).
- `TestPerChunkResilience` (2) · one-failing-chunk doesn't fail
  the whole stage (real MP3 ships); FailingPiper is called once
  per voiced chunk before the stage raises (proves the loop
  shape, not bail-on-first).

The existing `TestPiperFailure` suite (which uses `FailingPiper`)
continues to pass · all chunks failing still triggers the
placeholder write per ADR-012. The chained-exception assertion in
`test_piper_crash_prints_traceback_to_stderr` confirms the
`from last_exc` chain preserves diagnostics through the per-chunk
dampening.

### Diagnostic against the 2026-05-23 briefing

The existing `briefings/2026/05/23/briefing.md` was rendered by
the pre-fix fallback writer and has no Diğer marker. Running it
through the new extractor:

- AÇIK GELİŞMELER section still extracts all 222 arcs into the
  voiced text · the truncation never fires because the Diğer
  marker isn't there. Chunk size remains ~64K characters.
- The new synthesizer would still fail on that oversized chunk
  · the per-chunk timeout fires.
- But now the failure is isolated · header, DEFCON 1-2, DEFCON 3,
  and the closing line all synthesize successfully. A real MP3
  ships, just with the AÇIK GELİŞMELER section silent. The
  operator sees `tts_chunk_failed` warnings in the log for the
  bad chunk.

The next smoke run produces a briefing with the split shape ·
AÇIK GELİŞMELER's Öne Çıkanlar chunk is under 10K characters,
synthesizes comfortably within the 60s budget, and the full
briefing audio ships.

---

## ❯ Operator caveats

- The 2026-05-23 `briefing.mp3` stays as-is (silent placeholder).
  The audit trail of the first smoke run is preserved.
- The next smoke run's briefing will have a structurally
  different AÇIK GELİŞMELER section · the operator should
  re-listen end-to-end (per the "Audio QA is a real review
  stage" convention in `memory/MEMORY.md`) to verify the
  subsection split sounds coherent in TTS.
- If a future briefing produces a `tts_chunk_failed` warning in
  the log, that's a per-chunk failure (synthesizable real MP3
  still shipped, just one chunk silent). Inspect the warning's
  `chars` field · if it's under 20K, the failure is a Piper bug
  rather than a size issue. File to `memory/operator-tasks.md`.
- Raising `VOICED_OPEN_ARCS_CAP` above 10 should be an ADR-009
  amendment · the 3-4 minute audio target is part of the
  contract. Lowering it (e.g. to 5 on holidays with no signal)
  could be ad-hoc.

---

## ❯ Related Docs

- ADR-009 · briefing template (amended 2026-05-24)
- ADR-010 · TTS / Piper
- ADR-012 · always-ships invariant
- `docs/implementations/2026-05-23-tts.md` · original TTS build
- `docs/implementations/2026-05-23-write.md` · original writer build
- `docs/implementations/2026-05-24-template-placeholder-fix.md` · sibling smoke-run fix (placeholder echo)
- `docs/implementations/2026-05-24-category-normalization.md` · sibling smoke-run fix (LLM diacritic folding)
- `docs/implementations/2026-05-24-arc-link-bug-fix.md` · sibling smoke-run fix (arcs cascade)

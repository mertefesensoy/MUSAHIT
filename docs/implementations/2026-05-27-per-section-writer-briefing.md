# MÜŞAHİT · Per-Section Writer Refactor (Angle E)

> *Date · 2026-05-27 (Tue)*
> *Component · `musahit/writer/`*
> *Decision · Implement per-section LLM generation*
> *Status · Spec finalized · ready for implementation*

This document is the **canonical spec** for the Angle E refactor. The
Claude Code prompt that ships this change points at this file and
treats every "must" in here as a goal condition. If you change your
mind about a decision below, change the spec first · then re-prompt.

---

## ❯ Context

The writer has shipped `writer_used_fallback: true` for three
consecutive days (2026-05-25 · 2026-05-26 · 2026-05-27 morning). Three
remediations were attempted:

| Angle | Change | Outcome |
|---|---|---|
| A | Template skeleton moved to recency window of prompt | Attempt 0 content improved · still flattens hierarchy |
| B | Response prefill via Ollama `/api/chat` · `LlmClient.generate_with_prefill` | Title and first section correct · model invents subsections (`DEFCON 1-3 · SAVUNMA`) |
| A.5 | Explicit "exactly 8 sections · no subsections" lead-in + SECTION_ROSTER + DISCIPLINE rule | Same failure pattern · stronger language did not reduce hallucination |

The diagnostic conclusion is that **Trendyol-LLM 7B is at the
capability ceiling for single-shot structured generation across 25-30K
tokens of input + 8-section hierarchical output**. The model
understands the data, can produce idiomatic Turkish prose, holds the
template in its recency window · but cannot reliably maintain the
section-as-grouping-vs-section-as-tag distinction over a long
generation.

Per-section generation pivots away from "make the prompt clearer" and
toward "make each generation small enough that the model cannot lose
structure." Each call generates one section · tightly scoped payload ·
prefilled with the canonical section marker · validated in isolation.
The orchestrator concatenates the pieces.

---

## ❯ Architecture

### Current writer (deprecated by this refactor)

```
build_writer_prompt(payload)
    → SYSTEM_ROLE + DEFCON schema + DISCIPLINE_RULES + all_data + template
    → 25-30K token single prompt
Briefer._compose:
    for attempt in range(max_retries + 1):
        text = await llm.generate_with_prefill(system, user, prefill, ...)
        markdown = prefill + text
        if validate_briefing_markdown(markdown):
            return markdown, used_fallback=False
        # append validator errors to user for next attempt
    return render_fallback_briefing(payload), used_fallback=True
```

One call · whole-briefing validator · whole-briefing fallback.

### New writer

```
Briefer._compose:
    sections: list[str] = []
    failed_indices: list[int] = []
    for idx in range(len(TEMPLATE_SECTIONS)):
        section = TEMPLATE_SECTIONS[idx]
        user = build_section_user(payload, idx)
        prefill = f"{section.marker}\n\n"
        text = await llm.generate_with_prefill(
            system=build_writer_system(),
            user=user,
            prefill=prefill,
            ...
        )
        full = prefill + text
        if validate_section(full, idx):
            sections.append(full)
        else:
            sections.append(render_section_stub(idx))
            failed_indices.append(idx)

    # SİSTEM LOG (always section idx 7) gets re-emitted last with
    # failed_indices populated, so the section reports its own
    # failures truthfully.
    sections[7] = build_system_log_section(payload, failed_indices)

    markdown = f"{DOCUMENT_TITLE}\n\n" + "\n\n".join(sections)

    # Final whole-briefing validation should pass by construction.
    # If it does not, fall back to render_fallback_briefing as a
    # last-resort safety net (should never fire in practice).
    if validate_briefing_markdown(markdown):
        used_fallback = len(failed_indices) == len(TEMPLATE_SECTIONS)
        return markdown, used_fallback, failed_indices
    return render_fallback_briefing(payload), True, list(range(len(TEMPLATE_SECTIONS)))
```

8 calls · per-section validator · per-section stubs.

### Why this works

- **Tiny payload per call** · cluster section gets only its DEFCON
  bucket · maybe 2K tokens instead of 25K.
- **Prefilled marker** · model cannot fail to emit the section header ·
  it is given to it.
- **Single section to hold** · model only generates content under
  one `##` · no hierarchy to maintain.
- **Isolated failure** · one bad section does not poison the others.

The cost is **8x Ollama calls per writer run** · estimated 2-4 minutes
total vs 30-60 seconds today. Acceptable for unattended 02:00 runs ·
noticeable for `--stage write --force` debugging.

---

## ❯ Locked decisions

### Decision 1 · Per-section stub on failure

When `validate_section(full, idx)` returns false, the section is
replaced with a stub from `render_section_stub(idx)`. The stub matches
the section's marker so the whole-briefing validator still passes.
Other sections in the same run are unaffected.

`writer_used_fallback` semantics change:

| Failed indices count | `writer_used_fallback` |
|---|---|
| 0 | `false` (all real) |
| 1 to 7 | `false` (mixed real + stub · per ADR-012 "briefing always ships") |
| 8 | `true` (full fallback) |

A new field tracks per-section detail:

```json
"writer_sections_fallback": [3, 5]   // indices of failed sections, empty list = clean run
```

### Decision 2 · Prompt module owns partition

`build_payload()` in `musahit/writer/payload.py` is **UNCHANGED**. It
still returns the same `BriefingPayload` shape. The per-section logic
lives entirely in `musahit/writer/prompt.py` as a new function
`build_section_user(payload, section_idx)` that slices the payload
internally.

### Decision 3 · SİSTEM LOG self-reports failures

The SİSTEM LOG section (idx 7) reports failed sections in human-readable
form within the briefing markdown itself. Example:

```markdown
## ❯ SİSTEM LOG

**Run** · `run_20260528`
**Çalıştırılan aşamalar** · ingest · normalize · cluster · score · arc-link · write · tts
**İşlenen olay** · 491
...
**Başarısız bölüm üretimi** · DEFCON 4 · GÜNDEM · DİKKAT · YALNIZCA SOSYALDE
```

The "Başarısız bölüm üretimi" line is OMITTED when `failed_indices` is
empty. When present, it lists the section markers (titles after `## ❯`)
joined with ` · `.

---

## ❯ Section roster

The 8 sections of the briefing template, indexed 0-7. Each row's "Data
source" is what `build_section_user` extracts from the payload.

| idx | Section marker | Data source |
|---|---|---|
| 0 | `## ❯ DEFCON 1-2 · ÖNCELİKLİ` | `payload.clusters_by_defcon[1..2]` |
| 1 | `## ❯ DEFCON 3 · MATERYAL` | `payload.clusters_by_defcon[3]` |
| 2 | `## ❯ AÇIK GELİŞMELER · DEVAM EDEN TAKİP` | `payload.open_arc_updates` |
| 3 | `## ❯ DEFCON 4 · GÜNDEM` | `payload.clusters_by_defcon[4]` |
| 4 | `## ❯ DİKKAT · YALNIZCA SOSYALDE` | clusters where `is_social_only=True` |
| 5 | `## ❯ AMBİYANS · DEFCON 5` | `payload.clusters_by_defcon[5]` |
| 6 | `## ❯ KAPATILAN HİKAYELER` | `payload.resolved_arcs` |
| 7 | `## ❯ SİSTEM LOG` | `payload` metrics + `failed_sources` + `failed_indices` |

**Source of truth for markers** · `musahit/writer/template.TEMPLATE_SECTIONS[idx].marker`.
Do not hardcode marker strings · derive from `TEMPLATE_SECTIONS` at
import time.

---

## ❯ Implementation requirements

### File · `musahit/writer/prompt.py`

**Add**:

```python
def build_section_user(payload: BriefingPayload, section_idx: int) -> str:
    """Compose the user-message text for a single-section writer call.

    The returned text contains ONLY the data needed for the section at
    section_idx · plus the DISCIPLINE_RULES · plus a short reminder that
    only this section is being written.

    The section's marker is NOT in the returned text · it is sent to
    the LLM as the prefilled assistant message by the orchestrator.
    """
```

The function dispatches by `section_idx`:

- **0, 1, 3, 5** (cluster DEFCON sections) · render only the matching
  DEFCON bucket. Reuse internal helpers from `_clusters_data_block` but
  scoped to one bucket.
- **2** · render `_arcs_data_block`'s open-arc portion only.
- **4** · render social-only cluster headlines from all buckets.
- **6** · render `_arcs_data_block`'s resolved-arc portion only.
- **7** · render system log via new `build_system_log_section` (see below).

Each section's user message follows this shape:

```
SYSTEM_ROLE  (omitted · this is the system message)

GÖREV · Aşağıdaki tek bir bölümü yaz · BAŞKA HİÇBİR BÖLÜM YAZMA.

Hedef bölüm · ## ❯ {SECTION_MARKER}

{DISCIPLINE_RULES}

BÖLÜM VERİSİ:
{section-specific data block}

ÇIKTI (yalnızca bu bölümün içeriği · marker hazır verilmiştir):
```

**Add** new module-level constant:

```python
SECTION_INSTRUCTION_TEMPLATE = (
    "GÖREV · Aşağıdaki tek bir bölümü yaz · BAŞKA HİÇBİR BÖLÜM YAZMA.\n\n"
    "Hedef bölüm · {marker}\n"
)
```

**Add** new public function:

```python
def build_system_log_section(
    payload: BriefingPayload,
    failed_section_indices: list[int],
) -> str:
    """Render the SİSTEM LOG section directly (not via LLM).

    The SİSTEM LOG is structured metadata · not creative prose · so
    the writer composes it deterministically rather than asking the
    LLM. This also lets the section faithfully report which other
    sections fell back to stubs.
    """
```

This is **always** generated deterministically, never via LLM. Section
idx 7 in `_compose` skips the LLM call and writes this directly.

**Preserve**:
- `SYSTEM_ROLE`
- `DISCIPLINE_RULES`
- `SECTION_ROSTER`
- `TEMPLATE_LEAD_IN`
- `OUTPUT_INSTRUCTION`
- `build_writer_system()`
- `_defcon_schema_block` (still used by per-section prompts that
  reference DEFCON levels)
- `_template_skeleton`
- All other private helpers

**Deprecate (do not delete)**:
- `build_writer_user` · mark with single-line docstring deprecation,
  keep functional for backward compat with any callers we miss
- `build_writer_prompt` · same treatment

### File · `musahit/writer/briefer.py`

Rewrite `Briefer._compose` to the new per-section loop per the
architecture pseudocode above.

**The first 7 sections** loop over `range(7)` and call
`llm.generate_with_prefill` for each.

**Section 7 (SİSTEM LOG)** is handled OUTSIDE the LLM loop · directly
via `build_system_log_section(payload, failed_indices)` · so it always
reports the truthful list of failed sections.

Final assembly:

```python
markdown = f"{DOCUMENT_TITLE}\n\n" + "\n\n".join(sections)
```

Run `validate_briefing_markdown(markdown)` as a final safety check. If
it fails (should not in practice), call `render_fallback_briefing` and
mark all 8 indices failed.

Return signature change:

```python
async def _compose(
    self, payload: BriefingPayload, log
) -> tuple[str, bool, list[int]]:
    """Returns (markdown, used_fallback, failed_indices)."""
```

The caller `run()` extracts these and includes `sections_failed` in
both the structured log event and the persisted counts row.

### File · `musahit/writer/validator.py`

**Add**:

```python
def validate_section(text: str, section_idx: int) -> bool:
    """Per-section validator. Asserts the given text is a valid
    single-section block matching TEMPLATE_SECTIONS[section_idx].marker.

    Pass conditions:
    - First non-blank line is TEMPLATE_SECTIONS[section_idx].marker
    - No other lines starting with '## ❯' appear in the text
    - Text contains at least one non-marker non-empty line (no
      empty sections)

    Failure conditions return False · the caller substitutes a stub.
    """
```

`validate_briefing_markdown` is **UNCHANGED**. It still runs at the
end of `_compose` as the final assembly check.

### File · `musahit/writer/fallback.py`

**Add**:

```python
def render_section_stub(section_idx: int) -> str:
    """Render a placeholder stub for a section whose LLM generation failed.

    The stub matches TEMPLATE_SECTIONS[section_idx].marker exactly
    so the assembled briefing still passes validate_briefing_markdown.
    """
    section = TEMPLATE_SECTIONS[section_idx]
    return (
        f"{section.marker}\n\n"
        f"Bu bölüm üretilemedi · yedek metin kullanıldı."
    )
```

`render_fallback_briefing` is **UNCHANGED**. It still serves as the
last-resort whole-briefing fallback if all 8 sections fail OR if the
final assembled markdown fails validation.

### Logging schema

**`writer_done` event** gains a new field:

```json
{
    "event": "writer_done",
    "used_fallback": false,
    "sections_failed": [3, 5],
    "cluster_count": 491,
    ...
}
```

`sections_failed` is the list of section indices (0-7) that fell back
to stubs. Empty list `[]` for clean runs.

**`pipeline_runs.counts` JSON** gains:

```json
{
    ...
    "writer_used_fallback": false,
    "writer_sections_fallback": [3, 5]
}
```

`writer_used_fallback: true` iff all 8 sections failed.
`writer_sections_fallback` always present (empty list for clean runs).

### File · `docs/adr/ADR-012-amendment-per-section-writer.md`

**New file**. Full text:

```markdown
# ADR-012 Amendment · Per-Section Writer Generation

> *Amendment date · 2026-05-27*
> *Original ADR · ADR-012 (failure and retention)*
> *Triggers · 2026-05-27 per-section writer refactor*

## Context

ADR-012 § Stage 6 Writer states "the briefing always ships." The
original implementation interpreted this as: writer LLM call fails
after 4 retries → whole-briefing fallback shipped → status COMPLETED.

The 2026-05-25 through 2026-05-27 incident demonstrated that
Trendyol-LLM 7B cannot reliably produce a structurally-valid 8-section
briefing under heavy-day payloads (800+ clusters). Single-shot
generation consistently failed validation. The whole-briefing fallback
fired every run · the operator received structurally-valid but
fallback-shaped prose for three consecutive days.

The 2026-05-27 per-section refactor (Angle E) replaces single-shot
generation with 8 sequential per-section LLM calls. This changes the
fallback granularity.

## Decision

The "always ships" semantic is preserved at the briefing level. The
fallback semantic is refined at the section level:

- **Clean run** · all 8 sections generated successfully by LLM.
  `writer_used_fallback = false` · `writer_sections_fallback = []`.

- **Partial-stub run** · 1 to 7 sections fell back to stubs · others
  are real LLM output. `writer_used_fallback = false` ·
  `writer_sections_fallback = [<indices>]`. The briefing.md contains
  a mix of real prose and stub placeholders. The SİSTEM LOG section
  reports the failed section markers explicitly.

- **Full-fallback run** · all 8 sections failed, OR the final assembled
  markdown failed `validate_briefing_markdown`. `render_fallback_briefing`
  is called as a last resort. `writer_used_fallback = true` ·
  `writer_sections_fallback = [0,1,2,3,4,5,6,7]`.

## Consequences

**Positive** · operator can distinguish a partial-quality briefing
(some sections real) from a full-fallback briefing (no LLM content)
at a glance. Failure isolation prevents one bad section from
contaminating the rest. Per-section validation is dramatically
cheaper than 4-attempt whole-briefing retry.

**Negative** · briefing.md may contain mixed-quality content without
visual distinction beyond the stub's own text. The SİSTEM LOG
self-report is the operator's only structural signal. Per-section
generation is ~4x slower in wall time (~2-4 min vs 30-60 sec).

**Operator action required** · review `writer_sections_fallback` in
the daily check. If the same section fails repeatedly, investigate
that section's prompt or payload partitioning.

## Related

- ADR-012 § Stage 6 Writer (original semantics)
- ADR-009 § Briefing template (8-section structure)
- 2026-05-27 implementation spec ·
  `docs/implementations/2026-05-27-per-section-writer-briefing.md`
```

---

## ❯ Test surface

All tests live under `tests/`. Existing tests outside `test_writer/`
must not regress.

### tests/test_writer/test_briefer.py

**Rewrite** the existing happy-path test:

- Mock `LlmClient.generate_with_prefill` to return valid per-section
  content for each of 7 calls (idx 0-6). Section 7 (SİSTEM LOG) is
  deterministic · no mock needed.
- Run `_compose`.
- Assert 7 LLM calls were made (NOT 8 · SİSTEM LOG is deterministic).
- Assert each call's `prefill` argument matches the expected section
  marker.
- Assert the assembled markdown starts with `DOCUMENT_TITLE`.
- Assert `used_fallback = False` and `failed_indices = []`.
- Assert `validate_briefing_markdown(markdown) == True`.

**Add** `test_per_section_failure_produces_stub`:

- Mock LLM returns invalid output for section idx 3 only · valid for
  others.
- Assert section 3 in the assembled markdown is the stub text
  ("Bu bölüm üretilemedi").
- Assert other 6 LLM sections contain their real content.
- Assert `failed_indices == [3]`.
- Assert `used_fallback = False`.
- Assert SİSTEM LOG section contains "Başarısız bölüm üretimi" with
  the failed section's title.

**Add** `test_all_llm_sections_fail_marks_full_fallback`:

- Mock LLM returns invalid output for all 7 LLM calls.
- Assert all 7 LLM sections become stubs · SİSTEM LOG still real.
- Assert `failed_indices == [0, 1, 2, 3, 4, 5, 6]`.
- Assert `used_fallback = False` (technically SİSTEM LOG succeeded ·
  per Decision 1, full-fallback requires ALL 8 indices including
  SİSTEM LOG). Document this edge case clearly in the test.

**Add** `test_final_validation_failure_triggers_full_fallback`:

- Mock per-section validator and final validator to force a state
  where validate_section passes but validate_briefing_markdown fails.
- Assert `render_fallback_briefing` is called.
- Assert `used_fallback = True` and `failed_indices` covers all 8.
- This is the last-resort path · should not fire in practice but
  must be tested.

### tests/test_writer/test_validator.py

**Add** tests for `validate_section`:

- `test_validate_section_accepts_valid_block` · marker on first line ·
  content below · no other ## ❯ markers.
- `test_validate_section_rejects_missing_marker` · text starts with
  prose instead of marker.
- `test_validate_section_rejects_wrong_marker` · marker present but
  it's the marker for a different section_idx.
- `test_validate_section_rejects_multiple_markers` · text contains
  two `## ❯` headers.
- `test_validate_section_rejects_empty_content` · marker only · no
  content beneath.

### tests/test_writer/test_prompt.py

**Add** tests for `build_section_user`:

- `test_build_section_user_includes_only_target_bucket` · pass payload
  with clusters in all DEFCON levels · call with section_idx=0
  (DEFCON 1-2 · ÖNCELİKLİ) · assert output contains DEFCON 1-2 cluster
  headlines but NOT DEFCON 3 or DEFCON 4 headlines.
- `test_build_section_user_for_open_arcs` · section_idx=2 returns
  open_arc data, no clusters.
- `test_build_section_user_for_resolved_arcs` · section_idx=6 returns
  resolved_arc data, no clusters.
- `test_build_section_user_includes_discipline_rules` · every
  section_idx output contains DISCIPLINE_RULES text.
- `test_build_section_user_omits_section_marker_from_text` · the
  marker is delivered via prefill · should NOT appear in the user
  message.

**Add** tests for `build_system_log_section`:

- `test_system_log_no_failures_omits_failure_line` · pass empty
  `failed_section_indices` · assert "Başarısız bölüm üretimi" string
  is NOT in output.
- `test_system_log_with_failures_includes_failure_line` · pass
  `[3, 5]` · assert the section titles for indices 3 and 5 appear
  in the output joined by " · ".

**Preserve** the existing positioning tests · they may now exercise
deprecated functions · adapt or remove only as strictly necessary.

### Tests OUTSIDE test_writer/

`tests/test_score/*` must remain UNCHANGED. Per-section refactor does
not touch the score worker. If any score test regresses, surface
immediately as a tripwire.

---

## ❯ Files touched

The refactor modifies ONLY these files. Anything else is a regression.

**Modified**:
- `musahit/writer/prompt.py`
- `musahit/writer/briefer.py`
- `musahit/writer/validator.py`
- `musahit/writer/fallback.py`
- `tests/test_writer/test_briefer.py`
- `tests/test_writer/test_validator.py`
- `tests/test_writer/test_prompt.py`

**New**:
- `docs/adr/ADR-012-amendment-per-section-writer.md`

**Forbidden** (FILE-PROTECTED + scope-protected):
- `musahit/score/llm_client.py` (writer reuses existing
  `generate_with_prefill` · do not modify it)
- `musahit/writer/payload.py` (per Decision 2 · payload unchanged)
- `musahit/writer/template.py` (template structure unchanged)
- `musahit/sources.py` · `musahit/ingest/poller.py` ·
  `musahit/score/defcon.py` (FILE-PROTECTED per project rules)
- Any file under `musahit/score/`, `musahit/ingest/`,
  `musahit/normalize/`, `musahit/cluster/`, `musahit/arcs/`,
  `musahit/tts/`
- Any test file under `tests/test_score/`, `tests/test_ingest/`,
  `tests/test_normalize/`, `tests/test_cluster/`, `tests/test_arcs/`,
  `tests/test_tts/`

---

## ❯ Operator verification (after merge)

```powershell
# Smoke against today's already-populated data
python -m musahit.pipeline run --date 2026-05-27 --stage write --force

# Pull the writer events from the live console output (NOT the log
# file · single-stage CLI runs may append to the existing run's log)
# Expected event sequence:
#   7x HTTP POST /api/chat (one per LLM section)
#   1x writer_done with sections_failed=[] and used_fallback=false
#   1x stage_complete
#   1x pipeline_done

# DB row check
python scripts\check_run_state.py
# Expected counts row:
#   "writer_used_fallback": false
#   "writer_sections_fallback": []

# Visual review · open briefing.md, read each of the 8 sections,
# confirm prose quality is qualitatively better than fallback shape
Get-Content briefings\2026\05\27\briefing.md | Select-Object -First 80
```

**Pass condition**: `writer_used_fallback: false` AND
`writer_sections_fallback: []` AND prose quality is qualitatively
better than the backed-up `briefing.fallback-shape.md`.

**Acceptable partial pass**: `writer_used_fallback: false` AND
`writer_sections_fallback: [<small list>]` · 1-2 sections fell back
but most are real. Open follow-up issues for the failing sections ·
do NOT block on this.

**Fail condition**: `writer_used_fallback: true` OR all 7 LLM sections
failed. Escalate to Angle F · one-shot example per section · before
considering further design changes.

---

## ❯ Standing rules during implementation

1. **No retry loops at the section level.** A section call fails once
   → falls back to stub immediately. Per-section retry would defeat
   the speed advantage and tempt a small-model thrash cycle.
2. **No prompt-engineering changes beyond the section payload split.**
   SYSTEM_ROLE, DISCIPLINE_RULES, SECTION_ROSTER, TEMPLATE_LEAD_IN
   stay as-is unless implementation strictly requires changing them.
3. **No new dependencies.** Use only what's already in pyproject.toml.
4. **No async-to-sync conversions.** The writer is async · keep it.
5. **No log-message renames.** `writer_done` stays `writer_done` ·
   `writer_fallback` stays · only add fields, do not rename existing.
6. **No schema migration for `pipeline_runs`.** The counts JSON is
   already a flexible blob · adding `writer_sections_fallback` is
   additive · no DDL needed.

---

## ❯ One-line takeaway

> *Eight small calls beat one big call · prefill the marker · stub the
> failures · let the operator see what failed.*

---

*End of spec. The Claude Code prompt pointing at this file is in the
project chat for 2026-05-27.*

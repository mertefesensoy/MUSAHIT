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

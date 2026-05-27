# Implementation: Writer Prompt Template Reorder

**Date** · 2026-05-27
**Author** · Claude Code
**ADR refs** · ADR-009

---

## ❯ Problem / Motivation

The writer prompt placed the template skeleton (SABLON) early in the prompt and the day's cluster/arc data after it. On heavy days (800+ clusters, 25-30K token prompts), the template skeleton ended up thousands of tokens upstream of the generation point. Trendyol-LLM 7B's recency bias meant it failed to follow the template structure — generating Turkish-textbook-shape defaults (# Bolum 1 / ## Baslik 1) instead of the briefing template's `## ❯` section markers.

---

## ❯ What Changed

| File | Description |
|---|---|
| `musahit/writer/prompt.py` | Reordered `build_writer_prompt()`: data now appears before the template skeleton; added `TEMPLATE_LEAD_IN` constant; updated `OUTPUT_INSTRUCTION` to reference "yukaridaki sablon"; updated docstring token estimates for heavy days. |
| `tests/test_writer/test_prompt.py` | Added `TestTemplatePositioning` class with 3 tests: template in last 30% of prompt, data before template, lead-in present. Added `_heavy_payload()` fixture with 40 clusters + 10 arcs. |

---

## ❯ Implementation Approach

Pure reorder of the `"\n\n".join(...)` list in `build_writer_prompt()`:

**Before:** SYSTEM_ROLE → DEFCON → SABLON → template → RULES → DATA → OUTPUT_INSTRUCTION → CIKTI

**After:** SYSTEM_ROLE → DEFCON → RULES → DATA → TEMPLATE_LEAD_IN → template → OUTPUT_INSTRUCTION → CIKTI

The template skeleton is now the last semantic block before `CIKTI (markdown):`, placing it squarely in the model's recency window on heavy-day prompts. The new `TEMPLATE_LEAD_IN` constant ("SABLONU ASAGIDA DOLDUR · DEGISTIRME:") replaces the old "SABLON:" label and reinforces that the template must not be altered.

`OUTPUT_INSTRUCTION` was updated from "Asagidaki sablonu doldur" to "Yukaridaki sablonu doldur" since the template now appears above (not below) the instruction.

---

## ❯ Design Decisions

**Why not few-shot examples:** The operator explicitly deferred few-shot examples as "Angle B" — this reorder is the minimal Angle A intervention. If the reorder alone is insufficient on heavy days, few-shot examples would be the next step.

**Heavy-payload test fixture:** The positioning test uses a 40-cluster + 10-arc fixture rather than the existing 1-cluster `_payload()`. With a light payload, the template skeleton represents ~50% of the prompt regardless of position — the 70% threshold would be meaningless.

**Token estimate update:** The docstring's worst-case estimate was updated from ~13K to ~25-30K based on observed heavy-day payloads (800+ clusters). The old estimate was misleading about available margin.

---

## ❯ Verification

```powershell
# All 77 writer tests pass (74 existing + 3 new positioning tests)
python -m pytest tests/test_writer/ -v

# Operator smoke test (post-merge):
python scripts\smoke_writer.py --date 2026-05-27
```

---

## ❯ Related Docs

- ADR-009 (writer discipline rules, voiced scope)
- `docs/implementations/2026-05-24-template-placeholder-fix.md`

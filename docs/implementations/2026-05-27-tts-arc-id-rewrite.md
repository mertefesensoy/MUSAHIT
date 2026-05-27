# Implementation: TTS Arc ID Rewrite

**Date** · 2026-05-27
**Author** · Claude Code
**ADR refs** · ADR-009, ADR-012

---

## ❯ Problem / Motivation

The briefing writer embeds arc IDs (`arc_20260523_0001`) inline as cross-reference anchors. When Piper TTS reads these verbatim, the YYYYMMDD segment is pronounced as a huge integer ("yirmi milyon iki yuz altmis bin...") — unintelligible audio. The operator wants the trailing serial only, spoken as "hikaye N" to match the "story" terminology used in the markdown.

---

## ❯ What Changed

| File | Description |
|---|---|
| `musahit/tts/preprocessor.py` | Added `_ARC_ID_RE` regex and `_rewrite_arc_ids_for_tts()` function; integrated as step 3 in `preprocess_for_tts()` pipeline (after markdown stripping, before DEFCON/acronym expansion). |
| `tests/test_tts/test_preprocessor.py` | Added `TestArcIdRewriting` class with 6 test cases covering single IDs, triple-digit serials, multiple IDs in bullet lists, inline prose, passthrough of text without arc IDs, and backtick-wrapped IDs. |

---

## ❯ Implementation Approach

A single compiled regex `r"arc_\d{8}_(\d{4})"` captures the 4-digit trailing serial via group 1. The replacement lambda converts it to an integer (stripping leading zeros) and formats as `hikaye {N}`. The regex matches regardless of surrounding characters (backticks, bold markers, etc.) because it doesn't use word boundaries — the `arc_` prefix is unique enough in the pipeline's text.

The function is placed inside `preprocess_for_tts()` as step 3 — after markdown stripping (which removes bold/italic/links/headers) but before DEFCON respelling and acronym expansion. This ordering means backtick-wrapped IDs are caught even if backtick stripping is added later, since the regex doesn't depend on surrounding characters.

The briefing markdown output (`briefing.md`) is unchanged — the rewrite is purely a TTS-input transformation.

---

## ❯ Design Decisions

**Preprocessor vs. synthesizer**: The function lives in `preprocessor.py` rather than `synthesizer.py` because the preprocessor is the established home for text-to-speech transformations. The synthesizer already calls `preprocess_for_tts()` on each chunk — no wiring change needed.

**No word boundaries**: Unlike the acronym regex which uses `\b`, the arc ID regex omits word boundaries. The `arc_\d{8}_` prefix is specific enough that false positives are not a concern, and `\b` would break matching inside backtick-wrapped IDs where the backtick abuts the `a`.

**run_YYYYMMDD excluded**: The System Log section contains `run_YYYYMMDD` identifiers that could also benefit from rewriting, but this is deferred to a separate scope per the operator's instruction.

---

## ❯ Verification

```powershell
# All 95 TTS tests pass (29 existing preprocessor + 6 new + 60 others)
python -m pytest tests/test_tts/ -v

# Smoke TTS against today's briefing (requires Piper voice installed)
python scripts/smoke_tts.py
```

---

## ❯ Related Docs

- ADR-009 (voiced scope)
- ADR-012 (TTS stage)
- `docs/implementations/2026-05-23-write.md`

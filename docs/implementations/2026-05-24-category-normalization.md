# Implementation: Category normalization · Turkish-diacritic folding for LLM output

**Date** · 2026-05-24
**Author** · Mert Efe Şensoy
**ADR refs** · ADR-009 (category taxonomy) · ADR-001 (worker LLM contract)

---

## ❯ Problem / Motivation

The first end-to-end smoke run on 2026-05-23 surfaced 2 of 139
clusters falling through the classifier's max-retries fallback to
`{category=UNCLASSIFIED, defcon=AMBIENT, confidence_self=low}`.
Inspection of the structured log showed both failures were Pydantic
validation errors on the `category` field. The LLM was returning
`"DIPLOMASİ"` · Latin I, Turkish İ at the end · instead of the
canonical `"DİPLOMASİ"`. Pydantic's `Category` enum coercion
rejected the value, the classifier retried `max_retries` times,
every retry produced the same near-miss, the fallback fired.

The bug is systemic, not specific to `DİPLOMASİ`. Every `Category`
value with Turkish-specific characters
(`POLİTİKA`, `EKONOMİ`, `GÜVENLİK`, `DİPLOMASİ`) is exposed to the
same pattern: any LLM that drops one or more diacritics produces a
string that looks correct to a human reader but fails strict enum
matching. With Qwen2.5 7B as the worker model, the failure rate is
low (≈1 %) but the failure mode is silent · the briefing keeps
shipping but loses category signal for those clusters · so it would
have continued degrading the dashboard until someone audited
classifier fallback rates.

The retry behaviour for *genuinely* malformed JSON or schema
violations must stay · the fix is to normalize fold-equivalent
inputs to canonical before enum coercion, without touching the
strict-validation contract for everything else.

---

## ❯ Root cause

Two interacting facts:

1. `musahit.common.types.Category` is a `StrEnum` whose values are
   the canonical Turkish strings (`POLİTİKA`, `EKONOMİ`, `YARGI`,
   `GÜVENLİK`, `DİPLOMASİ`, `MEVZUAT`, `TOPLUM`, `SINIFLANDIRILMADI`).
2. LLMs trained primarily on English data tend to drop or
   substitute Turkish-specific characters · `İ`→`I`, `Ş`→`S`,
   `Ğ`→`G`, `Ü`→`U`, `Ö`→`O`, `Ç`→`C`. The drop is partial · in
   the 2026-05-23 trace the model wrote `DIPLOMASİ` (only the
   first İ folded) rather than the fully-ASCII `DIPLOMASI`.
   Pydantic's enum coercion does byte-exact string comparison
   against `Category.value`, so both forms fail.

The classifier's existing retry loop catches the
`ValidationError` and re-prompts. The re-prompt does not contain
"please use Turkish characters" instructions and Qwen tends to
make the same mistake again. After `max_retries` attempts the
fallback fires. Logs from the smoke run confirmed the model never
self-corrected.

---

## ❯ What Changed

| File | Description |
|---|---|
| `musahit/score/schema.py` | Added `_tr_lower`, `_ASCII_FOLD`, `_fold_for_matching`, `_CATEGORY_NORMALIZATION_MAP` (built programmatically from `Category` values, with import-time collision check), `_normalize_category` helper, and a `field_validator("category", mode="before")` on `WorkerResponse` that delegates to the helper. Module docstring extended to describe the folding contract. |
| `tests/test_score/__init__.py` | Empty marker for the new subpackage. |
| `tests/test_score/test_schema.py` | `TestCategoryNormalization` (24 tests across canonical pass-through, full ASCII folds, lowercase folds, observed bug case, negative cases for unknown / empty / partial matches, fold-map invariants, direct helper tests) + `TestParseWorkerResponseWithFolding` (3 tests for the JSON-strip + fenced-JSON path). 27 tests total. |
| `memory/MEMORY.md` | "Turkish locale case folding" entry extended with an "LLM output normalization" subsection. Pattern is named (`field_validator(mode="before")` + fold map keyed by `_fold_for_matching`), the WorkerResponse case is cited, and the next-likely candidate (`Confidence` enum) is flagged. |
| `memory/build-progress.md` | "Step 16 follow-up · category normalization · 2026-05-24" between-step entry. |
| `docs/implementations/2026-05-24-category-normalization.md` | This file. |

---

## ❯ Implementation Approach

### Folding helper

```python
_TR_LOWER_PRE = str.maketrans({"İ": "i", "I": "ı"})
_ASCII_FOLD = str.maketrans({
    "ı": "i", "ş": "s", "ğ": "g",
    "ü": "u", "ö": "o", "ç": "c",
})

def _tr_lower(s):
    return s.translate(_TR_LOWER_PRE).lower()

def _fold_for_matching(s):
    return _tr_lower(s).translate(_ASCII_FOLD)
```

Two passes: Turkish-aware lowercase first (so `İ` becomes `i` and
`I` becomes `ı`, the correct Turkish lowercases per MEMORY.md),
then an ASCII fold over the remaining diacritic-lowercase set
(ı/ş/ğ/ü/ö/ç → i/s/g/u/o/c).

The fold map is keyed by lowercase-and-stripped form and valued by
canonical:

```python
_CATEGORY_NORMALIZATION_MAP = {
    _fold_for_matching(c.value): c.value for c in Category
}
```

For the current 8 values the map is:

| Fold key | Canonical |
|---|---|
| `politika` | `POLİTİKA` |
| `ekonomi` | `EKONOMİ` |
| `yargi` | `YARGI` |
| `guvenlik` | `GÜVENLİK` |
| `diplomasi` | `DİPLOMASİ` |
| `mevzuat` | `MEVZUAT` |
| `toplum` | `TOPLUM` |
| `siniflandirilmadi` | `SINIFLANDIRILMADI` |

All 8 keys are distinct · no collision. The import-time check
fails loudly if a future `Category` addition introduces one:

```python
if len(_CATEGORY_NORMALIZATION_MAP) != len(list(Category)):
    raise RuntimeError("Category fold collision · ...")
```

This is in addition to the unit-test assertion
(`TestCategoryNormalization::test_fold_map_has_no_collisions`)
so a future enum addition surfaces in both the import traceback
AND the test report.

### Validator wiring

```python
@field_validator("category", mode="before")
@classmethod
def _normalize_category_value(cls, v):
    if isinstance(v, str):
        return _normalize_category(v)
    return v
```

`mode="before"` runs the validator before Pydantic's enum
coercion. If the input string folds to a known `Category`,
`_normalize_category` returns the canonical value, enum coercion
succeeds. If it doesn't (genuine garbage, typo, malformed
output), `_normalize_category` returns the input unchanged · enum
coercion fails · `ValidationError` propagates · classifier
retries · eventually falls back. The retry contract is preserved.

Non-string inputs (e.g. an already-coerced `Category` instance,
which can happen when an internal caller hand-builds a
`WorkerResponse`) are returned unchanged so the validator is a
no-op in those paths.

---

## ❯ Mathematical / Statistical Details

Not applicable · the fold is a deterministic lookup, not a
similarity measure.

---

## ❯ Design Decisions

### Why a field validator, not a transform in `parse_worker_response`?

Alternative: have `parse_worker_response` parse the JSON, walk
the dict, normalize any field whose name is `category`, then
`model_validate`. Rejected because:

- The transform would silently apply to every caller of
  `parse_worker_response`, but it would NOT apply to callers that
  build a `WorkerResponse` from a dict via `model_validate` or
  `model_construct` directly (any future code path that bypasses
  the parser). The validator lives on the model itself · it
  catches every entry point.
- The transform pollutes the parser with knowledge of the
  schema's enum semantics. The validator keeps the schema
  responsible for its own type coercion, the parser responsible
  for stripping LLM artefacts (code fences, prose). Separation
  of concerns.
- A future schema with Turkish enums (e.g. a hypothetical
  `WriterResponse.confidence: Confidence`) would have to be
  hand-wired into a parser-side branch. The validator pattern is
  copy-paste at the schema level instead.

### Why fold-then-map, not fuzzy match?

Alternative: levenshtein-distance fuzzy match against the canonical
list. Rejected because:

- Cheap deterministic lookup beats per-call distance computation
  in the hot path (every cluster gets a worker call).
- Fuzzy match risks bridging real differences (e.g. a typo that
  happens to be 1 edit from a real category but is semantically
  different). The fold map only collapses the specific failure
  mode we have evidence for · LLM diacritic drop. Other failure
  modes still raise.

### Why programmatically build the map from the enum?

Hand-writing the map (`{"POLITIKA": "POLİTİKA", ...}`) duplicates
the canonical list. A future enum addition would silently miss
normalization until someone notices · the same class of bug as
the 2026-05-23 cascade in `_update_cluster_arc_id` (`docs/implementations/2026-05-24-arc-link-bug-fix.md`).
Programmatic construction means the fold map can never lag the
enum.

### Why an import-time collision check AND a test?

Defence in depth. The import-time check fails loudly when the
module is loaded · catches the case where the test suite isn't
run before a deploy. The test pins the invariant in the regression
report so a future enum addition that triggers the collision is
attributed to the right change, not buried in an opaque
`ImportError`. Both stay.

### Why pass unknown values through instead of raising in
`_normalize_category`?

Returning the unchanged input makes the validator additive · it
strictly enlarges the set of accepted strings without removing
any. If `_normalize_category` raised on unknown input, the
behaviour for genuinely malformed inputs would shift from
"`ValidationError` from enum coercion" to "`ValidationError`
from custom validator." Same outcome from the classifier's
perspective, but the existing tests in `test_classifier.py` (and
the implicit retry-on-`ValidationError` contract) pin the enum
coercion as the failure source. Keeping the validator additive
preserves that contract.

### Scope discipline · what was NOT changed?

- **Other LLM-output schemas.** Grep `class.*Response.*BaseModel`
  across `musahit/` returned exactly one file
  (`musahit/score/schema.py`). No `WriterResponse`, no
  `BrieferResponse`, no other pydantic models with Turkish-enum
  fields. /goal tripwire on "other model outputs need similar
  normalization" was checked and cleared. If/when one lands, the
  MEMORY.md convention now points the next implementer at the
  same pattern.
- **Other fields on WorkerResponse.** `summary`, `headline`, and
  `entities` are free-text · no enum, no normalization needed.
  `confidence_self` is a `Literal["high", "medium", "low"]` · all
  ASCII · LLMs don't ASCII-fold ASCII. /goal tripwire on "other
  fields need normalization" was checked and cleared.
- **Other enums.** `Confidence` (`YÜKSEK`, `ORTA`, `DÜŞÜK`) is
  Turkish but is not currently exposed as a field on any LLM-
  output schema; it's set by the score stage from rule output
  (`musahit/score/promotion.py`), which is internal code that
  uses the enum directly. No normalization needed yet.

---

## ❯ Verification

```powershell
# Linter clean.
python -m ruff check .

# New schema tests · expected: 27 passed.
$env:PYTHONIOENCODING = "utf-8"
python -m pytest tests/test_score/ -v

# Full suite stays green plus 27 · expected: 588 passed, 2 skipped.
python -m pytest tests/ -q
```

The validator behaviour is also locked in by:

- Import-time collision assertion in `schema.py` (failing this
  raises `RuntimeError` at load · pipeline cannot start).
- `TestCategoryNormalization::test_fold_map_has_no_collisions`
  (failing this surfaces the conflict in the regression report).
- `TestCategoryNormalization::test_all_canonical_values_pass_through_unchanged`
  parameterized across every `Category` value · catches a future
  enum value that the fold helper doesn't handle (e.g. a member
  introducing a character not in `_ASCII_FOLD`).
- `TestParseWorkerResponseWithFolding::test_malformed_json_still_raises_validation_error`
  · pins the retry contract.

---

## ❯ Operator caveats

- This fix does NOT retroactively re-classify clusters that hit
  the fallback on 2026-05-23. Those two clusters keep their
  `UNCLASSIFIED` / `AMBIENT` / `low` rows in the DB. If their
  categories matter for the dashboard, manual re-classification
  via the operator override path is the path (step 19+); for now
  they sit at the bottom of the AMBIENT bucket.
- New `Category` values added in future ADR amendments
  automatically pick up normalization · no action needed beyond
  the ADR + the enum addition. The import-time assertion will
  fire if the new value collides with an existing fold key.
- Future Pydantic schemas with Turkish-string-valued enum fields
  (e.g. if `Confidence` ever lands inside a `WriterResponse`)
  must apply the same `field_validator(mode="before")` + fold
  map pattern · documented in `memory/MEMORY.md` § "Turkish
  locale case folding".
- The fold map is keyed by the lowercase-stripped form; if an
  operator wants to add normalization for legitimate
  abbreviations (e.g. "POL" → POLİTİKA) the map can be extended
  by hand at the bottom of `schema.py`, but doing so blurs the
  "fold-equivalent variant" contract · prefer fixing the LLM
  prompt instead.

---

## ❯ Related Docs

- ADR-009 · briefing template / category taxonomy
- ADR-001 · worker LLM contract
- `docs/implementations/2026-05-23-score.md` · original WorkerResponse schema
- `memory/MEMORY.md` § "Turkish locale case folding"
- `docs/implementations/2026-05-24-arc-link-bug-fix.md` · sibling 2026-05-23 smoke-run fix (different bug, same kind of programmatic-defence-against-future-additions discipline)

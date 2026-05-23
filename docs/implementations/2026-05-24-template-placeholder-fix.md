# Implementation: Template placeholder echo fix

**Date** · 2026-05-24
**Author** · Mert Efe Şensoy
**ADR refs** · ADR-009 (briefing template)

---

## ❯ Problem / Motivation

The first end-to-end smoke run on 2026-05-23 produced a briefing in
which two sections · AÇIK GELİŞMELER · DEVAM EDEN TAKİP and AMBİYANS
· DEFCON 5 · contained the literal string
`[içerik buraya · şablon talimatlarına bak]` instead of either real
content or the standard `(bugün öğe yok)` empty-state phrase.

That string is the placeholder text from
`musahit/writer/prompt.py::_template_skeleton` · it tells the writer
LLM where to fill content. Trendyol-LLM 7B Chat v1.8 dutifully filled
some sections (DEFCON 1-2, DEFCON 3, GÜNDEM, SİSTEM LOG) but echoed
the placeholder verbatim into the two it found ambiguous. Worse,
the validator did not catch the echo · the malformed markdown
shipped to disk, then to the TTS stage which synthesised the
sentence "içerik buraya · şablon talimatlarına bak" inside the
briefing.mp3.

The "always-ships" invariant per ADR-012 kept the pipeline alive,
but every cycle would continue to ship corrupt content until fixed.

---

## ❯ Root cause

Three separate bugs in a single class of behaviour. The smoke-run
output looked like one bug but is actually three:

### (a) Single placeholder under every section

`_template_skeleton` used one literal placeholder string for every
section:

```python
parts.extend(["", section.marker, "", "[içerik buraya · şablon talimatlarına bak]"])
```

The placeholder gives no per-section guidance: no hint what the
content should look like, no instruction on the empty-state phrase.
A capable LLM (Trendyol on a routine section like DEFCON 4) fills
it in. A weaker prompt-follower (Trendyol on a section it doesn't
have data for) echoes it verbatim.

### (b) AMBİYANS · DEFCON 5 has no data block

`_clusters_data_block` rendered four buckets: ÖNCELİKLİ (DEFCON
1-2), MATERYAL (DEFCON 3), GÜNDEM (DEFCON 4), YALNIZCA SOSYAL. The
section AMBİYANS · DEFCON 5 in the template has no corresponding
data block. The writer's data section was silent on what should go
into AMBİYANS · the model had nothing to render so it fell back to
the placeholder.

The `BUCKET_AMBIENT = (5,)` constant in `payload.py` already exists
and `_load_clusters` already groups DEFCON 5 clusters under that key
in `BriefingPayload.clusters_by_defcon`. The data was there · the
prompt just never asked for it.

### (c) Validator silent on echoed instruction text

`validate_briefing_markdown` checks structural discipline (section
presence, marker prefix, ordering) but does not look at content.
A briefing whose AMBİYANS body is the literal placeholder text
passes every existing check (the section header is present, marker
is correct, order matches). The bad output shipped silently.

---

## ❯ What Changed

| File | Description |
|---|---|
| `musahit/writer/template.py` | Extended `TemplateSection` with a `prompt_instruction: str` field. Populated each of the 8 sections with Turkish guidance including the section's empty-state phrase. Module docstring updated to explain the new field and its 2026-05-23 motivation. |
| `musahit/writer/prompt.py` | `_template_skeleton` now writes each section's `prompt_instruction` under its marker instead of the single literal placeholder. `_clusters_data_block` gained an `AMBİYANS (DEFCON 5):` block using `BUCKET_AMBIENT` from `payload.py` · renders bullet headlines with kaynak count, or the standard empty-state phrase when no DEFCON 5 clusters are present. `BUCKET_AMBIENT` imported. |
| `musahit/writer/validator.py` | New `_PLACEHOLDER_ECHO_SUBSTRING = "[içerik buraya"` constant + an additional check in `validate_briefing_markdown` that appends `"briefing contains unfilled template placeholder · model echoed the instruction text"` when the substring is present. Match is on the opening fragment so any future trailer tweak still trips the guard. |
| `tests/test_writer/test_template.py` | New `TestPromptInstruction` (4 tests) covering: every section has a non-empty, substantive prompt_instruction; no instruction contains the old placeholder; data-carrying sections include an empty-state phrase; section names stay unique. |
| `tests/test_writer/test_prompt.py` | New `TestTemplateSkeletonInstructions` (2) and `TestAmbientClusterRendering` (3). Skeleton contains every section's instruction; skeleton does NOT contain the old literal placeholder; AMBİYANS bucket renders headlines and the empty-state phrase; the full prompt with an ambient payload stays under the 16K-char budget. |
| `tests/test_writer/test_validator.py` | New `TestPlaceholderEchoRejected` (3) · placeholder-bearing briefings fail with the new error message; partial fragments are caught; clean briefings (whose body uses bare "içerik") still pass. |
| `memory/build-progress.md` | "Step 16 follow-up · template placeholder echo fix · 2026-05-24" entry. |
| `docs/implementations/2026-05-24-template-placeholder-fix.md` | This file. |

---

## ❯ Implementation Approach

### Per-section prompt instruction

`TemplateSection` is a small frozen dataclass that already carried
`marker` (the exact `## ❯ …` text) and `name` (a short identifier
for logs). The new `prompt_instruction` field carries the Turkish
guidance text rendered under the section's marker in the skeleton.

The fix is to make the instruction specific to the section AND
specific about the empty-state. Each one says:

1. What goes here (bullet list / category-grouped / arc summaries /
   metadata reflection).
2. What the format looks like (`### başlık`, `- başlık · kaynak`,
   etc.).
3. The exact empty-state phrase to use when the section has no
   data (`"(bugün öğe yok)"`, `"(bugün güncelleme yok)"`,
   `"(bugün kapatılan yok)"`).

For example, the AÇIK GELİŞMELER instruction reads:

> Bugün güncellenen açık arc'ları sırala · her arc için
> ### başlık · arc_id · zirve DEFCON · kategori · kısa güncelleme
> özeti. Veri yoksa: "(bugün güncelleme yok)".

There is no longer any section where "I don't know what to write"
collapses to "echo the placeholder."

### AMBİYANS data block

`_clusters_data_block` now reads `BUCKET_AMBIENT` clusters and
renders them at the end of the data section:

```python
sections.append("\nAMBİYANS (DEFCON 5):")
if ambient:
    for c in ambient:
        srcs = "(" + str(len(c.sources)) + " kaynak)"
        sections.append(f"- {c.headline} · {srcs}")
else:
    sections.append("(bugün öğe yok)")
```

Bullets are intentionally lighter than DEFCON 4 (no category, no
arc_id) · AMBİYANS is the bottom of the briefing's signal stack
and gets the lowest formatting investment per ADR-009.

### Validator content guard

The new check matches the opening substring `"[içerik buraya"`
because:

- The full placeholder used to be
  `"[içerik buraya · şablon talimatlarına bak]"`. Any near-miss
  fragment (a future tweak to the trailer, a model that types the
  opening and then continues) still trips the guard.
- The phrase is unusual enough that no legitimate content produces
  it · `içerik` alone is a common Turkish word (= content) and
  appears in test fixtures, but `[içerik buraya` with the opening
  bracket immediately followed by the placeholder noun is unique
  to the prompt's old placeholder.

The check fires AFTER the structural checks so structural errors
take priority in the error list · operator sees the markers first,
then the content issue.

---

## ❯ Mathematical / Statistical Details

Not applicable · structural and content fix.

---

## ❯ Design Decisions

### Why three sub-fixes in one PR?

The smoke-run output looks like one bug ("echoed placeholder"). The
root cause analysis shows three independent failures that
*happened* to combine to produce the visible symptom:

- Fix only (a) and AMBİYANS still has no data, so the model with
  the better instruction might write "(bugün öğe yok)" · acceptable
  but doesn't surface real ambient content when it exists.
- Fix only (b) and ambiguous sections still echo the placeholder
  when their data block is empty for legitimate reasons.
- Fix only (c) and the validator catches the placeholder, the
  writer retries with the same prompt, gets the same output, and
  fails after `max_retries` · the briefing falls back to the
  always-ships canned content.

All three fixes belong in one commit because each one closes a
distinct hole; missing any of them leaves a known regression path
for the next smoke run.

### Why a `prompt_instruction` field, not a separate dict in prompt.py?

Alternative: keep `TemplateSection` minimal and put a
`{section.name: instruction}` dict in `prompt.py`. Rejected because:

- The instruction is part of the template's per-section contract
  (what does the writer see for THIS section). Co-locating it with
  marker / name keeps everything about the section in one place
  and forces a future enum addition to add an instruction at the
  same time. A separate dict in another module would let a future
  section land without an instruction and the test would catch it
  after-the-fact.
- The `TemplateSection` dataclass is already frozen and small;
  adding a field is cheap. The validator and the skeleton are
  the only readers · neither needs the instruction (validator
  reads marker, skeleton reads prompt_instruction) so coupling
  remains low.

### Why match only the opening substring in the validator?

Two alternatives considered: (i) match the exact full placeholder
string, (ii) regex `\[içerik buraya.*?\]`. Rejected (i) because
any future tweak to the trailer (`bak` → `bakınız`, the middle dot
spacing changing) would silently bypass the guard. Rejected (ii)
because regex is heavier than needed and the visible failure mode
in the smoke run was always the opening bracket + the unique noun
pair `içerik buraya`. The substring check is the smallest reliable
trigger.

### Why no ADR-009 amendment?

`prompt_instruction` is implementation guidance to the writer LLM ·
it changes nothing about the briefing's external contract
(sections, markers, order, DEFCON 5 ambient bucket existence).
Validators downstream still key off `marker` only. The /goal
tripwire on "ADR-009 needs amendment to document the per-section
instruction field" was checked and cleared.

### Why does the AMBİYANS bullet skip category + arc_id?

ADR-009 calls AMBİYANS "düşük öncelikli gündem" (low-priority
agenda) · the briefing's signal investment in this bucket is
intentionally minimal. The reader skim-and-stops well before
reaching AMBİYANS unless the day's signal is genuinely empty.
Lighter bullets here · headline + source count only · match that
priority. If a future briefing redesign elevates AMBİYANS, the
format change should be an ADR-009 amendment, not a silent edit.

---

## ❯ Verification

```powershell
# Lint clean.
python -m ruff check .

# Writer subsuite · expected: 59 passed (47 prior + 12 new).
$env:PYTHONIOENCODING = "utf-8"
python -m pytest tests/test_writer/ -q

# Full suite stays green · expected: 600 passed, 2 skipped (588 prior + 12 new).
python -m pytest tests/ -q
```

The 12 new tests pin:

1. `TestPromptInstruction` (4 tests) · every TemplateSection has a
   non-empty, substantive prompt_instruction; no instruction
   contains the old placeholder; data-carrying sections include
   an empty-state phrase; section names remain unique.
2. `TestTemplateSkeletonInstructions` (2) · skeleton contains every
   section's prompt_instruction; skeleton does NOT contain the
   old literal placeholder.
3. `TestAmbientClusterRendering` (3) · AMBİYANS bucket renders
   bullet headlines with kaynak counts; renders empty-state when
   no DEFCON 5 clusters present; prompt with ambient payload stays
   under the 16K-char size budget.
4. `TestPlaceholderEchoRejected` (3) · briefings with the literal
   placeholder fail with the new error message; partial fragments
   are caught; clean briefings whose body uses bare "içerik" still
   pass.

---

## ❯ Operator caveats

- The 2026-05-23 briefing artifact on disk still contains the
  echoed placeholder text. The next smoke run will produce a clean
  one; the 2026-05-23 file is best left as-is (operator log of the
  bug · do NOT regenerate retroactively, the audit trail matters).
- If the writer model is swapped from Trendyol to a different
  Turkish-tuned model, the per-section instruction text in
  `TemplateSection.prompt_instruction` may need tuning · the new
  model's prompt-following pattern is the variable. The validator
  guard catches a regression of this class regardless of model.
- Adding a new TemplateSection in a future ADR-009 amendment
  requires populating its `prompt_instruction` field; the
  `TestPromptInstruction::test_every_section_has_non_empty_prompt_instruction`
  test fails loudly if forgotten.
- The AMBİYANS bullet format (headline + kaynak count, no category
  or arc) is deliberate · matching ADR-009's "düşük öncelikli
  gündem" framing. Any format upgrade should go through ADR-009
  rather than silently editing `_clusters_data_block`.

---

## ❯ Related Docs

- ADR-009 · briefing template
- ADR-012 · always-ships invariant
- `docs/implementations/2026-05-23-write.md` · original writer build
- `docs/implementations/2026-05-24-arc-link-bug-fix.md` · sibling smoke-run fix (arcs)
- `docs/implementations/2026-05-24-category-normalization.md` · sibling smoke-run fix (LLM diacritic folding)

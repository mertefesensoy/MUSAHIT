# MÜŞAHİT · project memory index

Per global CLAUDE.md: each project maintains this file. Summaries are in the
linked files; this index gives the one-line hook.

| File | Contents |
|---|---|
| [build-progress.md](build-progress.md) | Build order completion status and key decisions per step |

---

## FILE-PROTECTED list

Files locked behind ADR amendments + explicit operator override:

* `musahit/ingest/sources.py` (step 3, ADR-003)
* `musahit/ingest/poller.py` (step 8, ADR-001/003/012)
* `musahit/score/defcon.py` (step 11, ADR-004)
* `musahit/score/promotion.py` (step 11, ADR-005)
* Any ADR file (`ADR-NNN-*.md` at repo root)


## Conventions

### Enum expansion is an ADR amendment, never a silent code change

Closed enumeration sets in this project — `Band`, `Tier`, `Category`,
`DEFCON` (when defined in step 11), `ArcState`, `GazetteSection`,
`GazetteItemType`, `OverrideAction`, and any future closed set — are
load-bearing for downstream stages: promotion rules, briefing template
slots, dashboard tabs, audit logs. Adding a value silently breaks
exhaustiveness checks somewhere else in the pipeline.

**Rule:** if real data surfaces a gap, the gap is reported (via
`ingest_log` for ingest gaps, via a new ADR amendment for taxonomy
gaps). The enum is expanded only by an ADR that names the new member
and lists the downstream code paths that must update with it. Never
add a member in the same PR that introduces the use case for it
without the matching ADR.

This applies symmetrically: removing or renaming a member is also an
ADR amendment.

### Turkish locale case folding requires explicit translate before .lower()

Python's `str.lower()` mishandles the two locale-specific Turkish
letters:

* `"İ".lower() == "i̇"` (i + combining dot above) — not `"i"`.
* `"I".lower() == "i"` — wrong for Turkish, which wants `"ı"` (dotless).

`str.casefold()` has the same problem. Code that needs Turkish-aware
case-insensitive matching MUST pre-translate `{"İ": "i", "I": "ı"}`
before calling `.lower()`:

```python
_TR_LOWER_PRE = str.maketrans({"İ": "i", "I": "ı"})

def tr_lower(s: str) -> str:
    return s.translate(_TR_LOWER_PRE).lower()
```

Apply to **both** the matched text and any vocabulary/pattern before
comparing. Used today in `musahit/normalize/entities.py`; reuse the
helper there for any new locale-aware matching.

### ADR semantic intent overrides formula text

When ADR prose and formula disagree, trace through worked examples to
identify which matches the ADR's stated purpose · the disagreeing side
is the bug · file an ADR amendment.

Discovered when ADR-005's `min(raw, ceiling)` formula was found to not
enforce the ceiling as a severity cap despite the prose claiming it did.
The implementation faithfully encoded the formula; the tests validated
the formula against itself; neither caught the bug because neither
checked against the three semantic intents (X cap · single-band cap ·
primary non-auto-promotion). The 2026-05-23 amendment switched the
formula to `max(raw, ceiling)` and recomputed every pinned test value.

Process for the next discovery:

1. Write the disagreement out as a table — columns: input, current
   formula result, alternative formula result, ADR-intended result.
2. The column matching the ADR's stated purpose is the correct formula.
3. The other formula is the bug — file an ADR amendment.
4. Update the implementation, walk every test that pins a derived
   value, and recompute under the corrected formula. Add inline
   comments on each updated assertion explaining the new direction.
5. Do NOT weaken assertions to make tests pass. If a test resists
   the change, that's evidence the ADR's stated semantics are more
   complex than the three cases — surface it before committing.


### DuckDB FK Update Pattern

DuckDB enforces foreign key constraints on UPDATE statements even
outside explicit transactions. This means `UPDATE clusters SET ...
WHERE id = ?` fails when `cluster_articles` or `cluster_embeddings`
rows reference that `cluster_id` · the FK check fires on the update
itself, not at commit time.

The pattern when updating a parent row that has active FK children:

    DELETE FROM child_table WHERE parent_id = ?
    UPDATE parent_table SET ... WHERE id = ?
    INSERT INTO child_table VALUES ?, ?, ...

Each statement auto-commits. No explicit `BEGIN`/`COMMIT`. Inside a
transaction the FK check still fires · the workaround does not need
transaction semantics because DuckDB handles per-statement atomicity.

Applies to:

* `clusters` update when `cluster_articles` or `cluster_embeddings`
  rows reference it (step 11 · step 12)
* `arcs` update when `arc_centroids` references it (step 12)
* any future parent table update with active child references

Discovered in step 11 · documented in
`docs/implementations/2026-05-23-score.md`.


### ADR-016 trigger: vocabulary-vs-transformer NER

The current entity tagger (step 9) uses a curated vocabulary instead of
spaCy or a Turkish NER transformer. Write **ADR-016** to reconsider the
decision if either of the following holds during the first month of
operation:

* More than 20% of clusters lack any tagged entity in the briefing.
* The operator manually tags missed entities more than 3 times per week
  (this signal lives in `manual_overrides` once the dashboard's
  override workflow is in place — step 19+).

ADR-016 would propose either expanding the vocabulary or adding a
transformer-NER fallback for opt-in long-tail coverage. Until the
trigger fires the vocabulary stays — it costs nothing per night and
covers the briefing's highest-signal names.

### Audio QA is a real review stage

Unit tests prove the TTS pipeline doesn't crash and that text 
transformations fire as specified. They cannot prove that the resulting
audio is acceptable to listen to. After any change touching extractor,
preprocessor, or piper modules, regenerate the diagnostic MP3 and 
listen end-to-end before moving to the next step. The DEFCON 
respelling and regex tightening on 2026-05-23 are the canonical 
example: 525 tests stayed green across three commits while three 
distinct pronunciation issues were caught and fixed by ear alone.


### No em or en dashes

Middle dot (·) is the only permitted separator across the codebase ·
docs · scripts · ADRs · briefings. Em dashes (—) and en dashes (–)
are project convention violations. This convention is carried over
from SuperconducTED and applies to every file in this repo
including PowerShell scripts and operator documentation. If
Claude Code finds itself using an em dash, that is a signal to stop
and convert it to a middle dot before committing.

Strict · no exceptions for numeric ranges, prose pauses, or table
cells. The character set is U+2014 (em dash) and U+2013 (en dash);
both must be replaced with U+00B7 (middle dot). The convention was
discovered the hard way during the first smoke run on 2026-05-23:
an em dash in `scripts/run_first_smoke.ps1` was saved as multi-byte
UTF-8 bytes that PowerShell mis-decoded under the Windows-1252
console codepage, producing a parse error. PowerShell scripts (.ps1)
with non-ASCII characters MUST be saved as UTF-8 with BOM
(`utf-8-sig` / `Out-File -Encoding utf8BOM`) so the parser reads
multi-byte sequences correctly regardless of the active codepage.

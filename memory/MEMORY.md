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

# ADR-005 · Bias promotion rules

**Status** · Accepted · 2026-05-22
**Author** · Mert Efe Şensoy
**Supersedes** · none
**Cross-references** · ADR-003 · ADR-004

> **Amended** · 2026-05-23 · prose clarification on ceiling directionality
> **Amended** · 2026-05-23 · formula correction · final_defcon uses
> max(raw, ceiling) · the prior min formula was a latent bug that did
> not enforce the ceiling as a severity cap · the implementation and
> tests are updated in this same commit

---

## ❯ Context

Turkish media is heavily polarized. A story reported only by gov-aligned outlets has a
different epistemic weight than one reported across both gov-aligned and opposition
outlets. If MÜŞAHİT promotes single-band stories to high DEFCON, the briefing becomes a
mirror of whichever ideological camp produced the most noise that day.

The operator explicitly added a rule for X: X content carries a "high bias variable"
because the platform is a firehose of partisan signaling, organized trolling, and
disinformation from multiple actors. X virality must not promote a story.

## ❯ Decision

The score stage produces a **raw DEFCON** from the worker model. The promotion stage then
applies **band-based ceiling rules** to compute the **final DEFCON**. Both are stored. The
final DEFCON drives the briefing template and the audio output.

### Promotion ceiling rules

Given a cluster with `bands_present: set[Band]` (the union of bands across all sources
that contributed to the cluster):

```python
def compute_ceiling(bands: set[Band]) -> DEFCON:
    # Primary sources override everything
    if any(b in PRIMARY_BANDS for b in bands):
        return DEFCON.UNTHINKABLE  # primary sources can promote to any level

    # X-only stories are hard-capped at DEFCON 4
    non_social = bands - {Band.SOCIAL_X, Band.SOCIAL_REDDIT}
    if not non_social:
        return DEFCON.ROUTINE  # 4

    # Count ideological sides represented
    sides_present = ideological_sides(bands)

    if len(non_social) >= 3 and len(sides_present) >= 2:
        return DEFCON.ACUTE       # 1 · eligible for high severity
    elif len(non_social) >= 2:
        return DEFCON.MATERIAL    # 3 · eligible for material
    else:
        return DEFCON.ROUTINE     # 4 · single-band stories capped at routine
```

### Constants

```python
PRIMARY_BANDS = {
    Band.PRIMARY_GOV,
    Band.PRIMARY_MARKET,
    Band.PRIMARY_JUDICIAL,
}

IDEOLOGICAL_SIDES = {
    "gov":           {Band.GOV_ALIGNED},
    "opposition":    {Band.OPPOSITION},
    "neutral":       {Band.CENTRIST, Band.INDEPENDENT, Band.INTERNATIONAL},
}

def ideological_sides(bands: set[Band]) -> set[str]:
    return {
        side for side, members in IDEOLOGICAL_SIDES.items()
        if bands & members
    }
```

### Final DEFCON computation

```python
final_defcon = max(raw_defcon, ceiling)

# Reads as: final cannot be more severe than the corroboration ceiling
# supports. Since lower integers are more severe, max enforces this
# by taking whichever rating is LESS severe (higher integer).
```

The ceiling produces a *minimum allowable DEFCON integer*. Since lower DEFCON integers
mean higher severity (DEFCON 1 is more severe than DEFCON 4), the ceiling effectively
*caps how severe* a cluster can be rated regardless of the worker's raw classification.
The expression `final_defcon = max(raw, ceiling)` enforces this: when the worker rates
a cluster as DEFCON 2 (severe) but the band corroboration only supports ceiling=DEFCON 4
(routine), the final is DEFCON 4 because the integer 4 is greater than 2 · the ceiling
capped severity at routine despite the worker's higher rating. This is intentional: a
worker model scoring a routine cabinet meeting as DEFCON 2 should not be escalated even
if it's covered by every band. Raw classification is the floor.

DEFCON 0 is treated specially: it requires both `raw_defcon == 0` AND a manual operator
override. The system never auto-promotes to 0.

### X high-bias rule formalized

- X is band `social_x` · ceiling is DEFCON 4 (`ROUTINE`) for any cluster that contains
  *only* social bands
- An X-corroborated cluster (where mainstream news also reports it) keeps its ceiling
  based on the *non-social* bands · X content does not contribute to ceiling computation
- X content *can* be cited in the briefing as evidence of public reaction · but never as
  evidence of the underlying event

### Reddit handling

Reddit (`social_reddit`) follows the same rule as X: hard cap at DEFCON 4 when the
cluster is social-only. Reddit's signal value is in surfacing things that haven't yet
hit mainstream news. If something appears only on Reddit, it stays at DEFCON 4 but is
flagged in the briefing's `DİKKAT` section for the operator to watch.

### Confidence tag

Each cluster gets a confidence tag for display:

```python
def confidence(bands: set[Band], num_sources: int) -> Confidence:
    if any(b in PRIMARY_BANDS for b in bands):
        return Confidence.YUKSEK     # YÜKSEK · primary source
    sides = ideological_sides(bands)
    if len(sides) >= 2 and num_sources >= 4:
        return Confidence.YUKSEK
    if len(sides) >= 2 or num_sources >= 3:
        return Confidence.ORTA       # ORTA
    return Confidence.DUSUK          # DÜŞÜK
```

Confidence tags appear next to every item in the briefing.

### Promotion log

Every promotion decision is logged to the `promotion_log` table:

```sql
CREATE TABLE promotion_log (
    cluster_id      TEXT,
    raw_defcon      INTEGER,
    ceiling_defcon  INTEGER,
    final_defcon    INTEGER,
    bands_present   TEXT,    -- JSON array
    sides_present   TEXT,    -- JSON array
    confidence      TEXT,
    rule_applied    TEXT,    -- which branch of compute_ceiling fired
    computed_at     TIMESTAMP
);
```

This log is auditable. The operator can challenge a promotion decision by querying this
table for any cluster.

## ❯ Consequences

**Positive**
- The system is explicit about the editorial role bands play in severity
- X cannot dominate the briefing through coordinated virality
- Primary sources have their proper override status · Resmi Gazete decrees, TCMB
  announcements, and court decisions can promote to any level alone
- Every promotion decision is logged and auditable

**Negative**
- Single-band stories that are genuinely severe (e.g., an exclusive investigative report
  from one independent outlet) are capped at DEFCON 4 · the operator must manually
  promote via dashboard if warranted · the audit log preserves the manual override
- The rule is conservative · errs toward under-promoting · this is intentional but means
  some real stories will arrive at DEFCON 4 when they deserve DEFCON 3 until more sources
  pick them up

## ❯ Alternatives considered

- **No promotion rules · trust the worker** · rejected · single-band echo chamber risk
- **Weighted scoring with continuous band weights** · over-engineered · revisit if needed
- **Auto-promote on manual override patterns** · rejected for v0.1 · operator's
  override history could be machine-learned for calibration · deferred

## ❯ Open questions

- Independent investigative reports from single outlets are a known edge case · monitor
  manual override frequency · if it exceeds 3/week, consider adding an `INVESTIGATIVE`
  band exemption
- Whether primary sources should require 2 confirmations for DEFCON 0 promotion · the
  current rule allows any primary source to elevate to any level · DEFCON 0 is gated by
  the override flag separately so this is acceptable

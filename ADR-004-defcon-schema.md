# ADR-004 · DEFCON schema

**Status** · Accepted · 2026-05-22
**Author** · Mert Efe Şensoy
**Supersedes** · none
**Cross-references** · ADR-002 · ADR-005 · ADR-009

---

## ❯ Context

The system must rank items by severity so the operator can read the briefing top-down and
stop at any point with the highest-priority items already absorbed. A continuous score
(0.0-1.0) is unhelpful for the operator because it requires interpretation. A discrete
ladder with concrete real-world anchors lets the worker model calibrate consistently and
lets the operator skim by category.

Military DEFCON convention runs 5 (peace) to 1 (nuclear war imminent). MÜŞAHİT preserves
this direction. The operator requested a DEFCON 0 tier above 1 as a theoretical ceiling.

## ❯ Decision

A discrete six-level severity ladder · DEFCON 0 through DEFCON 5 · with concrete
Turkey-specific historical anchors for calibration. The schema is declared as Python enums
and constants in `src/score/defcon.py` (FILE-PROTECTED).

### Levels

#### DEFCON 0 · DÜŞÜNÜLEMEZ

Tier so severe that anything landing here is the end-state of the system's purpose.
Effectively never assigned. The model is instructed that 0 requires multi-source primary
confirmation and a manual operator override flag.

**Calibration anchors (theoretical)**:
- Successful overthrow of constitutional order · government dissolved · military takes
  formal command
- Akkuyu nuclear incident with radioactive release
- Direct hot war with neighboring state (Greece, Syria escalated to full state-on-state)
  or with Russia
- Sovereign formal exit from NATO
- Mass-casualty terror attack with state-level paralysis (>500 casualties)
- Presidential assassination
- Hyperinflation transition · formal currency replacement · TL abandonment
- Civil war or armed insurrection sustained beyond 72 hours

**Promotion to DEFCON 0 requires** · 3+ primary sources confirming · operator override
flag in `manual_overrides` table · cannot be auto-assigned by writer or scorer

---

#### DEFCON 1 · AKUT

Constitutional rupture · attempted or successful coup · presidential incapacitation ·
martial law · suspension of constitution · sovereign default · capital controls imposed ·
large-scale armed conflict on Turkish soil.

**Anchor examples**:
- 15 Temmuz 2016 darbe girişimi
- 12 Eylül 1980 darbesi
- 1971 muhtırası

**Eligibility** · requires 3+ bands across at least 2 ideological sides · or primary
source confirmation

---

#### DEFCON 2 · ŞİDDETLİ

Major political event with national consequence. Party closure proceedings · referendum
announcements · cabinet collapse · Constitutional Court suspending core laws · TCMB
emergency action outside scheduled meetings · mass arrests of elected officials ·
disputed election results.

**Anchor examples**:
- İmamoğlu mahkumiyet ve hapis cezası
- 23 Haziran 2019 İstanbul yenileme
- CHP genel merkez baskını (2025)
- HDP kapatma davası
- 2018 Ağustos kur krizi · 500+ bps acil faiz hareketi

**Eligibility** · requires 3+ bands across at least 2 ideological sides · or primary
source confirmation

---

#### DEFCON 3 · MATERYAL

Scheduled MPC decisions · cabinet reshuffles · major court rulings · diplomatic incidents
(ambassador summoned or recalled) · large protests · BIST circuit breakers · TL daily
move >2% · regulatory shock from BDDK/SPK/EPDK · significant Resmi Gazete decrees.

**Anchor examples**:
- İstanbul Sözleşmesi'nden çekilme
- S-400 ABD yaptırım haberleri
- TCMB sürpriz 500 baz puan faiz kararı
- Berat Albayrak istifası

**Eligibility** · requires 2+ bands · or primary source

---

#### DEFCON 4 · GÜNDEM

Daily news cycle · routine market activity · ministerial statements · municipal news ·
ongoing investigation updates · scheduled regulatory announcements · TÜİK monthly data ·
routine TCMB communication.

**Anchor examples**:
- Monthly enflasyon açıklaması
- Standard cabinet meeting summary
- Regular parliamentary committee output

**Eligibility** · any source (single band OK)

---

#### DEFCON 5 · AMBIYANS

Background noise. X discourse · minor stories · opinion columns · cultural-political
subtext only · routine party congress events.

**Anchor examples**:
- Opinion piece by columnist
- Routine party congress
- X virality without underlying news event
- Reddit thread without primary source

**Eligibility** · default for unverified or single-source content

---

### Schema constants

```python
# src/score/defcon.py · FILE-PROTECTED

from enum import IntEnum

class DEFCON(IntEnum):
    UNTHINKABLE = 0   # DÜŞÜNÜLEMEZ
    ACUTE       = 1   # AKUT
    SEVERE      = 2   # ŞİDDETLİ
    MATERIAL    = 3   # MATERYAL
    ROUTINE     = 4   # GÜNDEM
    AMBIENT     = 5   # AMBIYANS

DEFCON_LABEL_TR = {
    DEFCON.UNTHINKABLE: "DÜŞÜNÜLEMEZ",
    DEFCON.ACUTE:       "AKUT",
    DEFCON.SEVERE:      "ŞİDDETLİ",
    DEFCON.MATERIAL:    "MATERYAL",
    DEFCON.ROUTINE:     "GÜNDEM",
    DEFCON.AMBIENT:     "AMBİYANS",
}

DEFCON_REQUIRES_OVERRIDE = {DEFCON.UNTHINKABLE}
```

### Worker model prompting

The classification prompt for Qwen2.5 7B carries the full DEFCON definitions and anchor
examples. The worker is instructed to:

1. Read the cluster's headlines and lead paragraphs
2. Identify the event being reported
3. Compare against the anchor examples for each level
4. Output the **raw DEFCON score** without considering promotion rules
5. Output its **confidence** in the classification (high/medium/low)

The promotion stage (ADR-005) then applies band-based ceilings to compute the final
DEFCON. The raw score and the ceiling-adjusted score are both stored.

### Bootstrap period calibration

During the first 7 days (per BOOTSTRAP.md), all DEFCON scores ride one ceiling lower than
computed. This protects against the system over-promoting events while it learns its
operator's preferences. The operator's manual overrides during this period inform
post-bootstrap calibration.

## ❯ Consequences

**Positive**
- Operator can skim the briefing top-down and stop at any severity threshold
- Worker model has concrete reference points · classification is consistent across
  multiple runs
- DEFCON 0 serves as a calibration ceiling · DEFCON 1 cannot drift upward into "actually
  this is unthinkable" territory because that tier is explicitly reserved
- Schema is small (6 levels) · operator memorizes it after one week

**Negative**
- Anchor examples are operator-curated · they reflect the operator's political reading
  of Turkish history · revisable via ADR amendment
- DEFCON 0 may never be used · arguably could be removed · the operator preferred to
  keep it as a ceiling reference for the worker model

## ❯ Alternatives considered

- **Continuous 0.0-1.0 score** · rejected · operator skim experience matters more than
  scoring precision
- **Three-tier severity (low/medium/high)** · rejected · loses the calibration value of
  having a hierarchy with anchors
- **DEFCON levels reversed (1 = low, 5 = high)** · rejected · military convention is more
  evocative and the operator already understands it

## ❯ Open questions

- Whether DEFCON 2 and DEFCON 3 anchors need refinement after first month of operation ·
  revisit in operator review
- Whether the schema needs a sub-category for **economic** severity that's decoupled from
  political severity · currently a single ladder · revisit if economic events get
  consistently misclassified

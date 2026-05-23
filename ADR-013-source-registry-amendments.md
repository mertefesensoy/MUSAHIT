# ADR-013 · Source registry amendments

**Status** · Accepted · 2026-05-23
**Author** · Mert Efe Şensoy
**Supersedes** · ADR-003 (two decisions — see below)
**Cross-references** · ADR-003 · ADR-005

---

## ❯ Context

During implementation of `musahit/ingest/sources.py` (build order step 3) the operator
reviewed the ADR-003 source list and identified two decisions that must change before the
file is committed and locked. This ADR formally supersedes those specific decisions while
leaving all other ADR-003 content intact and locked.

---

## ❯ Amendment 1 · Bloomberg HT band: INTERNATIONAL → CENTRIST

### ADR-003 decision

Bloomberg HT was classified as `Band.INTERNATIONAL` alongside DW Türkçe, BBC Türkçe,
VOA Türkçe, Euronews Türkçe, Reuters Turkey, and AP Turkey.

### Amendment

`bloomberg_ht.band = Band.CENTRIST`

### Rationale

Bloomberg HT is operated by Demirören Media Group, a Turkish conglomerate with cross-sector
holdings including energy, real estate, and media. Its editorial line reflects domestic Turkish
business and financial interests and tracks the mainstream centrist/mild pro-government-adjacent
posture common to large Turkish media groups — not the external-observer framing that defines
the INTERNATIONAL band. Classifying it as INTERNATIONAL would place it in the same band as
foreign-language news services whose Turkey coverage is explicitly framed from outside the
domestic political conversation.

The CENTRIST band correctly positions Bloomberg HT alongside Hürriyet, Milliyet, and NTV for
promotion ceiling purposes in ADR-005.

### Impact on promotion rules (ADR-005)

Reclassification moves bloomberg_ht from the `INTERNATIONAL` subtype of the `neutral` side
(`IDEOLOGICAL_SIDES["neutral"]`) to the same slot — CENTRIST is also in the `neutral` side.
Promotion ceiling arithmetic is unchanged. The change affects editorial framing in the briefing
template (ADR-009): Bloomberg HT's coverage will no longer be tagged as an international source.

---

## ❯ Amendment 2 · x_stub: Source instance not created

### ADR-003 decision

"The `x_stub` source exists in `sources.py` with `kind=DEFERRED`. The poller skips deferred
sources but the rest of the pipeline (promotion ceilings, dashboard tabs) is built as if X
were live."

### Amendment

No `Source` object for X is created in `sources.py`. The `Band.SOCIAL_X` enum value and
the `SourceKind.DEFERRED` enum value remain defined in `musahit/common/types.py` for use
when an X strategy is eventually chosen.

### Rationale

A `Source` object with `kind=DEFERRED` that can never be ingested is a dead entry in the
source registry. It would appear in the dashboard's source health view as a permanently
disabled source, confuse the operator during the bootstrap period, and make the
`sources.py` index (and its tests) assert on a source with no operational meaning. The
promotion ceiling and dashboard infrastructure for X will be built when an ingest strategy
is chosen — at that point, adding a `Source` to `sources.py` requires only an ADR amendment
(this one, referencing the chosen strategy), which is the correct change-control gate.

### Impact

`SOURCES_BY_ID` does not contain `"x_stub"`. Tests assert `"x_stub" not in SOURCES_BY_ID`
and `not any(s.kind == SourceKind.DEFERRED for s in SOURCES)`.

---

## ❯ Other ADR-003 decisions — unchanged

All other entries in ADR-003 are implemented verbatim, including:

- `reuters_tr` · `kind=RSS` · uses the global Reuters world-news feed with Turkey-topic
  filtering during the normalize stage (not a Turkey-specific feed; no Turkey-specific
  Reuters RSS exists as of 2026)
- `ap_tr` · `kind=HTML` · HTML scrape as specified in ADR-003
- `kap` · `kind=RSS` · RSS feed exists; specific URL not confirmed at scaffold time;
  marked `notes="URL pending operator verification"` until confirmed

---

## ❯ Alternatives considered

**Reclassify Bloomberg HT as GOV_ALIGNED** · Rejected. Demirören ownership does not
mean Bloomberg HT follows the same editorial posture as Sabah, A Haber, or Anadolu Ajansı.
The financial coverage is commercially driven and the editorial line is more cautious than
the explicitly pro-government outlets.

**Create x_stub with kind=DEFERRED but exclude from seeding** · Rejected. A `Source`
dataclass in the registry but not in the DB is a confusing split that future code would
have to special-case. The cleaner state is: the band and kind enums reserve the slot;
the source object and DB row come into existence together when ingestion is ready.

## ❯ Open questions

- When an X ingest strategy is chosen, this ADR will be cross-referenced in the amendment
  that re-enables x_stub.
- The Bloomberg HT band decision may need revisiting if Demirören's editorial posture shifts
  significantly. The operator reviews band assignments monthly in the first quarter.

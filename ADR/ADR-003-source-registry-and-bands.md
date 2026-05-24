# ADR-003 · Source registry and bands

**Status** · Accepted · 2026-05-22
**Author** · Mert Efe Şensoy
**Supersedes** · none
**Cross-references** · ADR-005 · ADR-012

---

## ❯ Context

Turkish media is heavily polarized. Any synthesis of "what happened in Turkey today" that
draws from only one ideological camp produces a distorted briefing. The system must
explicitly model the ideological position of each source and use that modeling in the
promotion rules (ADR-005) to require cross-band corroboration before elevating severity.

The operator approved a full-scope source list across news, markets, government primary
feeds, and limited social (Reddit and X). Telegram was explicitly excluded.

## ❯ Decision

A static **source registry** at `src/ingest/sources.py` enumerates every configured source
with the following fields:

```python
@dataclass(frozen=True)
class Source:
    id: str                # slug · e.g. "anadolu" · "resmi_gazete"
    display_name: str      # human label · "Anadolu Ajansı"
    band: Band             # ideological band · see below
    tier: Tier             # NEWS · MARKETS · GOV · SOCIAL
    kind: SourceKind       # RSS · HTML · PDF · API
    url: str               # primary URL · feed or scrape root
    rate_limit_seconds: int  # per-fetch delay
    fragility: Fragility   # ROBUST · MEDIUM · FRAGILE
    notes: str             # operator notes · scraping quirks · etc.
```

### Band taxonomy

Bands are the ideological position of the publication. Every news source carries exactly
one band. Primary sources (gov-published documents) carry the `PRIMARY_*` band, which has
override status in promotion rules.

```python
class Band(StrEnum):
    GOV_ALIGNED      = "gov_aligned"        # state or pro-government editorial line
    CENTRIST         = "centrist"           # mainstream, varying allegiance
    OPPOSITION       = "opposition"         # opposition-aligned editorial line
    INDEPENDENT      = "independent"        # explicitly non-aligned, often left-liberal
    INTERNATIONAL    = "international"      # foreign press covering Turkey
    SOCIAL_X         = "social_x"           # hard cap DEFCON 4 · high bias variable
    SOCIAL_REDDIT    = "social_reddit"      # hard cap DEFCON 4 · curated subs
    PRIMARY_GOV      = "primary_gov"        # Resmi Gazete, TCMB, presidential decrees
    PRIMARY_MARKET   = "primary_market"     # KAP, BIST, TÜİK
    PRIMARY_JUDICIAL = "primary_judicial"   # AYM, Yargıtay, Danıştay
```

### Tier taxonomy

Tiers describe the type of content and influence ingestion strategy.

```python
class Tier(StrEnum):
    NEWS    = "news"       # editorial content · ~20 sources
    MARKETS = "markets"    # economic and financial · ~5 sources
    GOV     = "gov"        # primary government sources · ~5 sources
    SOCIAL  = "social"     # X and Reddit · ~2 sources
```

### Full source list (locked for v0.1)

#### NEWS

| ID | Name | Band | Kind |
|---|---|---|---|
| anadolu | Anadolu Ajansı | gov_aligned | RSS |
| trt_haber | TRT Haber | gov_aligned | RSS |
| sabah | Sabah | gov_aligned | RSS |
| yeni_safak | Yeni Şafak | gov_aligned | RSS |
| a_haber | A Haber | gov_aligned | RSS |
| hurriyet | Hürriyet | centrist | RSS |
| milliyet | Milliyet | centrist | RSS |
| ntv | NTV | centrist | RSS |
| cumhuriyet | Cumhuriyet | opposition | RSS |
| sozcu | Sözcü | opposition | RSS |
| birgun | BirGün | opposition | RSS |
| halk_tv | Halk TV | opposition | RSS |
| t24 | T24 | independent | RSS |
| diken | Diken | independent | RSS |
| duvar | Gazete Duvar | independent | RSS |
| bianet | Bianet | independent | RSS |
| medyascope | Medyascope | independent | RSS |
| dw_tr | DW Türkçe | international | RSS |
| bbc_tr | BBC Türkçe | international | RSS |
| voa_tr | VOA Türkçe | international | RSS |
| euronews_tr | Euronews Türkçe | international | RSS |
| reuters_tr | Reuters Turkey | international | RSS |
| ap_tr | AP Turkey | international | HTML |
| bloomberg_ht | Bloomberg HT | international | RSS |

#### MARKETS

| ID | Name | Band | Kind |
|---|---|---|---|
| tcmb | Türkiye Cumhuriyet Merkez Bankası | primary_market | HTML |
| kap | Kamuyu Aydınlatma Platformu | primary_market | RSS |
| bist | Borsa İstanbul | primary_market | HTML |
| tuik | Türkiye İstatistik Kurumu | primary_market | HTML |
| dunya | Dünya Gazetesi | centrist | RSS |
| mahfi | Mahfi Eğilmez blog | independent | RSS |

#### GOV

| ID | Name | Band | Kind |
|---|---|---|---|
| resmi_gazete | T.C. Resmi Gazete | primary_gov | PDF |
| tbmm | TBMM gündem | primary_gov | HTML |
| cumhurbaskanligi | Cumhurbaşkanlığı kararnameleri | primary_gov | HTML |
| anayasa_mahkemesi | Anayasa Mahkemesi | primary_judicial | HTML |
| yargitay | Yargıtay | primary_judicial | HTML |
| danistay | Danıştay | primary_judicial | HTML |

#### SOCIAL

| ID | Name | Band | Kind |
|---|---|---|---|
| reddit_turkey | r/Turkey · r/TurkeyJerky · r/AskTurkey | social_reddit | API |
| x_stub | X / Twitter | social_x | DEFERRED |

### X (Twitter) honesty note

The free X API was discontinued in 2023. As of 2026-05-22, ingestion options are:

- **Paid X API Basic** · ~$100/month · 10K reads/month · rate-limited but stable
- **Scraping** · fragile · ToS violation · breaks weekly · not recommended
- **Skip** · accept that X is structurally deferred

**Decision** · the band slot for `social_x` stays reserved in the schema. The `x_stub`
source exists in `sources.py` with `kind=DEFERRED`. The poller skips deferred sources but
the rest of the pipeline (promotion ceilings, dashboard tabs) is built as if X were live.
When the operator chooses an X strategy, only the ingest implementation needs to be added.

### Reddit ingestion

- Library · `PRAW` · official Python Reddit API Wrapper
- Credentials · `.env` · `REDDIT_CLIENT_ID` · `REDDIT_CLIENT_SECRET` · `REDDIT_USER_AGENT`
- Subreddits ingested · `r/Turkey` · `r/TurkeyJerky` · `r/AskTurkey` · `r/europe` (Turkey
  posts filtered by flair/title)
- Filter · posts in the last 24h with score ≥ 50 OR comment count ≥ 25
- Output · headline + selftext (truncated 500 chars) + top 3 comments (truncated 200 each)
- Band · `social_reddit` · hard cap DEFCON 4

### Ingestion strategy

- **RSS** · `feedparser` · poll every source's feed once per night at stage 1
- **HTML** · `httpx` + `selectolax` · listing page parse + per-article fetch
- **PDF (Resmi Gazete)** · `pdfplumber` · daily edition fetched at 02:00 (Resmi Gazete
  posts late evening for next day's edition)
- **PRAW (Reddit)** · subreddit listings + selected posts
- **Polite scraping** · `User-Agent: MUSAHIT/0.1 (personal OSINT; mert@...)` · respect
  robots.txt · 5-second default delay between requests to the same host · explicit
  per-source rate limits in registry

### Fragility classification

- **ROBUST** · stable RSS feed · changes rarely · `feedparser` should keep working
- **MEDIUM** · HTML scrape · selectors may need updates · monitor monthly
- **FRAGILE** · X · paywalled or rate-limited · expected to break

### Failure isolation

Per ADR-012, a failed source produces an `ingest_log` row with the failure reason and is
listed in the briefing's `SİSTEM LOG · failed sources` section. The pipeline continues.

## ❯ Consequences

**Positive**
- Bands are explicit, machine-readable, and drive the promotion rules in ADR-005
- Adding a source is a one-line addition to `sources.py`
- The fragility tag lets the operator anticipate which sources will need maintenance
- Primary sources (gov-published documents) get their own override path

**Negative**
- Band assignment is a judgment call · operator owns the editorial decision · reviewable
  in ADR amendments
- Some sources blur band lines (Hürriyet has fluctuated · NTV similarly) · classified as
  `centrist` with the understanding that band drift may require ADR amendment
- X deferral means the most populous source of public opinion is absent · the briefing
  acknowledges this in the dashboard footer

## ❯ Alternatives considered

- **Dynamic band assignment via LLM** · rejected · bands are slow-changing and editorial ·
  static registry is more honest and easier to audit
- **Confidence-weighted bands instead of binary** · interesting but over-engineered for
  v0.1 · revisit if needed
- **Include Telegram** · operator explicitly excluded · skipped

## ❯ Open questions

- The international source list mixes Turkish-language and English-language feeds · the
  normalization stage must handle language detection · open in ADR-013 if a problem
- Whether `centrist` should be split into `mainstream_pro` and `mainstream_neutral` ·
  deferred · revisit after first month of operation

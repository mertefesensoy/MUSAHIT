# ============================================================================
# FILE-PROTECTED · musahit/ingest/sources.py
# Modifications require an ADR amendment + explicit operator override.
# See BOOTSTRAP.md § File protection list, ADR-003, and ADR-013.
# Operator override 2026-05-25 · danistay dropped (architecturally
# unreachable · see _GOV comment block below).
# ============================================================================
"""Static source registry for MÜŞAHİT.

Defines all 36 configured sources with their band, tier, kind, URL, and
operational metadata. Two decisions in ADR-003 are superseded by ADR-013:
bloomberg_ht.band is CENTRIST (not INTERNATIONAL) and x_stub is not created.
One source (``danistay``) was dropped 2026-05-25 per the curl_cffi roadmap:
its listing page is JS-rendered (selectolax cannot parse it) and the
ingester would need a Playwright back-end the project doesn't yet ship.

Exports
-------
Source          frozen dataclass
SOURCES         canonical tuple of all sources
SOURCES_BY_ID   validated id→Source mapping
get_source      KeyError on miss
get_sources_by_band / get_sources_by_tier
seed_sources    upsert all rows into the sources table
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb

from musahit.common.types import Band, Fragility, SourceKind, Tier

# ── URL placeholder note — set on every unverified RSS/HTML source ─────────
_PENDING = "URL pending operator verification"


# ── Core dataclass ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Source:
    """Immutable descriptor for one configured source.

    Fields
    ------
    id                  Slug used throughout the system (ASCII, lower, underscores).
    display_name        Human-readable label.
    band                Ideological band — drives promotion ceiling rules (ADR-005).
    tier                Content tier — determines ingest strategy.
    kind                Technical fetch method.
    url                 Primary URL — feed endpoint, scrape root, or API base.
    rate_limit_seconds  Minimum seconds between consecutive fetches to the same host.
    fragility           Expected maintenance burden.
    notes               Operator annotations — scraping quirks, URL status, etc.
    """

    id: str
    display_name: str
    band: Band
    tier: Tier
    kind: SourceKind
    url: str
    rate_limit_seconds: int = 5
    fragility: Fragility = Fragility.ROBUST
    notes: str = ""


# ── Registry validator ──────────────────────────────────────────────────────


def _build_sources_index(sources: tuple[Source, ...]) -> dict[str, Source]:
    """Build id→Source mapping with integrity checks at import time."""
    index: dict[str, Source] = {}
    for source in sources:
        if source.id in index:
            raise ValueError(f"Duplicate source ID: {source.id!r}")
        if not source.id or not source.id.replace("_", "").isalnum():
            raise ValueError(
                f"Invalid source ID (lowercase alphanum + underscores only): {source.id!r}"
            )
        if not source.url:
            raise ValueError(f"Empty URL for source: {source.id!r}")
        if source.rate_limit_seconds <= 0:
            raise ValueError(f"Non-positive rate_limit_seconds for: {source.id!r}")
        index[source.id] = source
    return index


# ── Source definitions ──────────────────────────────────────────────────────
#
# Sections follow ADR-003 table order: NEWS · MARKETS · GOV · SOCIAL.
#
# ★ VERIFIED   — WebFetch returned valid RSS/Atom XML at scaffold time.
# ⌛ PENDING    — URL not confirmed; _PENDING note is set in notes field.
# ○ HTML       — HTML-kind source; root domain URL; exempt from feed check.

# ─── NEWS ····················································· 24 sources ───

_NEWS: tuple[Source, ...] = (
    # ── GOV-ALIGNED ─────────────────────────────────────────────────────────
    Source(  # ⌛ PENDING
        id="anadolu",
        display_name="Anadolu Ajansı",
        band=Band.GOV_ALIGNED,
        tier=Tier.NEWS,
        kind=SourceKind.RSS,
        url="https://www.aa.com.tr/tr/rss/default?cat=guncel",
        rate_limit_seconds=10,
        fragility=Fragility.MEDIUM,
        notes=f"{_PENDING} · ECONNREFUSED at scaffold time; confirm before first run",
    ),
    Source(  # ★ VERIFIED
        id="trt_haber",
        display_name="TRT Haber",
        band=Band.GOV_ALIGNED,
        tier=Tier.NEWS,
        kind=SourceKind.RSS,
        url="https://www.trthaber.com/sondakika.rss",
        rate_limit_seconds=10,
    ),
    Source(  # ★ VERIFIED
        id="sabah",
        display_name="Sabah",
        band=Band.GOV_ALIGNED,
        tier=Tier.NEWS,
        kind=SourceKind.RSS,
        url="https://www.sabah.com.tr/rss/gundem.xml",
    ),
    Source(  # ★ VERIFIED
        id="yeni_safak",
        display_name="Yeni Şafak",
        band=Band.GOV_ALIGNED,
        tier=Tier.NEWS,
        kind=SourceKind.RSS,
        url="https://www.yenisafak.com/rss",
    ),
    Source(  # ★ VERIFIED
        id="a_haber",
        display_name="A Haber",
        band=Band.GOV_ALIGNED,
        tier=Tier.NEWS,
        kind=SourceKind.RSS,
        url="https://www.ahaber.com.tr/rss/anasayfa.xml",
    ),
    # ── CENTRIST ────────────────────────────────────────────────────────────
    Source(  # ★ VERIFIED
        id="hurriyet",
        display_name="Hürriyet",
        band=Band.CENTRIST,
        tier=Tier.NEWS,
        kind=SourceKind.RSS,
        url="https://www.hurriyet.com.tr/rss/anasayfa",
    ),
    Source(  # ★ VERIFIED
        id="milliyet",
        display_name="Milliyet",
        band=Band.CENTRIST,
        tier=Tier.NEWS,
        kind=SourceKind.RSS,
        url="https://www.milliyet.com.tr/rss/rssNew/gundemRss.xml",
    ),
    Source(  # ★ VERIFIED — returns Atom format
        id="ntv",
        display_name="NTV",
        band=Band.CENTRIST,
        tier=Tier.NEWS,
        kind=SourceKind.RSS,
        url="https://www.ntv.com.tr/son-dakika.rss",
    ),
    # ── OPPOSITION ──────────────────────────────────────────────────────────
    Source(  # ★ VERIFIED
        id="cumhuriyet",
        display_name="Cumhuriyet",
        band=Band.OPPOSITION,
        tier=Tier.NEWS,
        kind=SourceKind.RSS,
        url="https://www.cumhuriyet.com.tr/rss/son_dakika.xml",
    ),
    Source(  # ★ VERIFIED — category feed /rss/gundem/; main /feed/ returns HTML index
        id="sozcu",
        display_name="Sözcü",
        band=Band.OPPOSITION,
        tier=Tier.NEWS,
        kind=SourceKind.RSS,
        url="https://www.sozcu.com.tr/rss/gundem/",
    ),
    Source(  # ★ VERIFIED — /rss/home returns feed; /rss and /rss/feed return HTML index
        id="birgun",
        display_name="BirGün",
        band=Band.OPPOSITION,
        tier=Tier.NEWS,
        kind=SourceKind.RSS,
        url="https://www.birgun.net/rss/home",
    ),
    Source(  # ★ VERIFIED
        id="halk_tv",
        display_name="Halk TV",
        band=Band.OPPOSITION,
        tier=Tier.NEWS,
        kind=SourceKind.RSS,
        url="https://halktv.com.tr/rss",
    ),
    # ── INDEPENDENT ─────────────────────────────────────────────────────────
    Source(  # ⌛ PENDING
        id="t24",
        display_name="T24",
        band=Band.INDEPENDENT,
        tier=Tier.NEWS,
        kind=SourceKind.RSS,
        url="https://t24.com.tr/rss",
        fragility=Fragility.MEDIUM,
        notes=f"{_PENDING} · HTTP 403 at scaffold time",
    ),
    Source(  # ★ VERIFIED
        id="diken",
        display_name="Diken",
        band=Band.INDEPENDENT,
        tier=Tier.NEWS,
        kind=SourceKind.RSS,
        url="https://www.diken.com.tr/feed/",
    ),
    Source(  # ★ VERIFIED
        id="duvar",
        display_name="Gazete Duvar",
        band=Band.INDEPENDENT,
        tier=Tier.NEWS,
        kind=SourceKind.RSS,
        url="https://www.gazeteduvar.com.tr/feed",
    ),
    Source(  # ★ VERIFIED
        id="bianet",
        display_name="Bianet",
        band=Band.INDEPENDENT,
        tier=Tier.NEWS,
        kind=SourceKind.RSS,
        url="https://bianet.org/bianet.rss",
    ),
    Source(  # ⌛ PENDING
        id="medyascope",
        display_name="Medyascope",
        band=Band.INDEPENDENT,
        tier=Tier.NEWS,
        kind=SourceKind.RSS,
        url="https://medyascope.tv/feed/",
        fragility=Fragility.MEDIUM,
        notes=f"{_PENDING} · HTTP 403 at scaffold time",
    ),
    # ── INTERNATIONAL ───────────────────────────────────────────────────────
    Source(  # ⌛ PENDING
        id="dw_tr",
        display_name="DW Türkçe",
        band=Band.INTERNATIONAL,
        tier=Tier.NEWS,
        kind=SourceKind.RSS,
        url="https://rss.dw.com/rdf/rss-tur-all",
        fragility=Fragility.MEDIUM,
        notes=f"{_PENDING} · rss.dw.com blocked at scaffold time; confirm URL",
    ),
    Source(  # ★ VERIFIED
        id="bbc_tr",
        display_name="BBC Türkçe",
        band=Band.INTERNATIONAL,
        tier=Tier.NEWS,
        kind=SourceKind.RSS,
        url="https://feeds.bbci.co.uk/turkce/rss.xml",
    ),
    Source(  # ⌛ PENDING — FRAGILE per ADR-013: domain migrated from amerikaninsesi.com
        id="voa_tr",
        display_name="VOA Türkçe",
        band=Band.INTERNATIONAL,
        tier=Tier.NEWS,
        kind=SourceKind.RSS,
        url="https://www.voaturkce.com/api/zkreoo-m_tp$e",
        fragility=Fragility.FRAGILE,
        notes=(
            f"{_PENDING} · HTTP 403 at scaffold time · "
            "domain migrated from amerikaninsesi.com to voaturkce.com in 2025; "
            "DNS redirect in place; API URL pattern may change without notice"
        ),
    ),
    Source(  # ★ VERIFIED
        id="euronews_tr",
        display_name="Euronews Türkçe",
        band=Band.INTERNATIONAL,
        tier=Tier.NEWS,
        kind=SourceKind.RSS,
        url="https://tr.euronews.com/rss",
    ),
    Source(  # ⌛ PENDING — global world-news feed; normalize stage filters for Turkey
        id="reuters_tr",
        display_name="Reuters Turkey",
        band=Band.INTERNATIONAL,
        tier=Tier.NEWS,
        kind=SourceKind.RSS,
        url="https://feeds.reuters.com/reuters/worldNews",
        fragility=Fragility.MEDIUM,
        notes=(
            f"{_PENDING} · reuters.com blocked at scaffold time · "
            "global Reuters world-news feed; ingest normalizer filters for Turkey-topic articles"
        ),
    ),
    Source(  # ○ HTML (per ADR-003) — HTML scrape of AP Turkey hub
        id="ap_tr",
        display_name="AP Turkey",
        band=Band.INTERNATIONAL,
        tier=Tier.NEWS,
        kind=SourceKind.HTML,
        url="https://apnews.com/hub/turkey",
        fragility=Fragility.MEDIUM,
        notes="HTML scrape; ap_tr.py fetches listing page and filters Turkey-topic articles",
    ),
    Source(  # ★ VERIFIED — band=CENTRIST per ADR-013 (Demirören Media Group ownership)
        id="bloomberg_ht",
        display_name="Bloomberg HT",
        band=Band.CENTRIST,
        tier=Tier.NEWS,
        kind=SourceKind.RSS,
        url="https://bloomberght.com/rss",
        notes="Band changed from INTERNATIONAL to CENTRIST per ADR-013 · Demirören ownership",
    ),
)

# ─── MARKETS ··················································· 6 sources ───

_MARKETS: tuple[Source, ...] = (
    Source(  # ○ HTML — TCMB press releases; no public RSS
        id="tcmb",
        display_name="Türkiye Cumhuriyet Merkez Bankası",
        band=Band.PRIMARY_MARKET,
        tier=Tier.MARKETS,
        kind=SourceKind.HTML,
        url="https://www.tcmb.gov.tr/",
        rate_limit_seconds=15,
        fragility=Fragility.MEDIUM,
        notes="HTML scrape; tcmb.py targets the press-release listing page",
    ),
    Source(  # ⌛ PENDING — RSS endpoint not confirmed at scaffold time
        id="kap",
        display_name="Kamuyu Aydınlatma Platformu",
        band=Band.PRIMARY_MARKET,
        tier=Tier.MARKETS,
        kind=SourceKind.RSS,
        url="https://www.kap.org.tr/",
        rate_limit_seconds=10,
        fragility=Fragility.MEDIUM,
        notes=(
            f"{_PENDING} · RSS feed paths at kap.org.tr returned 404 at scaffold time; "
            "locate disclosure RSS endpoint before first ingest run"
        ),
    ),
    Source(  # ○ HTML — BIST market data; no public RSS
        id="bist",
        display_name="Borsa İstanbul",
        band=Band.PRIMARY_MARKET,
        tier=Tier.MARKETS,
        kind=SourceKind.HTML,
        url="https://www.borsaistanbul.com/",
        rate_limit_seconds=10,
        fragility=Fragility.MEDIUM,
        notes="HTML scrape; bist.py targets market-announcements section",
    ),
    Source(  # ○ HTML — TÜİK statistics; no public RSS
        id="tuik",
        display_name="Türkiye İstatistik Kurumu",
        band=Band.PRIMARY_MARKET,
        tier=Tier.MARKETS,
        kind=SourceKind.HTML,
        url="https://www.tuik.gov.tr/",
        rate_limit_seconds=15,
        fragility=Fragility.MEDIUM,
        notes="HTML scrape; tuik.py targets the press-release calendar page",
    ),
    Source(  # ★ VERIFIED
        id="dunya",
        display_name="Dünya Gazetesi",
        band=Band.CENTRIST,
        tier=Tier.MARKETS,
        kind=SourceKind.RSS,
        url="https://www.dunya.com/rss/",
    ),
    Source(  # ★ VERIFIED — Blogger Atom feed
        id="mahfi",
        display_name="Mahfi Eğilmez blog",
        band=Band.INDEPENDENT,
        tier=Tier.MARKETS,
        kind=SourceKind.RSS,
        url="https://mahfiegilmez.com/feeds/posts/default",
        rate_limit_seconds=30,
        notes="Blogger Atom feed; low post frequency; 30 s rate limit is generous",
    ),
)

# ─── GOV ······················································· 5 sources ───
#
# danistay was dropped 2026-05-25 · architecturally unreachable.
# The Danıştay press-release listing renders entirely in JavaScript:
# the HTML body returned to a non-JS client (httpx OR curl_cffi alike)
# contains the navigation chrome but no article links · selectolax then
# extracts zero URLs and the ingester would report OK with count=0 every
# night. Fixing this would require Playwright (or another headless-
# browser engine) to evaluate the page's JS before extraction · that is
# a strictly larger architectural change than the curl_cffi adoption
# the rest of the gov sources need. Re-add the entry when JS-rendering
# becomes a project dependency.
#
# The curl_cffi adoption itself (the 2026-05-25 spike) confirmed that
# anayasa_mahkemesi, cumhurbaskanligi (tccb.gov.tr), and yargitay are
# reachable with firefox133 impersonation + session bootstrap + Referer;
# those four (plus resmi_gazete and tbmm) remain in this tuple and route
# through ``musahit.ingest.gov_http``.

_GOV: tuple[Source, ...] = (
    Source(  # ★ VERIFIED — listing page; scraper constructs daily PDF URL
        id="resmi_gazete",
        display_name="T.C. Resmî Gazete",
        band=Band.PRIMARY_GOV,
        tier=Tier.GOV,
        kind=SourceKind.PDF,
        url="https://www.resmigazete.gov.tr/",
        rate_limit_seconds=15,
        fragility=Fragility.MEDIUM,
        notes=(
            "HTML listing page; resmi_gazete.py constructs the daily PDF URL "
            "in the form /eskiler/YYYY/MM/YYYYMMDD.pdf and fetches it with pdfplumber"
        ),
    ),
    Source(  # ○ HTML — TBMM daily agenda
        id="tbmm",
        display_name="TBMM gündem",
        band=Band.PRIMARY_GOV,
        tier=Tier.GOV,
        kind=SourceKind.HTML,
        url="https://www.tbmm.gov.tr/",
        rate_limit_seconds=15,
        fragility=Fragility.MEDIUM,
        notes="HTML scrape; tbmm.py targets the session-agenda listing page",
    ),
    Source(  # ★ VERIFIED — root page accessible
        id="cumhurbaskanligi",
        display_name="Cumhurbaşkanlığı kararnameleri",
        band=Band.PRIMARY_GOV,
        tier=Tier.GOV,
        kind=SourceKind.HTML,
        url="https://www.tccb.gov.tr/",
        rate_limit_seconds=15,
        fragility=Fragility.MEDIUM,
        notes="HTML scrape; cumhurbaskanligi.py targets the presidential-decree section",
    ),
    Source(  # ○ HTML — Constitutional Court decisions
        id="anayasa_mahkemesi",
        display_name="Anayasa Mahkemesi",
        band=Band.PRIMARY_JUDICIAL,
        tier=Tier.GOV,
        kind=SourceKind.HTML,
        url="https://www.anayasa.gov.tr/",
        rate_limit_seconds=15,
        fragility=Fragility.MEDIUM,
        notes="HTML scrape; aym.py targets the decisions listing page",
    ),
    Source(  # ○ HTML — Court of Cassation
        id="yargitay",
        display_name="Yargıtay",
        band=Band.PRIMARY_JUDICIAL,
        tier=Tier.GOV,
        kind=SourceKind.HTML,
        url="https://www.yargitay.gov.tr/",
        rate_limit_seconds=15,
        fragility=Fragility.MEDIUM,
        notes="HTML scrape; yargitay.py targets press-release section",
    ),
    # danistay (Council of State · Danıştay) was here · dropped 2026-05-25
    # · architecturally unreachable without a JS-rendering back-end. See
    # the comment block above _GOV for the full rationale.
)

# ─── SOCIAL ···················································· 1 source ───
# x_stub is NOT created — see ADR-013 Amendment 2.
# Band.SOCIAL_X is reserved in musahit.common.types.Band for future use.

_SOCIAL: tuple[Source, ...] = (
    Source(
        id="reddit_turkey",
        display_name="r/Turkey · r/TurkeyJerky · r/AskTurkey",
        band=Band.SOCIAL_REDDIT,
        tier=Tier.SOCIAL,
        kind=SourceKind.API,
        url="https://www.reddit.com/",
        rate_limit_seconds=2,
        notes=(
            "PRAW-based ingestion; subreddits: Turkey, TurkeyJerky, AskTurkey, "
            "europe (Turkey-topic posts filtered by flair/title); "
            "credentials from .env: REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET"
        ),
    ),
)

# ── Canonical tuple and validated index ────────────────────────────────────

SOURCES: tuple[Source, ...] = _NEWS + _MARKETS + _GOV + _SOCIAL
SOURCES_BY_ID: dict[str, Source] = _build_sources_index(SOURCES)


# ── Query helpers ───────────────────────────────────────────────────────────


def get_source(source_id: str) -> Source:
    """Return the Source with the given id. Raises KeyError if not found."""
    return SOURCES_BY_ID[source_id]


def get_sources_by_band(band: Band) -> tuple[Source, ...]:
    """Return all sources with the given band, in SOURCES order."""
    return tuple(s for s in SOURCES if s.band is band)


def get_sources_by_tier(tier: Tier) -> tuple[Source, ...]:
    """Return all sources with the given tier, in SOURCES order."""
    return tuple(s for s in SOURCES if s.tier is tier)


# ── DB seeding ─────────────────────────────────────────────────────────────

_UPSERT_SQL = """
    INSERT INTO sources
        (id, display_name, band, tier, kind, url, rate_limit_seconds, fragility, notes)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (id) DO UPDATE SET
        display_name = excluded.display_name,
        band         = excluded.band,
        tier         = excluded.tier,
        kind         = excluded.kind,
        url          = excluded.url,
        rate_limit_seconds = excluded.rate_limit_seconds,
        fragility    = excluded.fragility,
        notes        = excluded.notes
"""


def seed_sources(conn: duckdb.DuckDBPyConnection) -> int:
    """Upsert all sources from SOURCES into the sources table.

    Explicit ``.value`` coercion guards against DuckDB driver version drift
    in StrEnum handling. Returns the total number of rows upserted (always
    ``len(SOURCES)`` on success).
    """
    rows = [
        (
            s.id,
            s.display_name,
            s.band.value,
            s.tier.value,
            s.kind.value,
            s.url,
            s.rate_limit_seconds,
            s.fragility.value,
            s.notes,
        )
        for s in SOURCES
    ]
    conn.executemany(_UPSERT_SQL, rows)
    return len(rows)

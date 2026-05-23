"""Curated vocabulary for the rule-based entity tagger.

Entries are grouped by ``EntityType``; each entry has a canonical display
form plus optional aliases. The tagger matches case-insensitively under
Turkish locale folding (İ/i and I/ı are paired). For multi-word names the
matcher walks word boundaries.

Why a curated vocabulary instead of spaCy / a Turkish NER model:

* MÜŞAHİT runs CPU-only on a Windows laptop. A general NER model would
  cost a noticeable fraction of the nightly budget for marginal accuracy.
* The high-signal entities for the briefing are a *small, slow-changing*
  set — major parties, the cabinet, key institutions, blue-chip
  companies. A 100-entry vocabulary covers the daily briefing's needs.
* Curation gives explicit operator control. spaCy would surface
  half-recognised names that need filtering anyway.

When the operator's needs outgrow this list — long-tail companies,
court cases, individual journalists — ADR-016 (proposed) will revisit
this decision and may switch to a hybrid (vocabulary + transformer-NER
on-demand).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EntityType(StrEnum):
    """Coarse-grained entity categories.

    Expansion is an ADR amendment per the project convention (MEMORY.md).
    """

    PARTY = "PARTY"
    INSTITUTION = "INSTITUTION"
    PERSON = "PERSON"
    COMPANY = "COMPANY"


@dataclass(frozen=True)
class VocabEntry:
    """One vocabulary item.

    Attributes:
        canonical: Display form used in the briefing (e.g. ``"AKP"``).
        type: Coarse entity category.
        aliases: Additional surface forms the matcher should recognise.
            Always include the canonical form's most common variants;
            keep aliases ASCII-folded versions of Turkish-named entities
            so feeds that drop diacritics still match.
    """

    canonical: str
    type: EntityType
    aliases: tuple[str, ...] = ()


# ── Parties ────────────────────────────────────────────────────────────────


_PARTIES: tuple[VocabEntry, ...] = (
    VocabEntry(
        "AKP",
        EntityType.PARTY,
        aliases=("Adalet ve Kalkınma Partisi", "AK Parti", "Ak Parti"),
    ),
    VocabEntry(
        "CHP",
        EntityType.PARTY,
        aliases=("Cumhuriyet Halk Partisi",),
    ),
    VocabEntry(
        "MHP",
        EntityType.PARTY,
        aliases=("Milliyetçi Hareket Partisi",),
    ),
    VocabEntry(
        "İYİ Parti",
        EntityType.PARTY,
        aliases=("IYI Parti", "İyi Parti", "Iyi Parti"),
    ),
    VocabEntry(
        "DEM Parti",
        EntityType.PARTY,
        aliases=("Halkların Eşitlik ve Demokrasi Partisi", "DEM"),
    ),
    VocabEntry(
        "HDP",
        EntityType.PARTY,
        aliases=("Halkların Demokratik Partisi",),
    ),
    VocabEntry(
        "TİP",
        EntityType.PARTY,
        aliases=("Türkiye İşçi Partisi", "TIP", "Tip"),
    ),
    VocabEntry(
        "Saadet Partisi",
        EntityType.PARTY,
        aliases=("Saadet",),
    ),
    VocabEntry(
        "Gelecek Partisi",
        EntityType.PARTY,
    ),
    VocabEntry(
        "DEVA Partisi",
        EntityType.PARTY,
        aliases=("DEVA",),
    ),
    VocabEntry(
        "Zafer Partisi",
        EntityType.PARTY,
        aliases=("Zafer",),
    ),
)


# ── Institutions ───────────────────────────────────────────────────────────


_INSTITUTIONS: tuple[VocabEntry, ...] = (
    VocabEntry("TBMM", EntityType.INSTITUTION,
               aliases=("Türkiye Büyük Millet Meclisi",)),
    VocabEntry("AYM", EntityType.INSTITUTION,
               aliases=("Anayasa Mahkemesi",)),
    VocabEntry("Yargıtay", EntityType.INSTITUTION, aliases=("Yargitay",)),
    VocabEntry("Danıştay", EntityType.INSTITUTION, aliases=("Danistay",)),
    VocabEntry("TCMB", EntityType.INSTITUTION,
               aliases=("Türkiye Cumhuriyet Merkez Bankası", "Merkez Bankası")),
    VocabEntry("BDDK", EntityType.INSTITUTION,
               aliases=("Bankacılık Düzenleme ve Denetleme Kurumu",)),
    VocabEntry("SPK", EntityType.INSTITUTION,
               aliases=("Sermaye Piyasası Kurulu",)),
    VocabEntry("TÜİK", EntityType.INSTITUTION,
               aliases=("Türkiye İstatistik Kurumu", "TUIK", "Tuik")),
    VocabEntry("YSK", EntityType.INSTITUTION,
               aliases=("Yüksek Seçim Kurulu",)),
    VocabEntry("HSK", EntityType.INSTITUTION,
               aliases=("Hâkimler ve Savcılar Kurulu",)),
    VocabEntry("RTÜK", EntityType.INSTITUTION,
               aliases=("Radyo ve Televizyon Üst Kurulu", "RTUK")),
    VocabEntry("Diyanet", EntityType.INSTITUTION,
               aliases=("Diyanet İşleri Başkanlığı",)),
    VocabEntry("MİT", EntityType.INSTITUTION,
               aliases=("Milli İstihbarat Teşkilatı", "MIT")),
    VocabEntry("Cumhurbaşkanlığı", EntityType.INSTITUTION,
               aliases=("Cumhurbaskanligi",)),
    VocabEntry("Resmî Gazete", EntityType.INSTITUTION,
               aliases=("Resmi Gazete",)),
    VocabEntry("KAP", EntityType.INSTITUTION,
               aliases=("Kamuyu Aydınlatma Platformu",)),
    VocabEntry("BIST", EntityType.INSTITUTION,
               aliases=("Borsa İstanbul", "Borsa Istanbul")),
)


# ── People (top cabinet + key politicians; refresh on cabinet change) ──────


_PEOPLE: tuple[VocabEntry, ...] = (
    VocabEntry("Recep Tayyip Erdoğan", EntityType.PERSON,
               aliases=("Cumhurbaşkanı Erdoğan", "Erdoğan", "Erdogan")),
    VocabEntry("Cevdet Yılmaz", EntityType.PERSON, aliases=("Cevdet Yilmaz",)),
    VocabEntry("Mehmet Şimşek", EntityType.PERSON, aliases=("Mehmet Simsek", "Şimşek")),
    VocabEntry("Hakan Fidan", EntityType.PERSON),
    VocabEntry("Yaşar Güler", EntityType.PERSON, aliases=("Yasar Guler",)),
    VocabEntry("Ali Yerlikaya", EntityType.PERSON),
    VocabEntry("Yılmaz Tunç", EntityType.PERSON, aliases=("Yilmaz Tunc",)),
    VocabEntry("Özgür Özel", EntityType.PERSON,
               aliases=("Ozgur Ozel", "CHP Genel Başkanı")),
    VocabEntry("Ekrem İmamoğlu", EntityType.PERSON,
               aliases=("Ekrem Imamoglu", "İmamoğlu")),
    VocabEntry("Mansur Yavaş", EntityType.PERSON, aliases=("Mansur Yavas",)),
    VocabEntry("Devlet Bahçeli", EntityType.PERSON, aliases=("Devlet Bahceli",)),
    VocabEntry("Meral Akşener", EntityType.PERSON, aliases=("Meral Aksener",)),
    VocabEntry("Tuncer Karamollaoğlu", EntityType.PERSON),
    VocabEntry("Fatih Karahan", EntityType.PERSON,
               aliases=("Merkez Bankası Başkanı",)),
)


# ── Companies (blue-chip Turkish) ──────────────────────────────────────────


_COMPANIES: tuple[VocabEntry, ...] = (
    VocabEntry("Türk Hava Yolları", EntityType.COMPANY,
               aliases=("THY", "Turk Hava Yollari", "Turkish Airlines")),
    VocabEntry("Türk Telekom", EntityType.COMPANY, aliases=("Turk Telekom",)),
    VocabEntry("Ziraat Bankası", EntityType.COMPANY, aliases=("Ziraat",)),
    VocabEntry("Halkbank", EntityType.COMPANY,
               aliases=("Türkiye Halk Bankası",)),
    VocabEntry("Vakıfbank", EntityType.COMPANY, aliases=("Vakifbank",)),
    VocabEntry("İş Bankası", EntityType.COMPANY,
               aliases=("Is Bankasi", "Türkiye İş Bankası")),
    VocabEntry("Garanti BBVA", EntityType.COMPANY, aliases=("Garanti Bankası",)),
    VocabEntry("Akbank", EntityType.COMPANY),
    VocabEntry("Yapı Kredi", EntityType.COMPANY, aliases=("Yapi Kredi",)),
    VocabEntry("Koç Holding", EntityType.COMPANY,
               aliases=("Koc Holding", "Koç")),
    VocabEntry("Sabancı Holding", EntityType.COMPANY,
               aliases=("Sabanci Holding", "Sabancı")),
    VocabEntry("TÜPRAŞ", EntityType.COMPANY, aliases=("TUPRAS", "Tüpraş")),
    VocabEntry("BOTAŞ", EntityType.COMPANY, aliases=("BOTAS",)),
    VocabEntry("TPAO", EntityType.COMPANY,
               aliases=("Türkiye Petrolleri Anonim Ortaklığı",)),
    VocabEntry("Anadolu Efes", EntityType.COMPANY, aliases=("Efes",)),
)


VOCABULARY: tuple[VocabEntry, ...] = _PARTIES + _INSTITUTIONS + _PEOPLE + _COMPANIES


__all__ = ["VOCABULARY", "EntityType", "VocabEntry"]

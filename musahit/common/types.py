"""Shared domain enumerations used across all pipeline stages.

DEFCON is intentionally absent — it lives in musahit/score/defcon.py (FILE-PROTECTED).
"""

from __future__ import annotations

from enum import StrEnum


class Band(StrEnum):
    """Ideological band of a news source (ADR-003)."""

    GOV_ALIGNED = "gov_aligned"
    CENTRIST = "centrist"
    OPPOSITION = "opposition"
    INDEPENDENT = "independent"
    INTERNATIONAL = "international"
    SOCIAL_X = "social_x"
    SOCIAL_REDDIT = "social_reddit"
    PRIMARY_GOV = "primary_gov"
    PRIMARY_MARKET = "primary_market"
    PRIMARY_JUDICIAL = "primary_judicial"


class Tier(StrEnum):
    """Content tier, determining ingestion strategy (ADR-003)."""

    NEWS = "news"
    MARKETS = "markets"
    GOV = "gov"
    SOCIAL = "social"


class SourceKind(StrEnum):
    """Technical fetch method for a source (ADR-003)."""

    RSS = "RSS"
    HTML = "HTML"
    PDF = "PDF"
    API = "API"
    DEFERRED = "DEFERRED"


class Fragility(StrEnum):
    """Expected maintenance burden of a source (ADR-003)."""

    ROBUST = "ROBUST"
    MEDIUM = "MEDIUM"
    FRAGILE = "FRAGILE"


class IngestStatus(StrEnum):
    """Per-source ingest outcome recorded in ingest_log (ADR-006)."""

    OK = "OK"
    TIMEOUT = "TIMEOUT"
    HTTP_ERROR = "HTTP_ERROR"
    PARSE_ERROR = "PARSE_ERROR"
    SKIPPED = "SKIPPED"


class ArcState(StrEnum):
    """Story arc lifecycle state (ADR-008)."""

    OPEN = "OPEN"
    WATCH = "WATCH"
    RESOLVED = "RESOLVED"


class Confidence(StrEnum):
    """Cross-band corroboration confidence tag (ADR-005).

    Python identifiers are ASCII; string values use proper Turkish.
    """

    YUKSEK = "YÜKSEK"
    ORTA = "ORTA"
    DUSUK = "DÜŞÜK"


class Category(StrEnum):
    """Thematic category of a cluster or arc (ADR-009)."""

    POLITIKA = "POLİTİKA"
    EKONOMI = "EKONOMİ"
    YARGI = "YARGI"
    GUVENLIK = "GÜVENLİK"
    DIPLOMASI = "DİPLOMASİ"
    MEVZUAT = "MEVZUAT"
    TOPLUM = "TOPLUM"
    UNCLASSIFIED = "SINIFLANDIRILMADI"


class OverrideAction(StrEnum):
    """Operator actions recorded in manual_overrides (ADR-008, ADR-011)."""

    PROMOTE = "PROMOTE"
    DEMOTE = "DEMOTE"
    RESOLVE = "RESOLVE"
    REOPEN = "REOPEN"
    MERGE = "MERGE"
    SPLIT = "SPLIT"
    DISMISS = "DISMISS"
    RENAME = "RENAME"


class OverrideTarget(StrEnum):
    """Object type targeted by a manual override."""

    CLUSTER = "CLUSTER"
    ARC = "ARC"
    BRIEFING = "BRIEFING"


class PipelineStatus(StrEnum):
    """Top-level pipeline run status stored in pipeline_runs (ADR-006)."""

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


# Convenience sets used in promotion rules (ADR-005)
PRIMARY_BANDS: frozenset[Band] = frozenset(
    {Band.PRIMARY_GOV, Band.PRIMARY_MARKET, Band.PRIMARY_JUDICIAL}
)

SOCIAL_BANDS: frozenset[Band] = frozenset({Band.SOCIAL_X, Band.SOCIAL_REDDIT})

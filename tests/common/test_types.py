"""Tests for musahit.common.types enumerations."""

from __future__ import annotations

from musahit.common.types import (
    PRIMARY_BANDS,
    SOCIAL_BANDS,
    ArcState,
    Band,
    Category,
    Confidence,
    Fragility,
    IngestStatus,
    OverrideAction,
    OverrideTarget,
    PipelineStatus,
    SourceKind,
    Tier,
)


class TestBand:
    def test_all_nine_bands_exist(self) -> None:
        assert len(Band) == 10

    def test_primary_bands_are_strings(self) -> None:
        assert Band.PRIMARY_GOV == "primary_gov"
        assert Band.PRIMARY_MARKET == "primary_market"
        assert Band.PRIMARY_JUDICIAL == "primary_judicial"

    def test_social_bands(self) -> None:
        assert Band.SOCIAL_X == "social_x"
        assert Band.SOCIAL_REDDIT == "social_reddit"

    def test_editorial_bands(self) -> None:
        assert Band.GOV_ALIGNED == "gov_aligned"
        assert Band.OPPOSITION == "opposition"
        assert Band.CENTRIST == "centrist"
        assert Band.INDEPENDENT == "independent"
        assert Band.INTERNATIONAL == "international"

    def test_band_is_str(self) -> None:
        assert isinstance(Band.GOV_ALIGNED, str)

    def test_primary_bands_frozenset(self) -> None:
        expected = frozenset({Band.PRIMARY_GOV, Band.PRIMARY_MARKET, Band.PRIMARY_JUDICIAL})
        assert expected == PRIMARY_BANDS

    def test_social_bands_frozenset(self) -> None:
        expected = frozenset({Band.SOCIAL_X, Band.SOCIAL_REDDIT})
        assert expected == SOCIAL_BANDS

    def test_primary_and_social_disjoint(self) -> None:
        assert PRIMARY_BANDS.isdisjoint(SOCIAL_BANDS)


class TestTier:
    def test_four_tiers(self) -> None:
        assert {Tier.NEWS, Tier.MARKETS, Tier.GOV, Tier.SOCIAL} == set(Tier)

    def test_values(self) -> None:
        assert Tier.NEWS == "news"
        assert Tier.SOCIAL == "social"


class TestSourceKind:
    def test_five_kinds(self) -> None:
        assert len(SourceKind) == 5

    def test_deferred_exists(self) -> None:
        assert SourceKind.DEFERRED == "DEFERRED"


class TestFragility:
    def test_three_levels(self) -> None:
        assert len(Fragility) == 3

    def test_values(self) -> None:
        assert Fragility.ROBUST == "ROBUST"
        assert Fragility.MEDIUM == "MEDIUM"
        assert Fragility.FRAGILE == "FRAGILE"


class TestIngestStatus:
    def test_five_statuses(self) -> None:
        assert len(IngestStatus) == 5

    def test_ok_and_error_variants(self) -> None:
        assert IngestStatus.OK == "OK"
        assert IngestStatus.TIMEOUT == "TIMEOUT"
        assert IngestStatus.HTTP_ERROR == "HTTP_ERROR"
        assert IngestStatus.PARSE_ERROR == "PARSE_ERROR"
        assert IngestStatus.SKIPPED == "SKIPPED"


class TestArcState:
    def test_three_states(self) -> None:
        assert len(ArcState) == 3

    def test_state_progression(self) -> None:
        # OPEN → WATCH → RESOLVED per ADR-008
        assert ArcState.OPEN == "OPEN"
        assert ArcState.WATCH == "WATCH"
        assert ArcState.RESOLVED == "RESOLVED"


class TestConfidence:
    def test_three_levels(self) -> None:
        assert len(Confidence) == 3

    def test_turkish_string_values(self) -> None:
        assert Confidence.YUKSEK == "YÜKSEK"
        assert Confidence.ORTA == "ORTA"
        assert Confidence.DUSUK == "DÜŞÜK"

    def test_identifiers_are_ascii(self) -> None:
        # Identifiers must stay ASCII for cross-platform compatibility.
        for member in Confidence:
            assert member.name.isascii(), f"{member.name!r} is not ASCII"


class TestCategory:
    def test_eight_categories_including_unclassified(self) -> None:
        assert len(Category) == 8

    def test_turkish_values(self) -> None:
        assert Category.POLITIKA == "POLİTİKA"
        assert Category.EKONOMI == "EKONOMİ"
        assert Category.UNCLASSIFIED == "SINIFLANDIRILMADI"

    def test_identifiers_are_ascii(self) -> None:
        for member in Category:
            assert member.name.isascii(), f"{member.name!r} is not ASCII"


class TestOverrideAction:
    def test_eight_actions(self) -> None:
        assert len(OverrideAction) == 8

    def test_arc_actions_exist(self) -> None:
        assert OverrideAction.RESOLVE == "RESOLVE"
        assert OverrideAction.MERGE == "MERGE"
        assert OverrideAction.SPLIT == "SPLIT"


class TestOverrideTarget:
    def test_three_targets(self) -> None:
        assert len(OverrideTarget) == 3


class TestPipelineStatus:
    def test_three_statuses(self) -> None:
        assert len(PipelineStatus) == 3

    def test_terminal_statuses(self) -> None:
        assert PipelineStatus.COMPLETED == "COMPLETED"
        assert PipelineStatus.FAILED == "FAILED"

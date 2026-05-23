"""Tests for musahit.score.defcon — FILE-PROTECTED constants from ADR-004."""

from __future__ import annotations

from musahit.score.defcon import (
    DEFCON,
    DEFCON_ANCHORS,
    DEFCON_LABEL_TR,
    DEFCON_REQUIRES_OVERRIDE,
)


class TestEnumValues:
    def test_six_levels_with_correct_integer_values(self) -> None:
        assert int(DEFCON.UNTHINKABLE) == 0
        assert int(DEFCON.ACUTE) == 1
        assert int(DEFCON.SEVERE) == 2
        assert int(DEFCON.MATERIAL) == 3
        assert int(DEFCON.ROUTINE) == 4
        assert int(DEFCON.AMBIENT) == 5

    def test_ordering_is_severity_descending(self) -> None:
        # Lower number = more severe — ADR-004 ladder direction.
        assert DEFCON.UNTHINKABLE < DEFCON.ACUTE < DEFCON.SEVERE
        assert DEFCON.SEVERE < DEFCON.MATERIAL < DEFCON.ROUTINE < DEFCON.AMBIENT

    def test_no_extra_levels(self) -> None:
        # Pin the closed set — enum expansion is an ADR amendment.
        assert {int(d) for d in DEFCON} == {0, 1, 2, 3, 4, 5}


class TestTurkishLabels:
    def test_every_level_has_label(self) -> None:
        for level in DEFCON:
            assert level in DEFCON_LABEL_TR
            assert DEFCON_LABEL_TR[level]

    def test_exact_strings_match_adr_004(self) -> None:
        assert DEFCON_LABEL_TR[DEFCON.UNTHINKABLE] == "DÜŞÜNÜLEMEZ"
        assert DEFCON_LABEL_TR[DEFCON.ACUTE] == "AKUT"
        assert DEFCON_LABEL_TR[DEFCON.SEVERE] == "ŞİDDETLİ"
        assert DEFCON_LABEL_TR[DEFCON.MATERIAL] == "MATERYAL"
        assert DEFCON_LABEL_TR[DEFCON.ROUTINE] == "GÜNDEM"
        assert DEFCON_LABEL_TR[DEFCON.AMBIENT] == "AMBİYANS"


class TestRequiresOverride:
    def test_only_unthinkable_in_set(self) -> None:
        assert frozenset({DEFCON.UNTHINKABLE}) == DEFCON_REQUIRES_OVERRIDE

    def test_is_frozenset(self) -> None:
        assert isinstance(DEFCON_REQUIRES_OVERRIDE, frozenset)


class TestAnchors:
    def test_every_level_has_at_least_one_anchor(self) -> None:
        for level in DEFCON:
            assert level in DEFCON_ANCHORS
            assert len(DEFCON_ANCHORS[level]) >= 1

    def test_unthinkable_anchors_match_adr_004_theme(self) -> None:
        anchors = " ".join(DEFCON_ANCHORS[DEFCON.UNTHINKABLE])
        assert "Anayasal" in anchors  # constitutional rupture is the headline anchor
        assert "Akkuyu" in anchors

    def test_acute_anchors_include_known_examples(self) -> None:
        anchors = " ".join(DEFCON_ANCHORS[DEFCON.ACUTE])
        assert "15 Temmuz" in anchors
        assert "12 Eylül" in anchors

    def test_routine_anchors_describe_daily_news(self) -> None:
        anchors = " ".join(DEFCON_ANCHORS[DEFCON.ROUTINE])
        assert "enflasyon" in anchors.lower() or "kabine" in anchors.lower()

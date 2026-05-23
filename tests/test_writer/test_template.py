"""Tests for musahit.writer.template — the canonical section list."""

from __future__ import annotations

from musahit.writer.template import (
    DEFCON_3_CATEGORY_SUBSECTIONS,
    DOCUMENT_TITLE,
    TEMPLATE_SECTIONS,
)


class TestTemplateSections:
    def test_eight_sections_in_canonical_order(self) -> None:
        markers = [s.marker for s in TEMPLATE_SECTIONS]
        assert markers == [
            "## ❯ DEFCON 1-2 · ÖNCELİKLİ",
            "## ❯ DEFCON 3 · MATERYAL",
            "## ❯ AÇIK GELİŞMELER · DEVAM EDEN TAKİP",
            "## ❯ DEFCON 4 · GÜNDEM",
            "## ❯ DİKKAT · YALNIZCA SOSYALDE",
            "## ❯ AMBİYANS · DEFCON 5",
            "## ❯ KAPATILAN HİKAYELER",
            "## ❯ SİSTEM LOG",
        ]

    def test_every_section_uses_the_arrow_marker(self) -> None:
        for s in TEMPLATE_SECTIONS:
            assert s.marker.startswith("## ❯ ")

    def test_names_are_unique(self) -> None:
        names = [s.name for s in TEMPLATE_SECTIONS]
        assert len(names) == len(set(names))

    def test_document_title_is_briefing_header(self) -> None:
        assert DOCUMENT_TITLE == "# MÜŞAHİT · GÜNLÜK BRİF"


class TestCategorySubsections:
    def test_seven_categories_per_adr_009(self) -> None:
        assert DEFCON_3_CATEGORY_SUBSECTIONS == (
            "### POLİTİKA",
            "### EKONOMİ",
            "### YARGI",
            "### GÜVENLİK",
            "### DİPLOMASİ",
            "### MEVZUAT",
            "### TOPLUM",
        )

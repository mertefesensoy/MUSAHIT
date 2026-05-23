"""Tests for musahit.ingest.gazette_parsing — pure parser.

The parser is a pure function over (page_number, text) tuples plus a
``publication_date`` argument; this suite drives every section/item
edge case with synthetic strings so no PDF is required. One smoke test
at the bottom proves the PDF wrapper :func:`parse_gazette_pdf` works
against the hand-crafted fixture in ``tests/fixtures/resmi_gazete/``.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from musahit.ingest.gazette_parsing import (
    GazetteItemType,
    GazetteSection,
    parse_gazette_pages,
    parse_gazette_pdf,
)

PUB_DATE = date(2026, 5, 23)
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "resmi_gazete"


# ── Section detection ──────────────────────────────────────────────────────


class TestSectionDetection:
    def test_executive_section_recognized(self) -> None:
        text = "YÜRÜTME VE İDARE BÖLÜMÜ\n\nKANUN\n\nKanun No: 7460\nText"
        items = parse_gazette_pages([(1, text)], PUB_DATE)
        assert len(items) == 1
        assert items[0].section is GazetteSection.EXECUTIVE

    def test_judicial_section_recognized(self) -> None:
        text = "YARGI BÖLÜMÜ\n\nMAHKEME KARARI\n\nKarar No: 2026/1\nText"
        items = parse_gazette_pages([(1, text)], PUB_DATE)
        assert len(items) == 1
        assert items[0].section is GazetteSection.JUDICIAL

    def test_announcement_section_recognized(self) -> None:
        text = "İLAN BÖLÜMÜ\n\nTEBLİĞ\n\nTebliğ No: 2026/3\nText"
        items = parse_gazette_pages([(1, text)], PUB_DATE)
        assert len(items) == 1
        assert items[0].section is GazetteSection.ANNOUNCEMENT

    def test_section_switch_within_same_pdf(self) -> None:
        text = (
            "YÜRÜTME VE İDARE BÖLÜMÜ\n\nKANUN\n\nKanun No: 1\nA\n\n"
            "YARGI BÖLÜMÜ\n\nMAHKEME KARARI\n\nKarar No: 2026/2\nB"
        )
        items = parse_gazette_pages([(1, text)], PUB_DATE)
        assert [i.section for i in items] == [
            GazetteSection.EXECUTIVE,
            GazetteSection.JUDICIAL,
        ]


# ── Item type detection ────────────────────────────────────────────────────


class TestItemTypeDetection:
    @pytest.mark.parametrize(
        ("marker", "expected_type"),
        [
            ("KANUN", GazetteItemType.LAW),
            ("CUMHURBAŞKANLIĞI KARARNAMESİ", GazetteItemType.PRESIDENTIAL_DECREE),
            ("YÖNETMELİK", GazetteItemType.REGULATION),
            ("TEBLİĞ", GazetteItemType.COMMUNIQUE),
            ("ATAMA KARARI", GazetteItemType.APPOINTMENT),
            ("MAHKEME KARARI", GazetteItemType.COURT_DECISION),
        ],
    )
    def test_each_known_type_marker_recognized(
        self, marker: str, expected_type: GazetteItemType
    ) -> None:
        text = f"YÜRÜTME VE İDARE BÖLÜMÜ\n\n{marker}\n\nKarar Sayısı: 1\nText"
        items = parse_gazette_pages([(1, text)], PUB_DATE)
        assert len(items) == 1
        assert items[0].item_type is expected_type

    def test_unknown_item_marker_does_not_create_item(self) -> None:
        # An unrecognized header is treated as ordinary content; the parser
        # does NOT fabricate an item. OTHER is reserved for explicit
        # fall-through paths (none today; the goal pins it as a sentinel
        # for future heuristics that explicitly bucket unmatched markers).
        text = (
            "YÜRÜTME VE İDARE BÖLÜMÜ\n\n"
            "BİLİNMEYEN BİR BAŞLIK\n\n"
            "Some text\n"
        )
        items = parse_gazette_pages([(1, text)], PUB_DATE)
        assert items == []


# ── Reference number extraction ────────────────────────────────────────────


class TestReferenceExtraction:
    @pytest.mark.parametrize(
        ("body_line", "expected_ref"),
        [
            ("Kanun No: 7460", "7460"),
            ("Karar Sayısı: 152", "152"),
            ("Karar Sayısı: 2026/14523", "2026/14523"),
            ("Karar No: 2026/123", "2026/123"),
            ("Tebliğ No: 2026/89", "2026/89"),
            ("Yönetmelik No: 2026/7", "2026/7"),
        ],
    )
    def test_reference_pattern_matches(
        self, body_line: str, expected_ref: str
    ) -> None:
        text = f"YÜRÜTME VE İDARE BÖLÜMÜ\n\nKANUN\n\nTitle\n{body_line}\nBody"
        items = parse_gazette_pages([(1, text)], PUB_DATE)
        assert len(items) == 1
        assert items[0].reference_number == expected_ref

    def test_no_reference_yields_empty_string(self) -> None:
        text = "YÜRÜTME VE İDARE BÖLÜMÜ\n\nKANUN\n\nSome title\nNo reference here"
        items = parse_gazette_pages([(1, text)], PUB_DATE)
        assert len(items) == 1
        assert items[0].reference_number == ""

    def test_composite_court_reference_prefers_karar_no(self) -> None:
        text = (
            "YARGI BÖLÜMÜ\n\nMAHKEME KARARI\n\n"
            "Anayasa Mahkemesi Kararı\n"
            "Esas No: 2026/45      Karar No: 2026/123\n"
            "Body"
        )
        items = parse_gazette_pages([(1, text)], PUB_DATE)
        assert items[0].reference_number == "2026/123"


# ── Page range tracking ────────────────────────────────────────────────────


class TestPageRange:
    def test_single_page_item(self) -> None:
        text = "YÜRÜTME VE İDARE BÖLÜMÜ\n\nKANUN\n\nKanun No: 1\nBody"
        items = parse_gazette_pages([(3, text)], PUB_DATE)
        assert items[0].page_start == 3
        assert items[0].page_end == 3

    def test_multi_page_item(self) -> None:
        page1 = "YÜRÜTME VE İDARE BÖLÜMÜ\n\nKANUN\n\nKanun No: 1\nFirst page body"
        page2 = "More body on page two\nStill more"
        page3 = "Tail on page three"
        items = parse_gazette_pages(
            [(1, page1), (2, page2), (3, page3)], PUB_DATE
        )
        assert items[0].page_start == 1
        assert items[0].page_end == 3

    def test_separate_items_have_separate_page_ranges(self) -> None:
        page1 = "YÜRÜTME VE İDARE BÖLÜMÜ\n\nKANUN\n\nKanun No: 1\nA"
        page2 = "CUMHURBAŞKANLIĞI KARARNAMESİ\n\nKarar Sayısı: 9\nB"
        items = parse_gazette_pages([(1, page1), (2, page2)], PUB_DATE)
        assert items[0].page_start == 1
        assert items[0].page_end == 1
        assert items[1].page_start == 2
        assert items[1].page_end == 2


# ── Empty / pathological inputs ────────────────────────────────────────────


class TestPathologicalInputs:
    def test_empty_page_list_returns_empty_items(self) -> None:
        assert parse_gazette_pages([], PUB_DATE) == []

    def test_empty_page_text_returns_empty_items(self) -> None:
        assert parse_gazette_pages([(1, "")], PUB_DATE) == []

    def test_only_section_marker_no_items(self) -> None:
        # Section recognized but no item type → nothing to emit.
        text = "YÜRÜTME VE İDARE BÖLÜMÜ\n\n(no items)"
        assert parse_gazette_pages([(1, text)], PUB_DATE) == []

    def test_content_before_first_item_is_dropped(self) -> None:
        # Cover-page noise must not become an item.
        text = (
            "Resmi Gazete kapak metni\n"
            "Sayı: 32555\n"
            "Tarih: 23 Mayıs 2026\n"
            "YÜRÜTME VE İDARE BÖLÜMÜ\n\n"
            "KANUN\n\nKanun No: 1\nA"
        )
        items = parse_gazette_pages([(1, text)], PUB_DATE)
        assert len(items) == 1
        assert items[0].title == "Title".lower() or items[0].title  # any non-empty


# ── Item-type marker ordering ──────────────────────────────────────────────


class TestMarkerOrdering:
    def test_presidential_decree_does_not_match_shorter_prefix(self) -> None:
        # "CUMHURBAŞKANLIĞI KARARNAMESİ" must be detected as a single marker,
        # not as something starting with "CUMHURBAŞKANLIĞI" followed by
        # "KARARNAMESİ" being part of body.
        text = (
            "YÜRÜTME VE İDARE BÖLÜMÜ\n\n"
            "CUMHURBAŞKANLIĞI KARARNAMESİ\n\n"
            "Karar Sayısı: 1\nBody"
        )
        items = parse_gazette_pages([(1, text)], PUB_DATE)
        assert len(items) == 1
        assert items[0].item_type is GazetteItemType.PRESIDENTIAL_DECREE


# ── PDF wrapper integration ────────────────────────────────────────────────


class TestParseGazettePdfFixture:
    def test_sample_fixture_parses_to_four_items(self) -> None:
        pdf_bytes = (FIXTURE_DIR / "sample_gazette.pdf").read_bytes()
        items = parse_gazette_pdf(pdf_bytes, PUB_DATE)
        assert len(items) == 4
        # Order in the fixture: KANUN, KARARNAMESI, MAHKEME, TEBLİĞ.
        assert items[0].item_type is GazetteItemType.LAW
        assert items[0].reference_number == "7460"
        assert items[1].item_type is GazetteItemType.PRESIDENTIAL_DECREE
        assert items[1].reference_number == "152"
        assert items[2].item_type is GazetteItemType.COURT_DECISION
        assert items[2].reference_number == "2026/123"
        assert items[3].item_type is GazetteItemType.COMMUNIQUE
        assert items[3].reference_number == "2026/89"

    def test_corrupted_pdf_raises(self) -> None:
        # pdfplumber/pdfminer raise their own exception hierarchy (the exact
        # class has shifted across versions). Asserting "anything that isn't
        # a successful parse" is what the ingester actually needs to handle.
        corrupted = (FIXTURE_DIR / "corrupted.bin").read_bytes()
        with pytest.raises(Exception):  # noqa: B017 — see comment above
            parse_gazette_pdf(corrupted, PUB_DATE)

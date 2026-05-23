"""Tests for musahit.writer.template · the canonical section list."""

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


class TestPromptInstruction:
    """Regression for the 2026-05-23 placeholder-echo bug. Every section
    MUST carry a Turkish prompt_instruction so the writer LLM has an
    unambiguous fallback (including the empty-state phrase) and never
    echoes a generic placeholder back into the briefing."""

    def test_every_section_has_non_empty_prompt_instruction(self) -> None:
        for s in TEMPLATE_SECTIONS:
            assert s.prompt_instruction, (
                f"section {s.name} has empty prompt_instruction · the "
                f"writer will have nothing to render under {s.marker!r}"
            )
            assert s.prompt_instruction.strip() == s.prompt_instruction
            assert len(s.prompt_instruction) >= 20  # substantive guidance

    def test_no_section_carries_the_old_literal_placeholder(self) -> None:
        """The old placeholder text MUST NOT leak back into any instruction."""
        bad = "[içerik buraya"
        for s in TEMPLATE_SECTIONS:
            assert bad not in s.prompt_instruction, (
                f"section {s.name} still has the old placeholder fragment"
            )

    def test_data_carrying_sections_include_an_empty_state_phrase(self) -> None:
        """Sections whose content can be empty must instruct the model on
        the empty-state phrase. SİSTEM LOG is exempt because the payload
        always contains the run metadata."""
        empty_state_required = {
            "defcon_1_2", "defcon_3", "open_arcs", "defcon_4",
            "social_only", "ambient", "resolved_arcs",
        }
        for s in TEMPLATE_SECTIONS:
            if s.name not in empty_state_required:
                continue
            assert "(bugün" in s.prompt_instruction, (
                f"section {s.name} missing empty-state phrase '(bugün …)'"
            )

    def test_names_remain_unique_after_extension(self) -> None:
        names = [s.name for s in TEMPLATE_SECTIONS]
        assert len(names) == len(set(names))

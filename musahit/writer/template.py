"""Canonical template structure per ADR-009.

The briefing markdown is a fixed sequence of top-level (``##``) sections,
each prefixed with the ``❯`` marker that downstream stages (validator,
TTS scope extractor, dashboard renderer) key off. Adding or removing a
section is an ADR-009 amendment, not a silent edit.

The DEFCON-3 section nests one ``###`` subsection per category. Other
sections may have ``###`` subsections too (one per item) but those are
content, not structure — the validator does not pin them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TemplateSection:
    """One required top-level section."""

    marker: str  # the exact line text, e.g. "## ❯ DEFCON 1-2 · ÖNCELİKLİ"
    name: str  # short identifier for logs / error messages


# Ordered per ADR-009 § Master template. The order is load-bearing —
# the briefing's skim-and-stop discipline depends on DEFCON 1-2 being
# first and SİSTEM LOG last.
TEMPLATE_SECTIONS: tuple[TemplateSection, ...] = (
    TemplateSection("## ❯ DEFCON 1-2 · ÖNCELİKLİ", "defcon_1_2"),
    TemplateSection("## ❯ DEFCON 3 · MATERYAL", "defcon_3"),
    TemplateSection("## ❯ AÇIK GELİŞMELER · DEVAM EDEN TAKİP", "open_arcs"),
    TemplateSection("## ❯ DEFCON 4 · GÜNDEM", "defcon_4"),
    TemplateSection("## ❯ DİKKAT · YALNIZCA SOSYALDE", "social_only"),
    TemplateSection("## ❯ AMBİYANS · DEFCON 5", "ambient"),
    TemplateSection("## ❯ KAPATILAN HİKAYELER", "resolved_arcs"),
    TemplateSection("## ❯ SİSTEM LOG", "system_log"),
)

# Headline of the briefing — the validator allows this top-level "# "
# heading before the first "## ❯ " section.
DOCUMENT_TITLE: str = "# MÜŞAHİT · GÜNLÜK BRİF"

# Category subsection markers under DEFCON 3. Order documented in ADR-009.
DEFCON_3_CATEGORY_SUBSECTIONS: tuple[str, ...] = (
    "### POLİTİKA",
    "### EKONOMİ",
    "### YARGI",
    "### GÜVENLİK",
    "### DİPLOMASİ",
    "### MEVZUAT",
    "### TOPLUM",
)


__all__ = [
    "DEFCON_3_CATEGORY_SUBSECTIONS",
    "DOCUMENT_TITLE",
    "TEMPLATE_SECTIONS",
    "TemplateSection",
]

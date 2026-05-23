"""Pure parser for T.C. Resmî Gazete PDF documents.

The Resmî Gazete (Official Gazette) is published once per day (twice on rare
"Mükerrer" supplement days) as a single PDF that contains many distinct
*items* — laws, presidential decrees, regulations, communiqués, appointments,
court decisions — grouped into three top-level *sections*:

* **YÜRÜTME VE İDARE BÖLÜMÜ** — executive branch (laws, decrees, regs, etc.)
* **YARGI BÖLÜMÜ** — judicial (constitutional court, court of cassation, etc.)
* **İLAN BÖLÜMÜ** — announcements (tenders, statutory notices)

Each item carries a reference number (``Kanun No: 7460`` ·
``Karar Sayısı: 152`` · ``Esas No: 2026/45 Karar No: 2026/123`` · etc.)
that we extract as the source-native identifier (``feed_entry_id`` per
ADR-015).

This module is **pure** — no I/O, no clock, no network. The only external
dependency is :mod:`pdfplumber` for text extraction, and even that is
isolated behind :func:`parse_gazette_pdf` so tests can drive the inner
:func:`parse_gazette_pages` with synthetic page strings.

Per the build-plan tripwire, the section/item enums below are the closed
set. If real Gazette content surfaces a type that does not fit, the
unmatched item flows to :class:`GazetteItemType.OTHER` with the reference
extraction still attempted — the operator surfaces the gap and the enum
is expanded by a follow-up ADR amendment, not silently.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

import pdfplumber

# ── Enums ──────────────────────────────────────────────────────────────────


class GazetteSection(StrEnum):
    """Top-level section of a Resmî Gazete edition."""

    EXECUTIVE = "YÜRÜTME VE İDARE BÖLÜMÜ"
    JUDICIAL = "YARGI BÖLÜMÜ"
    ANNOUNCEMENT = "İLAN BÖLÜMÜ"


class GazetteItemType(StrEnum):
    """Item type within a section.

    ``OTHER`` is the explicit "did not match any known marker" bucket — see
    the module docstring for the policy on expanding the enum.
    """

    LAW = "KANUN"
    PRESIDENTIAL_DECREE = "CUMHURBAŞKANLIĞI KARARNAMESİ"
    REGULATION = "YÖNETMELİK"
    COMMUNIQUE = "TEBLİĞ"
    APPOINTMENT = "ATAMA KARARI"
    COURT_DECISION = "MAHKEME KARARI"
    OTHER = "DİĞER"


# ── Data class ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GazetteItem:
    """One parsed item from a Resmî Gazete edition.

    Attributes:
        section: Which top-level section this item belongs to. Defaults to
            ``EXECUTIVE`` when the parser encounters an item before any
            explicit section marker (the regular Gazette always opens with
            the executive section, so this is the safe default).
        item_type: One of :class:`GazetteItemType`; ``OTHER`` when the
            parser found no matching type marker but kept reading content
            so the item is not lost.
        reference_number: The source-native reference (``"7460"`` ·
            ``"2026/14523"`` · etc.) if the parser could extract one; the
            empty string when extraction failed. The ingester maps the
            empty string to ``NULL`` for the ``feed_entry_id`` column.
        title: First non-empty content line after the type marker — a
            reasonable proxy for the operator-facing title of the item.
        body: All other content lines, joined with newlines.
        page_start: 1-based PDF page where the item begins.
        page_end: 1-based PDF page where the item ends (inclusive). Equal
            to ``page_start`` for single-page items.
    """

    section: GazetteSection
    item_type: GazetteItemType
    reference_number: str
    title: str
    body: str
    page_start: int
    page_end: int


# ── Detection helpers ──────────────────────────────────────────────────────

# Section markers come ordered (lookup is linear; tiny set).
_SECTION_MARKERS: tuple[GazetteSection, ...] = (
    GazetteSection.EXECUTIVE,
    GazetteSection.JUDICIAL,
    GazetteSection.ANNOUNCEMENT,
)

# Item-type markers. Order matters when one marker is a substring of another
# (e.g. "CUMHURBAŞKANLIĞI KARARNAMESİ" matches before any shorter prefix).
_ITEM_TYPE_MARKERS: tuple[GazetteItemType, ...] = (
    GazetteItemType.PRESIDENTIAL_DECREE,
    GazetteItemType.APPOINTMENT,
    GazetteItemType.COURT_DECISION,
    GazetteItemType.REGULATION,
    GazetteItemType.COMMUNIQUE,
    GazetteItemType.LAW,
)

# Reference patterns tried in order. Each captures one group: the reference.
_REFERENCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Karar\s+No\s*[:：]\s*(\d{1,4}/\d{1,5})", re.IGNORECASE),
    re.compile(r"Karar\s+Sayısı\s*[:：]\s*(\d{1,5}(?:/\d{1,5})?)", re.IGNORECASE),
    re.compile(r"Kanun\s+No\s*[:：]\s*(\d{1,5}(?:/\d{1,5})?)", re.IGNORECASE),
    re.compile(r"Tebliğ\s+No\s*[:：]\s*(\d{1,5}(?:/\d{1,5})?)", re.IGNORECASE),
    re.compile(r"Yönetmelik\s+No\s*[:：]\s*(\d{1,5}(?:/\d{1,5})?)", re.IGNORECASE),
    re.compile(r"\bSayı\s*[:：]\s*(\d{1,5}(?:/\d{1,5})?)", re.IGNORECASE),
)


def _match_section(line: str) -> GazetteSection | None:
    stripped = line.strip()
    if not stripped:
        return None
    for section in _SECTION_MARKERS:
        if section.value in stripped:
            return section
    return None


def _match_item_type(line: str) -> GazetteItemType | None:
    stripped = line.strip()
    if not stripped:
        return None
    for item_type in _ITEM_TYPE_MARKERS:
        if item_type.value == stripped:
            return item_type
    return None


def _extract_reference(text: str) -> str | None:
    for pattern in _REFERENCE_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


# ── Public parser ──────────────────────────────────────────────────────────


def parse_gazette_pdf(
    pdf_bytes: bytes,
    publication_date: date,
) -> list[GazetteItem]:
    """Extract text from a Resmî Gazete PDF and parse it into items.

    The function opens the bytes with :mod:`pdfplumber`, extracts text per
    page, and delegates the structural parsing to
    :func:`parse_gazette_pages`. Any pdfplumber-level failure (corrupted
    bytes, unsupported PDF, etc.) raises — the caller (the ingester) is
    expected to translate that into :class:`IngestStatus.PARSE_ERROR`.
    """
    pages: list[tuple[int, str]] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            pages.append((i, text))
    return parse_gazette_pages(pages, publication_date)


def parse_gazette_pages(
    pages: list[tuple[int, str]],
    publication_date: date,
) -> list[GazetteItem]:
    """Parse pre-extracted (page_number, text) tuples into a list of items.

    Exposed as a public API specifically so the test suite can drive every
    section/item edge case with synthetic strings (no PDFs needed). The
    PDF-flavored entry point :func:`parse_gazette_pdf` is a thin wrapper
    over this function plus :mod:`pdfplumber` for text extraction.

    ``publication_date`` is accepted for forward-compatibility with parsers
    that need to disambiguate ranges of references; the current
    implementation ignores it but the ingester depends on the parameter
    being present.
    """
    items: list[GazetteItem] = []

    current_section: GazetteSection = GazetteSection.EXECUTIVE
    current_item_type: GazetteItemType | None = None
    current_lines: list[str] = []
    current_ref: str | None = None
    current_title: str | None = None
    current_page_start: int | None = None
    current_page_end: int | None = None

    def _emit() -> None:
        nonlocal current_item_type, current_lines, current_ref
        nonlocal current_title, current_page_start, current_page_end
        if current_item_type is None or current_page_start is None:
            return
        items.append(
            GazetteItem(
                section=current_section,
                item_type=current_item_type,
                reference_number=current_ref or "",
                title=(current_title or "").strip(),
                body="\n".join(current_lines).strip(),
                page_start=current_page_start,
                page_end=current_page_end or current_page_start,
            )
        )
        current_item_type = None
        current_lines = []
        current_ref = None
        current_title = None
        current_page_start = None
        current_page_end = None

    for page_num, page_text in pages:
        for line in page_text.splitlines():
            stripped = line.strip()

            section = _match_section(line)
            if section is not None:
                _emit()
                current_section = section
                continue

            item_type = _match_item_type(line)
            if item_type is not None:
                _emit()
                current_item_type = item_type
                current_page_start = page_num
                current_page_end = page_num
                continue

            if current_item_type is None:
                # Pre-item noise (cover page, headers) — drop.
                continue

            # Content line: update title, ref, body, page range.
            if stripped:
                if current_title is None:
                    current_title = stripped
                if current_ref is None:
                    candidate = _extract_reference(stripped)
                    if candidate:
                        current_ref = candidate
                current_lines.append(line)
                current_page_end = page_num

    _emit()
    return items


__all__ = [
    "GazetteItem",
    "GazetteItemType",
    "GazetteSection",
    "parse_gazette_pages",
    "parse_gazette_pdf",
]

"""Extract the voiced scope from a briefing markdown.

ADR-009 § TTS scope defines exactly what Piper reads:

1. The header (Tarih, Zirve DEFCON, İşlenen olay) — everything above
   the first ``## ❯`` section.
2. ``DEFCON 1-2 · ÖNCELİKLİ`` — full content.
3. ``DEFCON 3 · MATERYAL`` — one-paragraph summaries only; the
   ``**Kaynaklar** · …`` attribution lines are visual-only and excluded.
4. ``AÇIK GELİŞMELER · DEVAM EDEN TAKİP`` — full content.
5. A closing line: "DEFCON 4 ve sonrası dashboard'da görüntülenebilir."

Skipped (read on the dashboard, not voiced):

* ``DEFCON 4 · GÜNDEM``
* ``DİKKAT · YALNIZCA SOSYALDE``
* ``AMBİYANS · DEFCON 5``
* ``KAPATILAN HİKAYELER``
* ``SİSTEM LOG``

The parser does not validate template completeness — the writer's
:func:`musahit.writer.validator.validate_briefing_markdown` already does
that. The extractor is forgiving: missing voiced sections become empty
strings rather than errors, so the synthesiser can still produce a
short briefing when the writer landed only a degraded fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Marker constants — the exact lines from ADR-009. Kept as literals
# rather than imported from :mod:`musahit.writer.template` so the TTS
# stage can be reasoned about independently from the writer.

MARKER_DEFCON_1_2 = "## ❯ DEFCON 1-2 · ÖNCELİKLİ"
MARKER_DEFCON_3 = "## ❯ DEFCON 3 · MATERYAL"
MARKER_OPEN_ARCS = "## ❯ AÇIK GELİŞMELER · DEVAM EDEN TAKİP"
MARKER_DEFCON_4 = "## ❯ DEFCON 4 · GÜNDEM"
MARKER_SOCIAL_ONLY = "## ❯ DİKKAT · YALNIZCA SOSYALDE"
MARKER_AMBIENT = "## ❯ AMBİYANS · DEFCON 5"
MARKER_RESOLVED = "## ❯ KAPATILAN HİKAYELER"
MARKER_SYSTEM_LOG = "## ❯ SİSTEM LOG"

VOICED_MARKERS: tuple[str, ...] = (
    MARKER_DEFCON_1_2,
    MARKER_DEFCON_3,
    MARKER_OPEN_ARCS,
)

SKIPPED_MARKERS: tuple[str, ...] = (
    MARKER_DEFCON_4,
    MARKER_SOCIAL_ONLY,
    MARKER_AMBIENT,
    MARKER_RESOLVED,
    MARKER_SYSTEM_LOG,
)

# All ## ❯ markers in the template — used to detect any section start
# regardless of voiced/skipped status. The union of VOICED_MARKERS +
# SKIPPED_MARKERS is exactly the ADR-009 set.
ALL_MARKERS: tuple[str, ...] = VOICED_MARKERS + SKIPPED_MARKERS

CLOSING_LINE = "DEFCON 4 ve sonrası dashboard'da görüntülenebilir."

# Source-attribution line (visual-only, dropped from DEFCON 3).
_KAYNAKLAR_RE = re.compile(r"^\*?\*?Kaynaklar\*?\*?\s*·.*$", flags=re.MULTILINE)


@dataclass(frozen=True)
class VoicedBriefing:
    """The voiced scope split into ordered chunks.

    The synthesiser interleaves a transition tick tone between chunks
    so the listener hears section boundaries. ``joined()`` returns the
    concatenated form for callers that don't need chunk-level control
    (``extract_voiced_sections`` is the goal-spec entry point).

    All fields default to empty strings — a briefing that produced no
    DEFCON 1-2 items, for example, still synthesises a coherent run
    of (header → DEFCON 3 → open arcs → closing) without surfacing the
    absent section as a gap.
    """

    header: str = ""
    defcon_priority: str = ""
    defcon_material: str = ""
    open_arcs: str = ""
    closing: str = CLOSING_LINE
    # Keeping the raw markers we found is useful for debugging; not
    # used by the synthesiser.
    found_markers: tuple[str, ...] = field(default_factory=tuple)

    def chunks(self) -> list[str]:
        """Non-empty section chunks in voicing order.

        Empty sections (e.g., no DEFCON 1-2 today) are skipped — Piper
        would synthesise silence for an empty string which is wasted
        time and produces awkward tick-tick-tick transitions with no
        speech between.
        """
        ordered = [
            self.header,
            self.defcon_priority,
            self.defcon_material,
            self.open_arcs,
            self.closing,
        ]
        return [chunk.strip() for chunk in ordered if chunk and chunk.strip()]

    def joined(self) -> str:
        """All chunks joined with blank-line separators."""
        return "\n\n".join(self.chunks())


def extract_voiced_briefing(briefing_md: str) -> VoicedBriefing:
    """Parse ``briefing_md`` and return the voiced sections in order.

    Returns an instance with ``closing`` already populated; the four
    content fields are filled by walking the markdown line-by-line and
    bucketing content under whichever ``## ❯`` section it currently
    belongs to. The very first lines (above any section marker) form
    the header.
    """
    sections = _split_into_sections(briefing_md)
    found = tuple(m for m in ALL_MARKERS if m in sections)
    defcon_3_text = _strip_source_lines(sections.get(MARKER_DEFCON_3, ""))
    return VoicedBriefing(
        header=sections.get("__header__", "").strip(),
        defcon_priority=sections.get(MARKER_DEFCON_1_2, "").strip(),
        defcon_material=defcon_3_text.strip(),
        open_arcs=sections.get(MARKER_OPEN_ARCS, "").strip(),
        closing=CLOSING_LINE,
        found_markers=found,
    )


def extract_voiced_sections(briefing_md: str) -> str:
    """Return the joined voiced text — the goal-spec entry point."""
    return extract_voiced_briefing(briefing_md).joined()


def _split_into_sections(briefing_md: str) -> dict[str, str]:
    """Bucket markdown lines under their containing ``## ❯`` marker.

    Lines before the first marker are stored under the synthetic key
    ``"__header__"``. Lines after a marker accumulate under that
    marker until the next ``## ❯`` line is encountered. Section markers
    themselves are NOT stored in the bucket text — only the body lines
    are kept, so the consumer doesn't end up reading the marker line
    aloud to the operator.

    The trailing ``---`` horizontal rule lines between sections are
    kept in the bucket text and stripped by the preprocessor; keeping
    them simplifies bucketing.
    """
    buckets: dict[str, list[str]] = {"__header__": []}
    current_key: str = "__header__"
    for line in briefing_md.splitlines():
        stripped = line.strip()
        if stripped in ALL_MARKERS:
            current_key = stripped
            buckets.setdefault(current_key, [])
            continue
        buckets[current_key].append(line)
    return {k: "\n".join(v) for k, v in buckets.items()}


def _strip_source_lines(text: str) -> str:
    """Remove ``**Kaynaklar** · …`` attribution lines.

    Per ADR-009 § TTS scope, the source list is visual-only — it would
    read as a wall of source IDs and band names. The DEFCON 3 section
    keeps the per-item heading, confidence line, summary, but loses the
    source attribution.
    """
    return _KAYNAKLAR_RE.sub("", text)


__all__ = [
    "ALL_MARKERS",
    "CLOSING_LINE",
    "MARKER_AMBIENT",
    "MARKER_DEFCON_1_2",
    "MARKER_DEFCON_3",
    "MARKER_DEFCON_4",
    "MARKER_OPEN_ARCS",
    "MARKER_RESOLVED",
    "MARKER_SOCIAL_ONLY",
    "MARKER_SYSTEM_LOG",
    "SKIPPED_MARKERS",
    "VOICED_MARKERS",
    "VoicedBriefing",
    "extract_voiced_briefing",
    "extract_voiced_sections",
]

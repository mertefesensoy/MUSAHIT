"""Briefing markdown validator.

Pins the eight required top-level sections from ADR-009 in their
documented order. Returns a list of error strings · empty means valid.

The validator is intentionally tight on **structural** discipline
(section presence, marker prefix, order) and loose on **content**
(the LLM may use any number of ``###`` subsections, any bullet style,
any prose length). If the writer drifts on structure the prompt is
retried; if the writer drifts on content the operator sees it but
parsing still works.

Per-section validation (``validate_section``) is stricter on content
than the whole-briefing validator. After the 2026-05-27 hallucinated
specimen showed Trendyol-LLM 7B echoing DISCIPLINE_RULES verbatim into
DİKKAT and emitting "Adım 1:" / "Gerekçe:" chain-of-thought scaffolding
into AMBİYANS, the section validator now rejects:

* Prompt echo · any DISCIPLINE_RULES / "BÖLÜM VERİSİ" / "ÇIKTI" /
  "GÖREV ·" marker substring in the body.
* Chain-of-thought scaffolding · lines matching ``^\\s*Adım\\s*\\d+:``
  or ``^\\s*Gerekçe\\s*:`` (Turkish CoT).

Rejected sections become honest stubs via ``render_section_stub`` ·
the briefing never ships fabricated, echoed, or CoT-scaffolded content.
"""

from __future__ import annotations

import re

from musahit.writer.template import DOCUMENT_TITLE, TEMPLATE_SECTIONS

EXTRA_SECTION_ALLOWED_PREFIX: str = "## ❯ "

# Substring of the old single-placeholder instruction
# (``[içerik buraya · şablon talimatlarına bak]``). Any briefing that
# still contains this fragment means the writer echoed the prompt's
# template instructions back verbatim · the 2026-05-23 smoke-run bug.
# The fragment is unusual enough that no legitimate content produces it;
# matching only the opening "[içerik buraya" keeps the check robust
# against any future tweak to the instruction trailer.
_PLACEHOLDER_ECHO_SUBSTRING: str = "[içerik buraya"

# Substrings that, when present in a section body, indicate the model
# echoed the per-section prompt back instead of producing content. All
# come from the 2026-05-27 hallucinated specimen. Each is rare enough
# in legitimate Turkish prose that a false positive is implausible.
_PROMPT_ECHO_MARKERS: tuple[str, ...] = (
    "KURALLAR (ADR-009)",
    "BÖLÜM VERİSİ:",
    "ÇIKTI (yalnızca",
    "GÖREV ·",
    "Hedef bölüm ·",
)

# Chain-of-thought scaffolding patterns (Turkish). The hallucinated
# specimen emitted "Adım 1: ..." / "Gerekçe: ..." into AMBİYANS as if
# the model were showing its work. Any line whose first non-blank
# content matches these prefixes is CoT leak · reject the section.
_COT_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*Adım\s*\d+\s*:", re.MULTILINE),
    re.compile(r"^\s*Gerekçe\s*:", re.MULTILINE),
)


def validate_briefing_markdown(text: str) -> list[str]:
    """Return a list of validation errors; empty list means valid.

    Each error is a short, human-readable string suitable for inclusion
    in a retry prompt back to the writer model.
    """
    errors: list[str] = []
    if not text or not text.strip():
        errors.append("briefing markdown is empty")
        return errors

    lines = text.splitlines()

    # 1) Document title must be the first non-blank line.
    first_nonblank = next((line for line in lines if line.strip()), "")
    if first_nonblank.strip() != DOCUMENT_TITLE:
        errors.append(
            f"first non-blank line must be exactly '{DOCUMENT_TITLE}', "
            f"got '{first_nonblank.strip()}'"
        )

    # 2) Find every top-level "## " section header in order.
    found_section_lines: list[str] = [
        line.strip() for line in lines if line.startswith("## ")
    ]

    # 3) Each required section must be present, in the exact order, with
    # the exact marker text.
    required = [s.marker for s in TEMPLATE_SECTIONS]
    j = 0  # index into required
    for line in found_section_lines:
        if j < len(required) and line == required[j]:
            j += 1
            continue
        # Found a top-level section that isn't the next required one.
        if line not in required:
            errors.append(f"unexpected top-level section: '{line}'")
        else:
            # It's a required section but out of order.
            errors.append(
                f"required section out of order: '{line}' "
                f"(expected '{required[j]}' next)"
            )

    if j < len(required):
        missing = required[j:]
        errors.append(
            "missing required sections (in order): " + " | ".join(missing)
        )

    # 4) Every top-level section must use the ❯ marker prefix.
    for line in found_section_lines:
        if not line.startswith(EXTRA_SECTION_ALLOWED_PREFIX):
            errors.append(
                f"top-level section missing '❯' prefix: '{line}' "
                f"(must start with '{EXTRA_SECTION_ALLOWED_PREFIX}')"
            )

    # 5) Reject any briefing that contains the prompt's template-
    # instruction placeholder verbatim · the writer echoed the
    # instruction text instead of producing content.
    if _PLACEHOLDER_ECHO_SUBSTRING in text:
        errors.append(
            "briefing contains unfilled template placeholder · model "
            "echoed the instruction text"
        )

    return errors


def validate_section(text: str, section_idx: int) -> bool:
    """Per-section validator.

    Pass conditions (structural):
    - First non-blank line is TEMPLATE_SECTIONS[section_idx].marker
    - No other lines starting with '## ❯' appear in the text
    - Text contains at least one non-marker non-empty line

    Reject conditions (content · added 2026-05-28 after the
    hallucinated specimen review):
    - Text contains any DISCIPLINE_RULES / per-section prompt marker
      substring (``_PROMPT_ECHO_MARKERS``) · the model echoed the prompt
      back instead of producing content.
    - Text contains any chain-of-thought line (``Adım N:`` /
      ``Gerekçe:``) · CoT scaffolding must not ship in the briefing.
    - Text contains the old single-placeholder fragment
      (``[içerik buraya``) · 2026-05-23 placeholder-echo bug.

    A rejected section is replaced with ``render_section_stub`` by the
    Briefer; the operator sees an honest stub, never fabricated prose.
    """
    if not text or not text.strip():
        return False
    lines = text.splitlines()
    first_nonblank = next((line for line in lines if line.strip()), "")
    expected_marker = TEMPLATE_SECTIONS[section_idx].marker
    if first_nonblank.strip() != expected_marker:
        return False
    marker_count = sum(1 for line in lines if line.strip().startswith("## ❯"))
    if marker_count > 1:
        return False
    content_lines = [
        line for line in lines
        if line.strip() and line.strip() != expected_marker
    ]
    if not content_lines:
        return False

    body = "\n".join(content_lines)

    # Reject prompt echo · the body must not carry DISCIPLINE_RULES /
    # per-section prompt markers verbatim.
    for marker in _PROMPT_ECHO_MARKERS:
        if marker in body:
            return False

    # Reject chain-of-thought scaffolding · the model should produce
    # the briefing section, not show its reasoning.
    for pattern in _COT_LINE_PATTERNS:
        if pattern.search(body):
            return False

    # Reject the historical placeholder echo from the 2026-05-23 bug.
    return _PLACEHOLDER_ECHO_SUBSTRING not in body


__all__ = [
    "EXTRA_SECTION_ALLOWED_PREFIX",
    "validate_briefing_markdown",
    "validate_section",
]

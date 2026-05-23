"""Briefing markdown validator.

Pins the eight required top-level sections from ADR-009 in their
documented order. Returns a list of error strings · empty means valid.

The validator is intentionally tight on **structural** discipline
(section presence, marker prefix, order) and loose on **content**
(the LLM may use any number of ``###`` subsections, any bullet style,
any prose length). If the writer drifts on structure the prompt is
retried; if the writer drifts on content the operator sees it but
parsing still works.
"""

from __future__ import annotations

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


__all__ = [
    "EXTRA_SECTION_ALLOWED_PREFIX",
    "validate_briefing_markdown",
]

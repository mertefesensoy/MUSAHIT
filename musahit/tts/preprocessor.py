"""Turkish text preprocessing for TTS.

The Piper voice ``tr_TR-dfki-medium`` pronounces unadorned Turkish text
naturally enough on its own, but a handful of patterns recur in MÜŞAHİT
output that benefit from preprocessing before synthesis:

* Acronyms (``TCMB``, ``BDDK`` …) read out letter-by-letter as Turkish
  pronunciations of each letter — Piper otherwise produces an unhappy
  consonant cluster collapse on ``TCMB``.
* ``DEFCON 2`` reads more clearly as ``DEFCON İki`` — the numeral alone
  can be read in English by the voice.
* Markdown formatting (``**bold**``, ``[link](url)``, ``# Header``)
  produces audible "yıldız", "köşeli parantez", etc. — must be stripped.
* Source-attribution lines (``**Kaynaklar** · sabah·gov_aligned · …``)
  are visual-only per ADR-009 § TTS scope — they read as a wall of
  source IDs and band names — must be removed.

This module's sole entry point is :func:`preprocess_for_tts`. It is a
pure function (no I/O, no side effects); callers pass the extracted
voiced text and receive a TTS-ready string.
"""

from __future__ import annotations

import re

# ── Abbreviation expansion ────────────────────────────────────────────────
#
# Spelled out as Turkish letter names. The Piper voice handles "Tee"
# (English /tiː/-style) consistently for the letter T — the dictionary
# below uses the Turkish letter-name spelling so each letter is read in
# Turkish pronunciation: T = "Te", C = "Ce", M = "Me", B = "Be", etc.
#
# Multi-letter expansions use space-separated capitalised syllables so
# the voice inserts a brief pause between letters rather than running
# them together.

ABBREVIATIONS: dict[str, str] = {
    # Finance / markets
    "TCMB": "Te Ce Me Be",
    "BDDK": "Be De De Ka",
    "SPK": "Se Pe Ka",
    "EPDK": "E Pe De Ka",
    "BIST": "Bist",  # widely read as a word
    "KAP": "Ka A Pe",
    "BES": "Be E Se",
    # Regulators / agencies
    "RTÜK": "Re Te Ü Ka",
    "TÜİK": "Tüik",  # widely read as a word
    # Political institutions
    "TBMM": "Te Be Me Me",
    "AYM": "A Ye Me",
    "YSK": "Ye Se Ka",
    "HSK": "Ha Se Ka",
    # Parties (commonly heard as words but acronyms enough to expand)
    "MHP": "Me He Pe",
    "CHP": "Ce He Pe",
    "AKP": "A Ka Pe",
    "DEM": "Dem",
    # Diplomacy
    "MİT": "Mit",
    "AB": "A Be",
    "BM": "Be Me",
    "NATO": "Nato",
}

# DEFCON numeric → Turkish word. Used by :func:`_replace_defcon_numbers`.
DEFCON_TR_NUMBERS: dict[int, str] = {
    1: "Bir",
    2: "İki",
    3: "Üç",
    4: "Dört",
    5: "Beş",
}


# ── Regex patterns ────────────────────────────────────────────────────────
#
# Bold + italic must be stripped *before* link patterns so we don't see
# ``[**label**](url)`` confuse the link extractor. Headers must be
# stripped *before* DEFCON-number replacement so the section line
# ``## ❯ DEFCON 1-2 · ÖNCELİKLİ`` doesn't get half-rewritten.

_HEADER_RE = re.compile(r"^#{1,6}\s*", flags=re.MULTILINE)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_KAYNAKLAR_RE = re.compile(r"^\*?\*?Kaynaklar\*?\*?\s*·.*$", flags=re.MULTILINE)
# DEFCON appears in the briefing in three shapes that all need
# Piper-friendly respelling:
#   ``DEFCON 2``         — numeral right after a single space
#   ``DEFCON · 3``       — section / metadata line with a middle-dot
#                          (or ``:`` or ``-``) separator between the
#                          term and the numeral
#   ``Zirve DEFCON``     — standalone label without a trailing numeral
#
# The optional non-capture group ``(?:\s*[·:-]?\s*([1-5]))?`` captures
# the digit when present; when it fails to match (no digit available),
# the whole match is just ``\bDEFCON\b`` and the replacement function
# emits the bare ``Defkon`` form. Case-sensitive: ``Defkon`` in the
# output cannot re-match.
_DEFCON_NUM_RE = re.compile(r"\bDEFCON\b(?:\s*[·:-]?\s*([1-5]))?")
# Markdown horizontal rule / arrow marker remnants left over from
# extracted briefing markdown. ``❯`` is the section pip.
_HRULE_RE = re.compile(r"^---\s*$", flags=re.MULTILINE)
_ARROW_RE = re.compile(r"❯\s*")
# Multiple blank lines → single blank line (preserves paragraph breaks
# for Piper's natural sentence pausing).
_BLANKLINES_RE = re.compile(r"\n{3,}")


def preprocess_for_tts(text: str) -> str:
    """Clean ``text`` into a TTS-ready string.

    The order of operations matters; see the regex section above. The
    function never returns an empty string for non-empty input — a
    fully-stripped section becomes a single blank line, which Piper
    handles silently.
    """
    if not text:
        return ""

    # 1. Drop the visual-only source-attribution lines.
    text = _KAYNAKLAR_RE.sub("", text)

    # 2. Strip markdown formatting (headers, bold, italic, links).
    text = _HEADER_RE.sub("", text)
    text = _BOLD_RE.sub(r"\1", text)
    text = _ITALIC_RE.sub(r"\1", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _HRULE_RE.sub("", text)
    text = _ARROW_RE.sub("", text)

    # 3. Expand DEFCON numerals to Turkish words.
    text = _DEFCON_NUM_RE.sub(_defcon_num_repl, text)

    # 4. Expand acronyms (whole-word, case-sensitive — Turkish acronyms
    #    are always uppercase in MÜŞAHİT's pipeline so case-sensitivity
    #    is safe and faster than re-matching mixed case).
    for acronym, expansion in ABBREVIATIONS.items():
        text = re.sub(rf"\b{re.escape(acronym)}\b", expansion, text)

    # 5. Collapse runs of blank lines to a single blank line.
    text = _BLANKLINES_RE.sub("\n\n", text)

    return text.strip() + "\n" if text.strip() else ""


def _defcon_num_repl(match: re.Match[str]) -> str:
    # Respell ``DEFCON`` as ``Defkon`` for the TTS-bound text only.
    # Piper's Turkish voice applies Turkish phoneme rules to "DEFCON"
    # which produces an awkward "De-Fe-Kon"; Turkish speakers familiar
    # with the term actually use the English-style "Def-Kon". Respelling
    # with mixed case + the ``k`` consonant nudges the voice into the
    # right phoneme path without translating the term. The written
    # briefing (briefing.md, dashboard HTML) keeps "DEFCON" — this
    # respelling only flows into ``PiperVoice.synthesize_wav``.
    #
    # When the regex captured a trailing 1-5 digit (the common case in
    # the briefing's section markers), we emit ``Defkon {numeral}``
    # with the Turkish word for the digit. When the regex matched a
    # bare ``DEFCON`` (header labels like "Zirve DEFCON"), group 1 is
    # None and we emit ``Defkon`` alone.
    digit = match.group(1)
    if digit is None:
        return "Defkon"
    return f"Defkon {DEFCON_TR_NUMBERS[int(digit)]}"


__all__ = [
    "ABBREVIATIONS",
    "DEFCON_TR_NUMBERS",
    "preprocess_for_tts",
]

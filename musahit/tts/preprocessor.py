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
* Dormant arc lines (2026-05-29 Group-A) — itemized arc lines carry a
  recency suffix (``· bugün`` / ``· dün`` / ``· N gün önce``). FRESH
  lines (bugün/dün) are voiced; DORMANT lines (``N gün önce``, N≥2 by
  construction — 1 day is always "dün") are dropped from the spoken
  text so the voice briefing covers only what moved recently. A block
  where every line is dormant collapses to one short spoken note rather
  than vanishing into a confusing silence. The on-disk briefing keeps
  every dormant line; only the Piper-bound text drops them.

This module's sole entry point is :func:`preprocess_for_tts`. It is a
pure function (no I/O, no side effects); callers pass the extracted
voiced text and receive a TTS-ready string. It never touches the
on-disk ``briefing.md`` — the writer owns that file and the markdown
keeps all dormant arcs.
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
# Inline-code backticks wrap arc ids in the itemized arc lines
# (`` `arc_20260523_0001` ``). The written briefing renders them as code;
# the voice must not read a stray backtick, so strip them.
_BACKTICK_RE = re.compile(r"`")
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
# Arc story IDs (``arc_20260523_0001``) are cross-reference anchors in
# the written briefing. Piper reads the YYYYMMDD segment as a huge
# integer ("yirmi milyon iki yüz altmış bin…") — audio garbage. The
# TTS-bound text rewrites them to ``hikaye N`` using only the trailing
# serial, matching the operator's spoken "story N" convention.
_ARC_ID_RE = re.compile(r"arc_\d{8}_(\d{4})")

# ── Dormancy skip (2026-05-29 Group-A) ─────────────────────────────────────
#
# The writer appends a recency suffix to every itemized arc line (see
# musahit.arcs.freshness.recency_label · the single source of truth for
# the vocabulary). FRESH lines end in "· bugün" or "· dün"; DORMANT lines
# end in "· N gün önce" (N≥2 — a 1-day gap is always "dün", never
# "1 gün önce", so any "N gün önce" line is guaranteed dormant). The
# match is anchored to the END of the line so ordinary prose that merely
# mentions "… 6 gün önce açıklandı" is not affected.
_DORMANT_LINE_RE = re.compile(r"·\s*\d+\s+gün\s+önce\s*$")
_FRESH_LINE_RE = re.compile(r"·\s*(?:bugün|dün)\s*$")

# The dormancy filter only touches DETERMINISTIC arc lines · a bullet that
# carries a backtick-wrapped arc id (the exact shape musahit.writer.render
# emits: "- … · `arc_YYYYMMDD_NNNN` · recency"). Anchoring to this shape
# is essential: the filter runs over the WHOLE voiced text, which includes
# DEFCON 1-2 / DEFCON 3 free-form LLM prose. Without the bullet+arc-id
# guard, an LLM line that merely ended in "· N gün önce" (e.g.
# "**Güncelleme** · 3 gün önce") would be silently dropped from the
# spoken briefing — losing real content and even injecting a false
# all-dormant note. See the 2026-05-29 arc-freshness review.
_ARC_BULLET_RE = re.compile(r"^\s*-\s.*`[^`]+`")
# A markdown subheader (e.g. "### Öne Çıkanlar") that may immediately
# precede a highlight arc block. When that block collapses to the
# all-dormant note we drop this orphaned header too, so the voice does
# not read "Öne Çıkanlar … no current developments".
_SUBHEADER_RE = re.compile(r"^\s*#{2,6}\s")

# Spoken note emitted when a block of arc lines is entirely dormant (so
# the voiced version would otherwise be empty). Keeps the voice briefing
# coherent instead of dropping a whole section silently.
ALL_DORMANT_VOICE_NOTE: str = "Bu bölümde bugüne ait güncel gelişme yok."


def _is_arc_recency_line(line: str) -> bool:
    """True only for a deterministic arc bullet carrying a recency suffix."""
    if not _ARC_BULLET_RE.match(line):
        return False
    return bool(_FRESH_LINE_RE.search(line) or _DORMANT_LINE_RE.search(line))


def _drop_dormant_arc_lines(text: str) -> str:
    """Drop DORMANT arc lines from the spoken text; keep FRESH ones.

    Operates on contiguous blocks of deterministic arc bullets (the writer
    renders each itemized section's arcs as an unbroken, freshest-first
    bullet list, so one block == one section's arc list). Within a block:

    * keep the FRESH lines (``bugün`` / ``dün``);
    * drop the DORMANT lines (``N gün önce``);
    * if the block is entirely dormant (no FRESH survivor), replace it with
      :data:`ALL_DORMANT_VOICE_NOTE` — dropping any immediately-preceding
      orphaned subheader (e.g. ``### Öne Çıkanlar``) and never emitting two
      consecutive notes.

    Lines that are not arc bullets (headers, DEFCON 1-2/3 LLM prose, the
    closing line) pass through untouched — even if they happen to end in a
    recency phrase.
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if not _is_arc_recency_line(lines[i]):
            out.append(lines[i])
            i += 1
            continue
        # Consume the maximal contiguous run of arc bullets.
        fresh: list[str] = []
        had_dormant = False
        while i < n and _is_arc_recency_line(lines[i]):
            if _DORMANT_LINE_RE.search(lines[i]):
                had_dormant = True
            else:
                fresh.append(lines[i])
            i += 1
        if fresh:
            out.extend(fresh)
        elif had_dormant:
            _append_all_dormant_note(out)
    return "\n".join(out)


def _append_all_dormant_note(out: list[str]) -> None:
    """Append the all-dormant note, dropping an orphaned preceding subheader
    and collapsing consecutive notes."""
    # Drop trailing blanks then a lone subheader that introduced the block.
    while out and not out[-1].strip():
        out.pop()
    if out and _SUBHEADER_RE.match(out[-1]):
        out.pop()
    # De-duplicate: skip if the last non-blank line is already the note.
    j = len(out) - 1
    while j >= 0 and not out[j].strip():
        j -= 1
    if j >= 0 and out[j] == ALL_DORMANT_VOICE_NOTE:
        return
    out.append(ALL_DORMANT_VOICE_NOTE)


def _rewrite_arc_ids_for_tts(text: str) -> str:
    """Rewrite ``arc_YYYYMMDD_NNNN`` to ``hikaye N`` for TTS."""
    return _ARC_ID_RE.sub(lambda m: f"hikaye {int(m.group(1))}", text)


def preprocess_for_tts(text: str) -> str:
    """Clean ``text`` into a TTS-ready string.

    The order of operations matters; see the regex section above. The
    function never returns an empty string for non-empty input — a
    fully-stripped section becomes a single blank line, which Piper
    handles silently.
    """
    if not text:
        return ""

    # 0. Drop dormant arc lines from the spoken text (keep fresh). Done
    #    first, while line structure + recency suffixes are pristine.
    text = _drop_dormant_arc_lines(text)

    # 1. Drop the visual-only source-attribution lines.
    text = _KAYNAKLAR_RE.sub("", text)

    # 2. Strip markdown formatting (headers, bold, italic, links, code).
    text = _HEADER_RE.sub("", text)
    text = _BOLD_RE.sub(r"\1", text)
    text = _ITALIC_RE.sub(r"\1", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _BACKTICK_RE.sub("", text)
    text = _HRULE_RE.sub("", text)
    text = _ARROW_RE.sub("", text)

    # 3. Rewrite arc IDs to readable "hikaye N" form.
    text = _rewrite_arc_ids_for_tts(text)

    # 4. Expand DEFCON numerals to Turkish words.
    text = _DEFCON_NUM_RE.sub(_defcon_num_repl, text)

    # 5. Expand acronyms (whole-word, case-sensitive — Turkish acronyms
    #    are always uppercase in MÜŞAHİT's pipeline so case-sensitivity
    #    is safe and faster than re-matching mixed case).
    for acronym, expansion in ABBREVIATIONS.items():
        text = re.sub(rf"\b{re.escape(acronym)}\b", expansion, text)

    # 6. Collapse runs of blank lines to a single blank line.
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
    "ALL_DORMANT_VOICE_NOTE",
    "DEFCON_TR_NUMBERS",
    "preprocess_for_tts",
]

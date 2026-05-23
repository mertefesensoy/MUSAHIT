"""Rule-based entity extraction over the curated :data:`VOCABULARY`.

The function :func:`extract_entities` walks every (canonical + alias) form
in :mod:`musahit.normalize.entities_vocab` and returns every match
``{"type": str, "text": str, "span": [start, end]}`` keeping the longest
non-overlapping span at any position. Matching is Turkish-locale aware:
``İ ⇄ i`` and ``I ⇄ ı`` are folded so a feed that drops dots still matches
the canonical form.

The implementation is deliberately simple — a single pass over the
vocabulary, per-document. For our 100-entry vocab × ~500 articles per
night the cost is below 50 ms total; switching to a trie/Aho-Corasick is
premature optimisation. If profiling later disagrees, the swap is local.
"""

from __future__ import annotations

import re

from musahit.normalize.entities_vocab import VOCABULARY, EntityType, VocabEntry

# Turkish-locale lowercase: handle İ → i and I → ı correctly. Python's
# str.lower() turns "İ" into "i̇" (with combining dot) and "I" into
# "i" — both wrong for Turkish matching. We pre-substitute and then call
# .lower() to handle the rest of the alphabet.
_TR_LOWER_PRE = str.maketrans({"İ": "i", "I": "ı"})


def _tr_lower(s: str) -> str:
    return s.translate(_TR_LOWER_PRE).lower()


# Token-boundary character class — Latin + Turkish letters, ASCII digits.
# Anything outside this is a boundary.
_WORD_CHARS = "A-Za-z0-9ÇĞİıÖŞÜçğıöşü"


def _pattern_for_term(term: str) -> re.Pattern[str]:
    """Compile a word-boundary regex for ``term`` against the lowered text."""
    escaped = re.escape(_tr_lower(term))
    return re.compile(rf"(?<![{_WORD_CHARS}]){escaped}(?![{_WORD_CHARS}])")


# Pre-compile every (canonical, type, surface_form) triple once at import.
# Surface forms include the canonical itself plus every alias.
_COMPILED: list[tuple[re.Pattern[str], str, EntityType]] = []


def _build_compiled_table(vocab: tuple[VocabEntry, ...]) -> None:
    _COMPILED.clear()
    for entry in vocab:
        for surface in (entry.canonical, *entry.aliases):
            _COMPILED.append((_pattern_for_term(surface), entry.canonical, entry.type))


_build_compiled_table(VOCABULARY)


def extract_entities(text: str | None) -> list[dict[str, object]]:
    """Return non-overlapping entity matches in ``text``.

    The returned list is sorted by ``span[0]``; overlapping matches are
    resolved by preferring the earliest start, then the longest span.
    Each item is ``{"type": <EntityType.value>, "text": <canonical>,
    "span": [start, end]}``.
    """
    if not text:
        return []

    lowered = _tr_lower(text)
    candidates: list[tuple[int, int, str, EntityType]] = []
    for pattern, canonical, ent_type in _COMPILED:
        for match in pattern.finditer(lowered):
            candidates.append((match.start(), match.end(), canonical, ent_type))

    # Resolve overlaps: sort by start; for any pair of overlapping
    # candidates, keep the longer span (and the first one if equal length).
    candidates.sort(key=lambda c: (c[0], -(c[1] - c[0])))
    kept: list[tuple[int, int, str, EntityType]] = []
    last_end = -1
    for start, end, canonical, ent_type in candidates:
        if start >= last_end:
            kept.append((start, end, canonical, ent_type))
            last_end = end

    return [
        {"type": ent_type.value, "text": canonical, "span": [start, end]}
        for start, end, canonical, ent_type in kept
    ]


__all__ = ["extract_entities"]

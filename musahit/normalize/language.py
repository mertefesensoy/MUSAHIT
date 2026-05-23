"""Language detection wrapper around :mod:`langdetect`.

Used by the Normalizer to populate ``articles.language``. The wrapper:

* Returns ``"unknown"`` for inputs shorter than 20 characters — langdetect's
  probabilistic model is unreliable on very short text and "unknown" is
  honest about that.
* Catches :class:`langdetect.LangDetectException` and returns ``"tr"`` as
  the fallback. The corpus is overwhelmingly Turkish; defaulting to ``tr``
  is the cheapest right answer when detection fails.
* Calls :func:`langdetect.DetectorFactory.seed` once at import time so
  language detection is deterministic across runs (the operator should
  see the same language assignment for the same body).
"""

from __future__ import annotations

from langdetect import DetectorFactory, LangDetectException, detect

DetectorFactory.seed = 0

MIN_DETECTABLE_LENGTH: int = 20
DEFAULT_LANGUAGE: str = "tr"
UNKNOWN: str = "unknown"


def detect_language(text: str | None) -> str:
    """Return an ISO 639-1 code, ``"unknown"``, or the Turkish fallback.

    Returns:
        ``"unknown"`` when text is empty or too short for reliable
        detection; the detected code (e.g. ``"tr"``, ``"en"``) on success;
        ``"tr"`` when langdetect raises (mojibake, all-symbols, etc.).
    """
    if not text:
        return UNKNOWN
    if len(text.strip()) < MIN_DETECTABLE_LENGTH:
        return UNKNOWN
    try:
        return detect(text)
    except LangDetectException:
        return DEFAULT_LANGUAGE


__all__ = ["DEFAULT_LANGUAGE", "MIN_DETECTABLE_LENGTH", "UNKNOWN", "detect_language"]

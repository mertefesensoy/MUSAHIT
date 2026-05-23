"""Pydantic models for parsing the worker LLM's JSON output.

The classifier asks Qwen2.5 7B to return a strict JSON object matching
:class:`WorkerResponse`. Validation runs on every reply; a
:class:`pydantic.ValidationError` triggers the classifier's retry loop.
After the configured ``max_retries`` the classifier falls back to a
canned, conservative response (``category=UNCLASSIFIED``,
``defcon=AMBIENT``, ``confidence_self="low"``) so the pipeline never
stalls on a single misbehaving LLM call.

Turkish-character folding for ``category``: LLMs trained primarily on
English data tend to drop Turkish diacritics in their JSON outputs
(``DIPLOMASİ`` instead of ``DİPLOMASİ``, ``POLITIKA`` instead of
``POLİTİKA``). Before-mode field validator folds the input through a
Turkish-locale-aware lowercase + ASCII-diacritic map and looks the
result up in :data:`_CATEGORY_NORMALIZATION_MAP`. Match → canonical
value; no match → input passes through unchanged and pydantic's enum
coercion raises (which fires the classifier retry loop). The bug was
surfaced by the 2026-05-23 smoke run · see
``docs/implementations/2026-05-24-category-normalization.md``.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from musahit.common.types import Category

WorkerConfidence = Literal["high", "medium", "low"]


# ── Turkish-locale folding helpers ─────────────────────────────────────────

# Per memory/MEMORY.md § "Turkish locale case folding": default
# str.lower() / str.casefold() mishandle Turkish I / İ. Pre-translate
# before lowercasing.
_TR_LOWER_PRE = str.maketrans({"İ": "i", "I": "ı"})

# Post-lowercase ASCII fold for matching against Turkish diacritic
# variants. Keys are lowercase only because :func:`_fold_for_matching`
# runs the Turkish-aware lowercase first.
_ASCII_FOLD = str.maketrans(
    {
        "ı": "i",
        "ş": "s",
        "ğ": "g",
        "ü": "u",
        "ö": "o",
        "ç": "c",
    }
)


def _tr_lower(s: str) -> str:
    """Lowercase ``s`` under Turkish locale rules (see MEMORY.md)."""
    return s.translate(_TR_LOWER_PRE).lower()


def _fold_for_matching(s: str) -> str:
    """Turkish-aware lowercase + ASCII fold · case- and diacritic-insensitive key."""
    return _tr_lower(s).translate(_ASCII_FOLD)


# Build the fold map programmatically so a future Category addition picks
# up the normalization automatically. Keys are folded forms; values are
# the canonical enum values exactly as written in
# :class:`musahit.common.types.Category`.
_CATEGORY_NORMALIZATION_MAP: dict[str, str] = {
    _fold_for_matching(c.value): c.value for c in Category
}

# Defence in depth: if two Category values ever fold to the same key the
# map silently shadows one of them. Surface that at import time so the
# next reader (or the next /goal spec adding an enum member) sees the
# conflict immediately.
if len(_CATEGORY_NORMALIZATION_MAP) != len(list(Category)):
    _collisions = {}
    for c in Category:
        _collisions.setdefault(_fold_for_matching(c.value), []).append(c.value)
    _conflicts = {k: v for k, v in _collisions.items() if len(v) > 1}
    raise RuntimeError(
        "Category fold collision · two enum values share a normalization "
        f"key: {_conflicts}. Resolve by choosing distinct values or revising "
        "_fold_for_matching."
    )


def _normalize_category(value: str) -> str:
    """Return the canonical :class:`Category` value for ``value`` if its
    fold matches one · otherwise return ``value`` unchanged so pydantic's
    enum coercion fires and the classifier retries.

    Pure function · no I/O · safe to call from a Pydantic validator.
    """
    if not value:
        return value
    return _CATEGORY_NORMALIZATION_MAP.get(_fold_for_matching(value), value)


# ── Pydantic model ─────────────────────────────────────────────────────────


class WorkerResponse(BaseModel):
    """The shape Qwen2.5 7B is instructed to return.

    Bounds and types are enforced by pydantic. The classifier never
    consumes a partially-validated object · either the parse succeeds
    fully or the response is treated as malformed.
    """

    defcon: int = Field(ge=0, le=5)
    category: Category
    confidence_self: WorkerConfidence
    entities: list[str] = Field(default_factory=list)
    summary: str = Field(default="", max_length=500)
    headline: str = Field(default="", max_length=200)

    @field_validator("category", mode="before")
    @classmethod
    def _normalize_category_value(cls, v: object) -> object:
        """Fold Turkish-diacritic-dropped LLM outputs back to canonical.

        Runs BEFORE enum coercion (``mode="before"``) so a string that
        folds to a valid :class:`Category` value reaches enum coercion
        as the canonical form. Non-string inputs (e.g. an already-coerced
        enum instance from internal callers) are returned unchanged.
        """
        if isinstance(v, str):
            return _normalize_category(v)
        return v


def parse_worker_response(raw: str) -> WorkerResponse:
    """Parse a raw LLM completion into a :class:`WorkerResponse`.

    The wrapper exists so callers can ``try/except`` on a single
    exception type · :class:`pydantic.ValidationError` · and not have to
    branch on JSON-decoding vs schema-validation. We also strip common
    LLM artifacts (triple-backtick ``json`` code fences, leading/trailing prose)
    so a model that wraps its JSON in markdown still parses.
    """
    text = raw.strip()
    if text.startswith("```"):
        # Strip leading ``` and trailing ``` fences with optional language tag.
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
    text = text.strip()
    # Some models still embed prose around the JSON object · locate the
    # outermost { ... } and try just that span.
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    try:
        data = json.loads(text)
    except ValueError as exc:
        # Re-raise as ValidationError so callers have one type to catch.
        raise ValidationError.from_exception_data(
            "WorkerResponse",
            [{"type": "json_invalid", "loc": (), "input": raw, "ctx": {"error": str(exc)}}],
        ) from exc
    return WorkerResponse.model_validate(data)


__all__ = [
    "WorkerConfidence",
    "WorkerResponse",
    "parse_worker_response",
]

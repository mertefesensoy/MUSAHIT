"""Unit tests for :mod:`musahit.score.schema`.

The bulk of coverage here is :class:`TestCategoryNormalization`, which
locks in the Turkish-diacritic-folding behaviour added on 2026-05-24
after the first real smoke run revealed that Qwen2.5 returns category
strings with Latin I instead of Turkish İ
(``"DIPLOMASİ"`` instead of ``"DİPLOMASİ"``). Without folding,
pydantic's enum coercion rejects those values · the classifier retry
loop fires for max_retries · the cluster ends up with the conservative
``UNCLASSIFIED`` / ``AMBIENT`` / ``low`` fallback.

The retry-on-malformed-JSON behaviour stays · the validator only
rescues fold-equivalent variants; truly bad input still raises
``ValidationError``.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from musahit.common.types import Category
from musahit.score.schema import (
    _CATEGORY_NORMALIZATION_MAP,
    WorkerResponse,
    _normalize_category,
    parse_worker_response,
)

# ── Shared helper ──────────────────────────────────────────────────────────


def _worker_payload(**overrides: object) -> dict[str, object]:
    """Minimum-valid WorkerResponse payload with optional overrides."""
    payload: dict[str, object] = {
        "defcon": 3,
        "category": "POLİTİKA",
        "confidence_self": "medium",
    }
    payload.update(overrides)
    return payload


# ── TestCategoryNormalization ──────────────────────────────────────────────


class TestCategoryNormalization:
    """The fold validator handles diacritic-dropped LLM outputs without
    breaking truly-malformed inputs."""

    # ── Canonical pass-through ─────────────────────────────────────────

    @pytest.mark.parametrize("value", [c.value for c in Category])
    def test_all_canonical_values_pass_through_unchanged(
        self, value: str
    ) -> None:
        """Every Category value survives the validator as itself."""
        parsed = WorkerResponse.model_validate(_worker_payload(category=value))
        assert parsed.category.value == value

    # ── The observed bug ───────────────────────────────────────────────

    def test_diplomasi_with_latin_i_folds_to_canonical(self) -> None:
        """The exact 2026-05-23 smoke-run case: ``DIPLOMASİ`` → ``DİPLOMASİ``."""
        parsed = WorkerResponse.model_validate(
            _worker_payload(category="DIPLOMASİ")
        )
        assert parsed.category is Category.DIPLOMASI
        assert parsed.category.value == "DİPLOMASİ"

    # ── Full ASCII folds ───────────────────────────────────────────────

    @pytest.mark.parametrize(
        ("ascii_input", "expected"),
        [
            ("POLITIKA", Category.POLITIKA),
            ("EKONOMI", Category.EKONOMI),
            ("DIPLOMASI", Category.DIPLOMASI),
            ("GUVENLIK", Category.GUVENLIK),
        ],
    )
    def test_full_ascii_fold_normalizes_to_canonical(
        self, ascii_input: str, expected: Category
    ) -> None:
        """An LLM that drops EVERY Turkish diacritic still parses."""
        parsed = WorkerResponse.model_validate(
            _worker_payload(category=ascii_input)
        )
        assert parsed.category is expected

    # ── Lowercase folds ────────────────────────────────────────────────

    @pytest.mark.parametrize(
        ("lower_input", "expected"),
        [
            ("politika", Category.POLITIKA),
            ("dıplomasi", Category.DIPLOMASI),  # dotless ı after İ→i lower
            ("guvenlik", Category.GUVENLIK),
            ("ekonomi", Category.EKONOMI),
            ("yargi", Category.YARGI),
        ],
    )
    def test_lowercase_folds_normalize_to_canonical(
        self, lower_input: str, expected: Category
    ) -> None:
        """Lowercase LLM outputs (some Qwen variants do this) also normalize."""
        parsed = WorkerResponse.model_validate(
            _worker_payload(category=lower_input)
        )
        assert parsed.category is expected

    # ── Negative cases · validator must NOT swallow real errors ────────

    def test_unknown_category_raises_validation_error(self) -> None:
        """No fold match → input passes through unchanged → enum coercion
        fails · classifier retries (this is the contract the rest of the
        score stage relies on)."""
        with pytest.raises(ValidationError):
            WorkerResponse.model_validate(
                _worker_payload(category="NOT_A_REAL_CATEGORY")
            )

    def test_empty_string_raises_validation_error(self) -> None:
        """Empty category is treated as malformed."""
        with pytest.raises(ValidationError):
            WorkerResponse.model_validate(_worker_payload(category=""))

    def test_partial_fold_collisions_still_raise(self) -> None:
        """A near-miss (e.g. an extra letter) should NOT silently snap
        to a Category · only exact fold matches qualify."""
        for bad in ("POLITIKAA", "DIPLOMAS", "GUVENL", "GUVENLIKK"):
            with pytest.raises(ValidationError):
                WorkerResponse.model_validate(_worker_payload(category=bad))

    # ── Fold map invariants ────────────────────────────────────────────

    def test_fold_map_has_no_collisions(self) -> None:
        """Each Category value folds to a distinct key · this is also
        asserted at import time in ``schema.py``, but pinning it here
        makes a future enum addition surface the conflict in the
        test report rather than the import traceback."""
        assert len(_CATEGORY_NORMALIZATION_MAP) == len(list(Category))

    def test_normalize_returns_canonical_for_known_folds(self) -> None:
        """The standalone helper is the single source of truth used by
        the validator · tested directly so a refactor that splits the
        validator off the model still has coverage."""
        assert _normalize_category("DIPLOMASİ") == "DİPLOMASİ"
        assert _normalize_category("POLITIKA") == "POLİTİKA"
        assert _normalize_category("politika") == "POLİTİKA"

    def test_normalize_passes_unknown_through_unchanged(self) -> None:
        """Helper contract: unknown input returns as-is so pydantic enum
        coercion fires and the classifier retry loop runs."""
        assert _normalize_category("UNKNOWN") == "UNKNOWN"
        assert _normalize_category("") == ""


# ── TestParseWorkerResponse ────────────────────────────────────────────────
#
# Light regression on parse_worker_response · the dash-stripping +
# fenced-JSON handling exists separately from the fold validator, and
# both must keep working together.


class TestParseWorkerResponseWithFolding:
    def test_json_with_folded_category_parses(self) -> None:
        raw = json.dumps(_worker_payload(category="DIPLOMASİ"))
        parsed = parse_worker_response(raw)
        assert parsed.category is Category.DIPLOMASI

    def test_fenced_json_with_folded_category_parses(self) -> None:
        raw = "```json\n" + json.dumps(_worker_payload(category="POLITIKA")) + "\n```"
        parsed = parse_worker_response(raw)
        assert parsed.category is Category.POLITIKA

    def test_malformed_json_still_raises_validation_error(self) -> None:
        """The validator MUST NOT swallow malformed JSON · classifier
        retry depends on ValidationError firing."""
        with pytest.raises(ValidationError):
            parse_worker_response("{not a json at all")

"""Tests for musahit.cluster.centroid — pure cosine + centroid arithmetic."""

from __future__ import annotations

import math

import pytest

from musahit.cluster.centroid import compute_centroid, cosine_similarity


class TestCosineSimilarity:
    def test_identical_vectors_return_one(self) -> None:
        a = [1.0, 2.0, 3.0]
        assert cosine_similarity(a, a) == pytest.approx(1.0)

    def test_orthogonal_vectors_return_zero(self) -> None:
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        assert cosine_similarity(a, b) == pytest.approx(0.0)

    def test_opposite_vectors_return_negative_one(self) -> None:
        a = [1.0, 2.0, 3.0]
        b = [-1.0, -2.0, -3.0]
        assert cosine_similarity(a, b) == pytest.approx(-1.0)

    def test_zero_vector_returns_zero_safely(self) -> None:
        a = [0.0, 0.0, 0.0]
        b = [1.0, 1.0, 1.0]
        # Convention: zero norm → zero similarity (no ZeroDivisionError).
        assert cosine_similarity(a, b) == 0.0
        assert cosine_similarity(b, a) == 0.0

    def test_known_angle_45_degrees(self) -> None:
        # (1, 0) vs (1, 1) — 45° angle, cosine = 1/sqrt(2).
        sim = cosine_similarity([1.0, 0.0], [1.0, 1.0])
        assert sim == pytest.approx(1.0 / math.sqrt(2))

    def test_dimension_mismatch_raises(self) -> None:
        with pytest.raises(ValueError):
            cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0])


class TestComputeCentroid:
    def test_centroid_of_one_vector_is_itself(self) -> None:
        v = [1.0, 2.0, 3.0]
        assert compute_centroid([v]) == v

    def test_centroid_is_arithmetic_mean(self) -> None:
        vectors = [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ]
        expected = [1 / 3, 1 / 3, 1 / 3]
        result = compute_centroid(vectors)
        assert len(result) == 3
        for r, e in zip(result, expected, strict=True):
            assert r == pytest.approx(e)

    def test_empty_input_returns_empty(self) -> None:
        assert compute_centroid([]) == []

    def test_dimension_mismatch_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_centroid([[1.0, 2.0], [1.0, 2.0, 3.0]])

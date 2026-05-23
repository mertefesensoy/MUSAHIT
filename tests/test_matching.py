"""Tests for musahit.arcs.matching — pure cosine + Jaccard filters."""

from __future__ import annotations

import pytest

from musahit.arcs.matching import (
    CandidateArc,
    find_candidate_arcs,
    jaccard,
    match_arc,
)


class TestJaccard:
    def test_identical_sets(self) -> None:
        assert jaccard({"a", "b", "c"}, {"a", "b", "c"}) == pytest.approx(1.0)

    def test_disjoint_sets(self) -> None:
        assert jaccard({"a", "b"}, {"c", "d"}) == pytest.approx(0.0)

    def test_partial_overlap(self) -> None:
        # |∩| = 1, |∪| = 3 → 1/3
        assert jaccard({"a", "b"}, {"a", "c"}) == pytest.approx(1 / 3)

    def test_one_empty(self) -> None:
        assert jaccard(set(), {"a", "b"}) == pytest.approx(0.0)

    def test_both_empty(self) -> None:
        # By convention we return 0.0 (no signal).
        assert jaccard(set(), set()) == pytest.approx(0.0)

    def test_frozenset_inputs_also_work(self) -> None:
        assert jaccard(frozenset({"a"}), frozenset({"a", "b"})) == pytest.approx(0.5)


# ── find_candidate_arcs ────────────────────────────────────────────────────


def _arc(arc_id: str, vec: list[float], entities: set[str]) -> CandidateArc:
    return CandidateArc(arc_id=arc_id, centroid=vec, entity_set=frozenset(entities))


class TestFindCandidateArcs:
    def test_filters_by_cosine_threshold(self) -> None:
        target = [1.0, 0.0]
        arcs = [
            _arc("arc_high", [1.0, 0.0], {"x"}),       # cos=1.0
            _arc("arc_mid",  [0.6, 0.8], {"x"}),       # cos=0.6
            _arc("arc_low",  [0.0, 1.0], {"x"}),       # cos=0.0
        ]
        result = find_candidate_arcs(target, arcs, threshold=0.55)
        ids = [arc_id for arc_id, _ in result]
        # arc_low (0.0) is below the 0.55 threshold and is dropped.
        assert "arc_high" in ids
        assert "arc_mid" in ids
        assert "arc_low" not in ids

    def test_sorted_by_cosine_descending(self) -> None:
        target = [1.0, 0.0]
        arcs = [
            _arc("mid", [0.6, 0.8], {"x"}),
            _arc("high", [1.0, 0.0], {"x"}),
            _arc("nearmid", [0.7, 0.7], {"x"}),
        ]
        result = find_candidate_arcs(target, arcs, threshold=0.55)
        ids = [arc_id for arc_id, _ in result]
        scores = [score for _, score in result]
        assert ids[0] == "high"
        assert scores == sorted(scores, reverse=True)

    def test_empty_arc_list_returns_empty(self) -> None:
        assert find_candidate_arcs([1.0, 0.0], [], threshold=0.55) == []


# ── match_arc ──────────────────────────────────────────────────────────────


class TestMatchArc:
    def test_returns_top_cosine_when_jaccard_passes(self) -> None:
        target = [1.0, 0.0]
        entities = {"İmamoğlu", "İstanbul Büyükşehir"}
        arcs = [
            _arc("top", [1.0, 0.0], {"İmamoğlu", "İstanbul Büyükşehir", "Yargıtay"}),
            _arc("mid", [0.6, 0.8], {"İmamoğlu", "İstanbul Büyükşehir"}),
        ]
        result = match_arc(
            target, entities, arcs,
            cosine_threshold=0.55, jaccard_threshold=0.4,
        )
        assert result == "top"

    def test_skips_when_jaccard_below_threshold(self) -> None:
        target = [1.0, 0.0]
        entities = {"Bahçeli", "MHP"}
        arcs = [
            # Centroid clears 0.55 but entity overlap is 0.
            _arc("centroid_only", [1.0, 0.0], {"Erdoğan", "AKP"}),
        ]
        result = match_arc(
            target, entities, arcs,
            cosine_threshold=0.55, jaccard_threshold=0.4,
        )
        assert result is None

    def test_falls_back_to_lower_cosine_if_top_fails_jaccard(self) -> None:
        target = [1.0, 0.0]
        entities = {"İmamoğlu", "İstanbul Büyükşehir"}
        arcs = [
            # Top cosine, no entity overlap.
            _arc("top_cosine_no_jaccard", [1.0, 0.0], {"Erdoğan", "AKP"}),
            # Lower cosine, perfect entity overlap.
            _arc("mid_cosine_full_jaccard", [0.6, 0.8],
                 {"İmamoğlu", "İstanbul Büyükşehir"}),
        ]
        result = match_arc(
            target, entities, arcs,
            cosine_threshold=0.55, jaccard_threshold=0.4,
        )
        assert result == "mid_cosine_full_jaccard"

    def test_returns_none_when_no_arc_clears_cosine(self) -> None:
        target = [1.0, 0.0]
        entities = {"x"}
        arcs = [_arc("low", [0.1, 0.9], {"x"})]  # cosine=0.1 below 0.55
        assert match_arc(
            target, entities, arcs,
            cosine_threshold=0.55, jaccard_threshold=0.4,
        ) is None

    def test_empty_entities_returns_none(self) -> None:
        # ADR-008 explicitly skips clusters with no usable entities.
        target = [1.0, 0.0]
        arcs = [_arc("any", [1.0, 0.0], {"a", "b"})]
        assert match_arc(
            target, set(), arcs,
            cosine_threshold=0.55, jaccard_threshold=0.4,
        ) is None

    def test_empty_arc_list_returns_none(self) -> None:
        assert match_arc(
            [1.0, 0.0], {"a"}, [],
            cosine_threshold=0.55, jaccard_threshold=0.4,
        ) is None

"""Pure matching functions for the arc-linking stage.

Per ADR-008 a cluster matches an arc when **both** filters pass:

* Cosine similarity between cluster centroid and arc centroid ≥ 0.55
* Jaccard overlap of stopword-filtered entity sets ≥ 0.4

The combined predicate is the conservative "both signals agree" rule:
either signal alone misfires often enough (centroid drift for ongoing
stories; spurious entity matches via Turkish naming overlap) that we
require corroboration before linking.

This module is **pure** — no DB, no time, no I/O. The orchestrator
hands in pre-loaded arc records; the functions return data only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from musahit.cluster.centroid import cosine_similarity

DEFAULT_COSINE_THRESHOLD: float = 0.55
DEFAULT_JACCARD_THRESHOLD: float = 0.4


@dataclass(frozen=True)
class CandidateArc:
    """In-memory snapshot of an arc the linker is considering as a match."""

    arc_id: str
    centroid: list[float]
    entity_set: frozenset[str]


# ── Set math ───────────────────────────────────────────────────────────────


def jaccard(a: set[str] | frozenset[str], b: set[str] | frozenset[str]) -> float:
    """Standard Jaccard index ``|a ∩ b| / |a ∪ b|``.

    Returns ``0.0`` when both sets are empty (the alternative —
    undefined — is unhelpful for the caller, and an empty ∪ never
    clears the threshold anyway).
    """
    if not a and not b:
        return 0.0
    intersection = a & b
    union = a | b
    if not union:
        return 0.0
    return len(intersection) / len(union)


# ── Candidate filtering ────────────────────────────────────────────────────


def find_candidate_arcs(
    centroid: Sequence[float],
    arcs: Sequence[CandidateArc],
    threshold: float = DEFAULT_COSINE_THRESHOLD,
) -> list[tuple[str, float]]:
    """Return ``[(arc_id, cosine)]`` for arcs whose centroid clears ``threshold``.

    Sorted by cosine descending so the caller can scan the highest-
    similarity first.
    """
    scored: list[tuple[str, float]] = []
    for arc in arcs:
        sim = cosine_similarity(centroid, arc.centroid)
        if sim >= threshold:
            scored.append((arc.arc_id, sim))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def match_arc(
    centroid: Sequence[float],
    entities: set[str] | frozenset[str],
    arcs: Sequence[CandidateArc],
    cosine_threshold: float = DEFAULT_COSINE_THRESHOLD,
    jaccard_threshold: float = DEFAULT_JACCARD_THRESHOLD,
) -> str | None:
    """Return the highest-cosine arc whose Jaccard also clears the threshold.

    ``entities`` is the cluster's stopword-filtered entity set. An empty
    set returns ``None`` immediately — without entities there is no
    Jaccard signal to validate the centroid match, and ADR-008
    explicitly skips such clusters at the matching step.
    """
    if not entities:
        return None
    by_id = {arc.arc_id: arc for arc in arcs}
    for arc_id, _cosine in find_candidate_arcs(centroid, arcs, cosine_threshold):
        candidate = by_id[arc_id]
        if jaccard(entities, candidate.entity_set) >= jaccard_threshold:
            return arc_id
    return None


__all__ = [
    "DEFAULT_COSINE_THRESHOLD",
    "DEFAULT_JACCARD_THRESHOLD",
    "CandidateArc",
    "find_candidate_arcs",
    "jaccard",
    "match_arc",
]

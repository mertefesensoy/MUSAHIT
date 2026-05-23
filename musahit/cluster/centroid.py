"""Pure cosine + centroid arithmetic for 1024-dim vectors.

No numpy dependency — at our volume (≤ 500 vectors per night, 1024
dimensions) the pure-Python implementation is well under 10ms per
operation and avoids pulling in a ~25 MB native dependency for a
single-machine project. If profiling later disagrees, swapping to numpy
is a localised change.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

Vector = Sequence[float]


def cosine_similarity(a: Vector, b: Vector) -> float:
    """Cosine similarity between two equally-sized vectors.

    Returns ``0.0`` when either vector has zero norm (which is the
    degenerate case for an empty token bag — well-defined and avoids a
    ZeroDivisionError without surprising the caller).
    """
    if len(a) != len(b):
        raise ValueError(
            f"cosine_similarity: dimension mismatch {len(a)} vs {len(b)}"
        )
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def compute_centroid(vectors: Sequence[Vector]) -> list[float]:
    """Arithmetic mean across ``vectors`` — the standard cluster centroid.

    All vectors must have the same dimension. Empty input returns an
    empty list (callers should guard against this; computing a centroid
    of zero members is a programming error, not a data condition).
    """
    if not vectors:
        return []
    dimension = len(vectors[0])
    sums = [0.0] * dimension
    count = 0
    for v in vectors:
        if len(v) != dimension:
            raise ValueError(
                f"compute_centroid: dimension mismatch {len(v)} vs {dimension}"
            )
        for i, val in enumerate(v):
            sums[i] += val
        count += 1
    return [s / count for s in sums]


__all__ = ["Vector", "compute_centroid", "cosine_similarity"]

# ============================================================================
# FILE-PROTECTED · musahit/score/promotion.py
# Modifications require an ADR amendment + explicit operator override.
# See BOOTSTRAP.md § File protection list and ADR-005.
# ============================================================================
"""Promotion-ceiling rules and final-DEFCON computation per ADR-005.

The flow is deterministic — no LLM in the loop here:

1. The cluster's :data:`bands_present` (a :class:`set` of :class:`Band`)
   plus its :data:`num_sources` integer come in.
2. :func:`compute_ceiling` returns the most-severe DEFCON the cluster's
   band coverage *permits*.
3. :func:`ideological_sides` and :func:`confidence` produce the audit
   metadata for ``promotion_log`` and the briefing display tag.
4. :func:`final_defcon` combines the worker's ``raw_defcon`` with the
   ceiling via the ADR-005 formula ``min(raw, ceiling)``.
5. :func:`bootstrap_demoted` reads ``pipeline_runs`` to check whether
   the cluster falls inside the system's 7-day bootstrap window
   (BOOTSTRAP.md / ADR-004). If yes, the caller bumps ``final`` by one
   tier toward AMBIENT.

Numeric direction note: in our :class:`DEFCON` IntEnum lower numbers
mean MORE severe. The ADR-005 formula is :func:`min`. See the cluster
implementation doc for the directional gotcha and tracking issue.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import duckdb

from musahit.common.types import Band, Confidence
from musahit.score.defcon import DEFCON

BOOTSTRAP_DAYS: int = 7


# ── Constants (ADR-005) ────────────────────────────────────────────────────


PRIMARY_BANDS: frozenset[Band] = frozenset(
    {Band.PRIMARY_GOV, Band.PRIMARY_MARKET, Band.PRIMARY_JUDICIAL}
)

SOCIAL_BANDS: frozenset[Band] = frozenset({Band.SOCIAL_X, Band.SOCIAL_REDDIT})

IDEOLOGICAL_SIDES: dict[str, frozenset[Band]] = {
    "gov": frozenset({Band.GOV_ALIGNED}),
    "opposition": frozenset({Band.OPPOSITION}),
    "neutral": frozenset(
        {Band.CENTRIST, Band.INDEPENDENT, Band.INTERNATIONAL}
    ),
}


# ── Pure rule functions ────────────────────────────────────────────────────


def ideological_sides(bands: set[Band] | frozenset[Band]) -> set[str]:
    """Return the set of ideological side names represented in ``bands``.

    A band can belong to at most one side (per the disjoint partition in
    :data:`IDEOLOGICAL_SIDES`). Primary and social bands belong to no
    ideological side; they are tracked separately.
    """
    return {
        side for side, members in IDEOLOGICAL_SIDES.items() if bands & members
    }


def compute_ceiling(bands: set[Band] | frozenset[Band]) -> tuple[DEFCON, str]:
    """Return ``(ceiling, rule_name)``.

    ``rule_name`` is the branch of the function that fired — written to
    ``promotion_log.rule_applied`` for audit.
    """
    if any(b in PRIMARY_BANDS for b in bands):
        return DEFCON.UNTHINKABLE, "primary"

    non_social = set(bands) - SOCIAL_BANDS
    if not non_social:
        return DEFCON.ROUTINE, "social_only"

    sides = ideological_sides(bands)
    if len(non_social) >= 3 and len(sides) >= 2:
        return DEFCON.ACUTE, "three_bands_two_sides"
    if len(non_social) >= 2:
        return DEFCON.MATERIAL, "two_bands"
    return DEFCON.ROUTINE, "single_band"


def confidence(
    bands: set[Band] | frozenset[Band], num_sources: int
) -> Confidence:
    """Return the :class:`Confidence` tag per ADR-005."""
    if any(b in PRIMARY_BANDS for b in bands):
        return Confidence.YUKSEK
    sides = ideological_sides(bands)
    if len(sides) >= 2 and num_sources >= 4:
        return Confidence.YUKSEK
    if len(sides) >= 2 or num_sources >= 3:
        return Confidence.ORTA
    return Confidence.DUSUK


def final_defcon(raw: DEFCON | int, ceiling: DEFCON | int) -> DEFCON:
    """``DEFCON(min(raw, ceiling))`` per ADR-005.

    Both operands are coerced through ``int(...)`` so a caller may pass
    raw integers (e.g. a worker response's ``.defcon`` field) without
    constructing :class:`DEFCON` first.
    """
    return DEFCON(min(int(raw), int(ceiling)))


# ── Bootstrap demotion ─────────────────────────────────────────────────────


def bootstrap_demoted(
    cluster_created_at: datetime,
    conn: duckdb.DuckDBPyConnection,
    bootstrap_days: int = BOOTSTRAP_DAYS,
) -> bool:
    """``True`` if the cluster falls inside the 7-day bootstrap window.

    "First run" = the earliest ``pipeline_runs.started_at``. If the table
    is empty (no run on record), there's no bootstrap reference — return
    ``False`` so the caller doesn't demote.
    """
    row = conn.execute(
        "SELECT MIN(started_at) FROM pipeline_runs"
    ).fetchone()
    if row is None or row[0] is None:
        return False
    first_started = row[0]
    return (cluster_created_at - first_started) < timedelta(days=bootstrap_days)


def apply_bootstrap_demotion(final: DEFCON | int) -> DEFCON:
    """Bump ``final`` by one tier toward AMBIENT (capped).

    Demotion in our numeric convention = increase the number by 1, up to
    :attr:`DEFCON.AMBIENT`. The pipeline applies this on every cluster
    inside the bootstrap window per ADR-004.
    """
    return DEFCON(min(int(final) + 1, int(DEFCON.AMBIENT)))


__all__ = [
    "BOOTSTRAP_DAYS",
    "IDEOLOGICAL_SIDES",
    "PRIMARY_BANDS",
    "SOCIAL_BANDS",
    "apply_bootstrap_demotion",
    "bootstrap_demoted",
    "compute_ceiling",
    "confidence",
    "final_defcon",
    "ideological_sides",
]

"""Arc freshness axis · the surfacing signal that DEFCON is NOT.

Per the 2026-05-29 Group-A brief, DEFCON encodes **severity** (a serious
story stays serious regardless of age) and is FILE-PROTECTED in
``musahit/score/defcon.py``. This module owns a separate, orthogonal
**freshness** signal derived purely from an arc's last-update timestamp
(``arcs.last_update_at``). Freshness governs *surfacing* — prominence,
inclusion in the markdown briefing, and whether the line is voiced by
TTS — and never touches DEFCON scoring.

Three surfacing states (driven by calendar-day age):

* ``FRESH``   — a new source arrived within the last ``dormancy_days``
  (locked at 2). Days 0 ("bugün") and 1 ("dün"). Full prominence in
  markdown and voiced by TTS.
* ``DORMANT`` — no new source for ``dormancy_days``..``expire_days``
  (2..7). Days 2..6 ("N gün önce"). Still within lifespan: kept in the
  markdown briefing with a recency marker, but **skipped from the voice
  briefing** (a wall of stale stories read aloud helps no one).
* ``EXPIRED`` — no new source for ``expire_days`` (default 7, tunable).
  Day 7+. Excluded from active sections; the lifecycle pass
  (:mod:`musahit.arcs.transitions`) resolves these so the open-arc
  backlog drains.

The boundaries are inclusive-low / exclusive-high on the lower band and
inclusive on expiry, chosen to match the brief's locked boundary tests
(1d → FRESH, 2d → DORMANT, 6d → DORMANT, 7d → EXPIRED):

    days < dormancy_days           → FRESH
    dormancy_days <= days < expire → DORMANT
    days >= expire_days            → EXPIRED

This module is **pure** — no DB, no I/O, no wall-clock reads. The caller
hands in the two datetimes (typically the project's tz-aware-naive
:func:`musahit.common.time.utcnow` for *now*, and an arc's stored
``last_update_at`` for *last_update*). Day math is calendar-day based
(``date()`` difference), not elapsed-seconds, so "2 days ago at 23:00"
and "2 days ago at 01:00" both read as the same N-day age — the unit the
operator reasons in.

The Turkish recency label (``bugün`` / ``dün`` / ``N gün önce``) is also
defined here as the single source of truth for the suffix the writer
appends to itemized arc lines and that the TTS preprocessor keys off to
decide what to drop from the spoken text.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

# ── Tunable thresholds ─────────────────────────────────────────────────────
#
# DORMANCY_DAYS is LOCKED at 2 per the operator's D3 decision · it is the
# boundary between "fresh enough to voice" and "show-but-don't-voice".
#
# EXPIRE_DAYS is a flagged DEFAULT (tunable). The project had no working
# resolution/aging at all (the 875-open-arc backlog · the transitions FK
# bug meant the cleanup pass crashed every run), so per the brief we set
# auto-resolution at 7 days here and mark it clearly tunable. Raising it
# keeps arcs surfaced (and matchable) longer; lowering it drains faster.

DORMANCY_DAYS: int = 2  # LOCKED · fresh→dormant boundary
EXPIRE_DAYS: int = 7  # TUNABLE DEFAULT · dormant→expired (auto-resolve) boundary


class Freshness(StrEnum):
    """The surfacing state of an arc, derived from its last-update age."""

    FRESH = "FRESH"
    DORMANT = "DORMANT"
    EXPIRED = "EXPIRED"


def days_since_update(
    last_update: datetime | None,
    now: datetime,
) -> int:
    """Whole calendar days between ``last_update`` and ``now``.

    Contract:

    * ``last_update is None`` → ``0``. A legacy arc with no recorded
      last-update (pre-migration-004 backfill, or a freshly seeded arc
      whose timestamp write is pending) is treated as touched *today* —
      the safe, non-stale default. The renderer then shows "bugün".
    * The difference is taken on ``.date()`` so it counts calendar days,
      not 24-hour windows. ``now`` two calendar days after ``last_update``
      returns ``2`` regardless of the clock time on either day.
    * Negative results (an arc whose ``last_update_at`` is in the future
      relative to ``now`` — possible when a join stamps the cluster's
      ``created_at`` slightly ahead of the writer's ``now``) clamp to
      ``0``. There is no such thing as "negative staleness".
    """
    if last_update is None:
        return 0
    delta = (now.date() - last_update.date()).days
    return max(delta, 0)


def freshness_from_days(
    days: int,
    *,
    dormancy_days: int = DORMANCY_DAYS,
    expire_days: int = EXPIRE_DAYS,
) -> Freshness:
    """Classify a precomputed day-count into a :class:`Freshness` state.

    Split out from :func:`arc_freshness` so callers that already hold the
    day-count (the writer payload computes ``days_since_last_update`` once
    against the briefing date) do not recompute it from datetimes.
    """
    if days >= expire_days:
        return Freshness.EXPIRED
    if days >= dormancy_days:
        return Freshness.DORMANT
    return Freshness.FRESH


def arc_freshness(
    last_update: datetime | None,
    now: datetime,
    *,
    dormancy_days: int = DORMANCY_DAYS,
    expire_days: int = EXPIRE_DAYS,
) -> Freshness:
    """Classify an arc's freshness from its last-update timestamp.

    Pure, DB-free. See the module docstring for the boundary semantics.
    """
    return freshness_from_days(
        days_since_update(last_update, now),
        dormancy_days=dormancy_days,
        expire_days=expire_days,
    )


def recency_label(days: int) -> str:
    """Turkish recency suffix for a day-count · the writer/TTS contract.

    * ``0`` (or negative, clamped by the caller) → ``"bugün"``
    * ``1`` → ``"dün"``
    * ``N >= 2`` → ``"N gün önce"``

    This is the single source of truth for the suffix the writer appends
    to itemized arc lines (``… · arc_id · 6 gün önce``). The TTS
    preprocessor keys off the same vocabulary to decide what to drop:
    ``bugün``/``dün`` (FRESH) are voiced; ``N gün önce`` (DORMANT, N≥2 by
    construction — 1 day is always "dün", never "1 gün önce") is dropped
    from the spoken text. Keeping the formats coupled here means the two
    stages can never drift apart.
    """
    if days <= 0:
        return "bugün"
    if days == 1:
        return "dün"
    return f"{days} gün önce"


__all__ = [
    "DORMANCY_DAYS",
    "EXPIRE_DAYS",
    "Freshness",
    "arc_freshness",
    "days_since_update",
    "freshness_from_days",
    "recency_label",
]

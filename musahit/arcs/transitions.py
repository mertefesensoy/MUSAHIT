"""Arc lifecycle auto-resolution pass.

Per ADR-008 the lifecycle states are ``OPEN`` / ``WATCH`` / ``RESOLVED``.
This module owns the automatic aging transition that drains the
open-arc backlog: any ``OPEN`` or ``WATCH`` arc that has had **no new
source for ``expire_days``** (default 7, tunable · see
:mod:`musahit.arcs.freshness`) is auto-resolved to ``RESOLVED`` so it
leaves the active set. ``RESOLVED`` is terminal — never auto-transitioned
back (reverse transitions on a fresh link / operator override are the
linker's and dashboard's job).

Why this replaced the old OPEN→WATCH(7d) + WATCH→RESOLVED(30d) ladder
(2026-05-29 Group-A):

* The old ladder took 37 days to retire a dead arc, which (combined with
  the FK crash below) is why 875 arcs piled up in OPEN. The brief's
  freshness model resolves at the 7-day expiry boundary instead, so the
  lifecycle (OPEN→RESOLVED at expiry) now mirrors the freshness axis
  (FRESH <2d, DORMANT 2-6d, EXPIRED ≥7d). DORMANT arcs (2-6d) stay OPEN
  so the writer keeps surfacing them with a recency marker; EXPIRED arcs
  (≥7d) resolve and drain. The aging threshold is the single
  ``EXPIRE_DAYS`` constant in :mod:`musahit.arcs.freshness`.
* The one-time backlog drain is intentionally *silent* — it does NOT
  bump ``last_update_at`` (preserving each arc's true recency) and does
  NOT flood ``KAPATILAN HİKAYELER`` with hundreds of entries. The writer
  surfaces operator-resolved / same-day-resolved arcs in KAPATILAN via
  its own ``last_update_at >= run_started`` rule; aged-out arcs simply
  fade from the active set as the open-arc count drops.

── The FK bug this pass fixes ──────────────────────────────────────────

``tests/test_linker.py::TestStopwordOnlyOverlap`` failed with a DuckDB
``ConstraintException`` ("key arc_id: … is still referenced by a foreign
key in a different table") raised from the bulk ``UPDATE arcs SET
state``. Root cause: ``idx_arcs_state`` is an index on the very column
being updated. DuckDB implements an UPDATE that touches an **indexed**
column as delete-then-insert of the row; during that window the incoming
foreign keys from ``arc_centroids`` and ``clusters`` (both
``REFERENCES arcs(id)``) momentarily see a missing parent and the FK
check fires — even though ``arcs.id`` never changes. (Updating a
*non-indexed* arcs column does not trip it; that is why the linker's
``_update_arc`` only needs its delete-children workaround because it also
writes ``state``.)

The pass crashed every run because it ran un-guarded at the end of
``ArcLinker.run``, so no arc ever transitioned and the backlog never
drained. The fix drops ``idx_arcs_state`` for the duration of the single
bulk UPDATE and recreates it in a ``finally`` (so a mid-update failure
can never leave the perf-only index missing). This is a single fast
statement — the right shape for draining hundreds of arcs at once — and
it addresses the root cause directly rather than replicating the
linker's per-row child-snapshot workaround.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import duckdb

from musahit.arcs.freshness import EXPIRE_DAYS
from musahit.common.types import ArcState

# Index whose presence on the updated ``state`` column triggers DuckDB's
# delete+insert FK trip. Defined in migration 001. Dropped and recreated
# around the bulk state UPDATE below.
_ARCS_STATE_INDEX: str = "idx_arcs_state"

# Türkiye Standard Time is UTC+3 year-round (mirrors musahit.common.time's
# convention). The resolution boundary is anchored on the TR-local calendar
# date so it lines up EXACTLY with the writer's freshness classifier, which
# measures days against the TR-local briefing date (payload.build_payload's
# ``target_date``). Without this shift the two stages disagreed for the
# 21:00-24:00 UTC window (00:00-03:00 TR-local · the nightly briefing slot):
# an arc the writer classified EXPIRED could be left OPEN here. See the
# 2026-05-29 arc-freshness review.
_TR_UTC_OFFSET: timedelta = timedelta(hours=3)


def transition_states(
    conn: duckdb.DuckDBPyConnection,
    current_time: datetime,
    *,
    expire_days: int = EXPIRE_DAYS,
) -> dict[str, int]:
    """Auto-resolve expired arcs and return the per-transition counts.

    Any arc in ``OPEN`` or ``WATCH`` whose ``last_update_at`` is at least
    ``expire_days`` **TR-local calendar days** before ``current_time``
    transitions to ``RESOLVED``. Calendar-day math (``CAST(... AS DATE)``)
    — not elapsed seconds — anchored on the TR-local date so the boundary
    lines up exactly with the writer's freshness classifier (which
    measures against the TR-local briefing date): an arc is resolved here
    iff the writer would classify it ``EXPIRED``. ``current_time`` is the
    UTC-naive project clock (``utcnow``); it is shifted to TR-local before
    the date is taken.

    Returns ``{"expired_to_resolved": N}`` where ``N`` is the number of
    arcs resolved. NULL ``last_update_at`` rows are never resolved
    (treated as touched today).
    """
    # Self-heal: if a previous run was hard-killed in the tiny window
    # between the DROP and CREATE below, the perf index would be missing
    # (a normal init_db won't re-add an already-applied migration's index).
    # Ensuring it here, idempotently, means the next arc-link run repairs
    # it. A no-op when the index already exists.
    conn.execute(f"CREATE INDEX IF NOT EXISTS {_ARCS_STATE_INDEX} ON arcs(state)")

    # TR-local calendar-day cutoff: resolve arcs whose last-update date is
    # on or before (TR_today − expire_days). days_since >= expire_days  ⇔
    # last_update.date() <= TR_today − expire_days. ``CAST(last_update_at
    # AS DATE)`` yields the UTC date of the stored (UTC-naive) timestamp,
    # exactly as the writer's _days_between uses last_update_at.date().
    cutoff_date = (current_time + _TR_UTC_OFFSET).date() - timedelta(days=expire_days)

    expired_count = conn.execute(
        """
        SELECT COUNT(*) FROM arcs
         WHERE state IN (?, ?)
           AND last_update_at IS NOT NULL
           AND CAST(last_update_at AS DATE) <= ?
        """,
        [ArcState.OPEN.value, ArcState.WATCH.value, cutoff_date],
    ).fetchone()[0]

    if expired_count > 0:
        # Drop the state index for the bulk UPDATE (see module docstring
        # for why this avoids the FK trip), recreate it no matter what.
        conn.execute(f"DROP INDEX IF EXISTS {_ARCS_STATE_INDEX}")
        try:
            conn.execute(
                """
                UPDATE arcs
                   SET state = ?
                 WHERE state IN (?, ?)
                   AND last_update_at IS NOT NULL
                   AND CAST(last_update_at AS DATE) <= ?
                """,
                [
                    ArcState.RESOLVED.value,
                    ArcState.OPEN.value,
                    ArcState.WATCH.value,
                    cutoff_date,
                ],
            )
        finally:
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS {_ARCS_STATE_INDEX} ON arcs(state)"
            )

    return {"expired_to_resolved": int(expired_count)}


__all__ = [
    "EXPIRE_DAYS",
    "transition_states",
]

"""Arc state-transition cleanup pass.

Per ADR-008 the lifecycle states are:

* ``OPEN``  — active in the last 7 days
* ``WATCH`` — dormant 7–30 days
* ``RESOLVED`` — operator-closed OR 30-day silence

This module owns the automatic transitions:

* OPEN  + ``last_update_at`` > 7 days ago  → WATCH
* WATCH + ``last_update_at`` > 30 days ago → RESOLVED

Reverse transitions (WATCH → OPEN on a fresh link, RESOLVED → OPEN on
operator override) are handled by the linker / dashboard respectively;
RESOLVED is never auto-transitioned by this pass.

The cleanup is run at the end of every ArcLinker.run so the dashboard's
OPEN/WATCH/RESOLVED tabs always reflect the freshest state.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import duckdb

from musahit.common.types import ArcState

OPEN_TO_WATCH_DAYS: int = 7
WATCH_TO_RESOLVED_DAYS: int = 30


def transition_states(
    conn: duckdb.DuckDBPyConnection,
    current_time: datetime,
) -> dict[str, int]:
    """Run the two automatic transitions and return the per-transition counts.

    Returns a dict like ``{"open_to_watch": N, "watch_to_resolved": M}``.
    Each value is the number of rows the corresponding UPDATE affected.
    """
    open_to_watch_cutoff = current_time - timedelta(days=OPEN_TO_WATCH_DAYS)
    watch_to_resolved_cutoff = current_time - timedelta(days=WATCH_TO_RESOLVED_DAYS)

    # Count then UPDATE — counting via SELECT is cheap and matches the
    # contract the caller expects (return per-transition counts).
    open_to_watch_count = conn.execute(
        """
        SELECT COUNT(*) FROM arcs
         WHERE state = ?
           AND last_update_at IS NOT NULL
           AND last_update_at < ?
        """,
        [ArcState.OPEN.value, open_to_watch_cutoff],
    ).fetchone()[0]
    if open_to_watch_count > 0:
        conn.execute(
            """
            UPDATE arcs
               SET state = ?
             WHERE state = ?
               AND last_update_at IS NOT NULL
               AND last_update_at < ?
            """,
            [ArcState.WATCH.value, ArcState.OPEN.value, open_to_watch_cutoff],
        )

    watch_to_resolved_count = conn.execute(
        """
        SELECT COUNT(*) FROM arcs
         WHERE state = ?
           AND last_update_at IS NOT NULL
           AND last_update_at < ?
        """,
        [ArcState.WATCH.value, watch_to_resolved_cutoff],
    ).fetchone()[0]
    if watch_to_resolved_count > 0:
        conn.execute(
            """
            UPDATE arcs
               SET state = ?
             WHERE state = ?
               AND last_update_at IS NOT NULL
               AND last_update_at < ?
            """,
            [ArcState.RESOLVED.value, ArcState.WATCH.value, watch_to_resolved_cutoff],
        )

    return {
        "open_to_watch": int(open_to_watch_count),
        "watch_to_resolved": int(watch_to_resolved_count),
    }


__all__ = [
    "OPEN_TO_WATCH_DAYS",
    "WATCH_TO_RESOLVED_DAYS",
    "transition_states",
]

"""Tests for musahit.arcs.transitions — expiry-based auto-resolution.

Rewritten 2026-05-29 (Group-A) for the freshness lifecycle: the old
OPEN→WATCH(7d) + WATCH→RESOLVED(30d) ladder was replaced by a single
auto-resolution at ``EXPIRE_DAYS`` (no new source for ≥7 calendar days →
RESOLVED), and the DuckDB FK bug in the bulk UPDATE was fixed.

The FK regression (``TestResolveWithReferencingRows``) is the heart of
this file: it reproduces the exact shape that crashed
``test_linker.py::TestStopwordOnlyOverlap`` — an expiring arc that owns
an ``arc_centroids`` row AND is referenced by a ``clusters`` row — and
asserts it resolves without a ``ConstraintException``.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from musahit.arcs.freshness import EXPIRE_DAYS
from musahit.arcs.transitions import transition_states
from musahit.common.migrations import init_db
from musahit.common.types import ArcState

NOW = datetime(2026, 5, 29, 12, 0, 0)


# ── Fixture ────────────────────────────────────────────────────────────────


@pytest.fixture()
def db(tmp_path: Path) -> Generator[duckdb.DuckDBPyConnection, None, None]:
    db_path = tmp_path / "x.duckdb"
    init_db(db_path, load_vss=False)
    conn = duckdb.connect(str(db_path))
    try:
        yield conn
    finally:
        conn.close()


def _insert_arc(
    conn: duckdb.DuckDBPyConnection,
    arc_id: str,
    state: ArcState,
    last_update_at: datetime,
    *,
    with_centroid: bool = False,
) -> None:
    conn.execute(
        """
        INSERT INTO arcs (
            id, created_at, headline, summary, state, last_update_at,
            category, peak_defcon, entity_set
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            arc_id,
            last_update_at - timedelta(days=1),
            "h",
            "s",
            state.value,
            last_update_at,
            "POLİTİKA",
            3,
            json.dumps([]),
        ],
    )
    if with_centroid:
        vec = [0.0] * 1024
        vec[0] = 1.0
        conn.execute(
            "INSERT INTO arc_centroids (arc_id, centroid, updated_at) VALUES (?, ?, ?)",
            [arc_id, vec, last_update_at],
        )


def _insert_cluster_for_arc(
    conn: duckdb.DuckDBPyConnection, cluster_id: str, arc_id: str
) -> None:
    conn.execute(
        """
        INSERT INTO clusters (
            id, created_at, headline, summary, category,
            raw_defcon, ceiling_defcon, final_defcon, confidence,
            bands_present, arc_id, operator_override
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        [
            cluster_id,
            NOW,
            "h",
            "s",
            "POLİTİKA",
            3,
            3,
            3,
            "ORTA",
            json.dumps(["independent"]),
            arc_id,
        ],
    )


def _state_of(conn: duckdb.DuckDBPyConnection, arc_id: str) -> str:
    row = conn.execute("SELECT state FROM arcs WHERE id = ?", [arc_id]).fetchone()
    return row[0]


# ── TestAutoResolve ────────────────────────────────────────────────────────


class TestAutoResolve:
    def test_open_idle_seven_days_resolves(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        _insert_arc(db, "arc_expired", ArcState.OPEN, NOW - timedelta(days=7))
        result = transition_states(db, NOW)
        assert result["expired_to_resolved"] == 1
        assert _state_of(db, "arc_expired") == ArcState.RESOLVED.value

    def test_open_idle_six_days_stays_open(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        # 6 days = DORMANT (within lifespan) · must NOT resolve.
        _insert_arc(db, "arc_dormant", ArcState.OPEN, NOW - timedelta(days=6))
        result = transition_states(db, NOW)
        assert result["expired_to_resolved"] == 0
        assert _state_of(db, "arc_dormant") == ArcState.OPEN.value

    def test_watch_idle_seven_days_resolves(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        # Legacy WATCH arcs also drain at expiry.
        _insert_arc(db, "arc_watch", ArcState.WATCH, NOW - timedelta(days=10))
        result = transition_states(db, NOW)
        assert result["expired_to_resolved"] == 1
        assert _state_of(db, "arc_watch") == ArcState.RESOLVED.value

    def test_fresh_arc_untouched(self, db: duckdb.DuckDBPyConnection) -> None:
        _insert_arc(db, "arc_fresh", ArcState.OPEN, NOW)
        result = transition_states(db, NOW)
        assert result["expired_to_resolved"] == 0
        assert _state_of(db, "arc_fresh") == ArcState.OPEN.value

    def test_null_last_update_never_resolves(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        db.execute(
            "INSERT INTO arcs (id, created_at, headline, summary, state, "
            "last_update_at, category, peak_defcon, entity_set) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            ["arc_null", NOW, "h", "s", ArcState.OPEN.value, "POLİTİKA", 3, "[]"],
        )
        result = transition_states(db, NOW)
        assert result["expired_to_resolved"] == 0
        assert _state_of(db, "arc_null") == ArcState.OPEN.value

    def test_resolved_arc_stays_resolved(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        _insert_arc(db, "arc_done", ArcState.RESOLVED, NOW - timedelta(days=60))
        result = transition_states(db, NOW)
        assert result["expired_to_resolved"] == 0
        assert _state_of(db, "arc_done") == ArcState.RESOLVED.value

    def test_calendar_day_boundary(self, db: duckdb.DuckDBPyConnection) -> None:
        # last_update late on the 7th-day-ago date · calendar-day math means
        # it is exactly EXPIRED (7 days) and resolves, matching the writer's
        # freshness classifier (no elapsed-hours skew).
        seven_days_ago_late = datetime(
            (NOW - timedelta(days=7)).year,
            (NOW - timedelta(days=7)).month,
            (NOW - timedelta(days=7)).day,
            23,
            30,
            0,
        )
        _insert_arc(db, "arc_boundary", ArcState.OPEN, seven_days_ago_late)
        result = transition_states(db, NOW)
        assert result["expired_to_resolved"] == 1
        assert _state_of(db, "arc_boundary") == ArcState.RESOLVED.value


# ── TestBacklogDrain ───────────────────────────────────────────────────────


class TestBacklogDrain:
    def test_mixed_population_only_expired_resolve(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        _insert_arc(db, "fresh", ArcState.OPEN, NOW)
        _insert_arc(db, "dormant", ArcState.OPEN, NOW - timedelta(days=4))
        _insert_arc(db, "exp1", ArcState.OPEN, NOW - timedelta(days=8))
        _insert_arc(db, "exp2", ArcState.OPEN, NOW - timedelta(days=20))
        _insert_arc(db, "exp_watch", ArcState.WATCH, NOW - timedelta(days=15))

        result = transition_states(db, NOW)
        assert result["expired_to_resolved"] == 3
        open_count = db.execute(
            "SELECT COUNT(*) FROM arcs WHERE state = ?", [ArcState.OPEN.value]
        ).fetchone()[0]
        # fresh + dormant remain OPEN; the three expired drained.
        assert open_count == 2

    def test_tunable_expire_days(self, db: duckdb.DuckDBPyConnection) -> None:
        _insert_arc(db, "arc_3d", ArcState.OPEN, NOW - timedelta(days=3))
        # With expire_days=3 the 3-day arc resolves.
        result = transition_states(db, NOW, expire_days=3)
        assert result["expired_to_resolved"] == 1
        assert _state_of(db, "arc_3d") == ArcState.RESOLVED.value


# ── TestTrLocalAnchor · alignment with the writer's TR-local briefing date ──


class TestTrLocalAnchor:
    """The resolution boundary is anchored on the TR-local (UTC+3) calendar
    date, matching the writer's freshness classifier. A run launched late in
    the UTC day (after 21:00 UTC = past TR midnight) must resolve per the TR
    date, not the UTC date · otherwise an arc the writer hides as EXPIRED
    would linger OPEN for ~3h every night (the 2026-05-29 review finding)."""

    def test_late_utc_resolves_per_tr_date(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        # 2026-05-29 22:00 UTC == 2026-05-30 01:00 TR-local.
        ct = datetime(2026, 5, 29, 22, 0, 0)
        # last_update 2026-05-23 → 7 TR-days old (EXPIRED) but only 6 UTC-days.
        _insert_arc(db, "arc_tr", ArcState.OPEN, datetime(2026, 5, 23, 10, 0, 0))
        result = transition_states(db, ct)
        assert result["expired_to_resolved"] == 1
        assert _state_of(db, "arc_tr") == ArcState.RESOLVED.value

    def test_late_utc_keeps_dormant_per_tr_date(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        ct = datetime(2026, 5, 29, 22, 0, 0)  # TR 2026-05-30
        # last_update 2026-05-24 → 6 TR-days (DORMANT) · must NOT resolve.
        _insert_arc(db, "arc_tr2", ArcState.OPEN, datetime(2026, 5, 24, 10, 0, 0))
        result = transition_states(db, ct)
        assert result["expired_to_resolved"] == 0
        assert _state_of(db, "arc_tr2") == ArcState.OPEN.value


# ── TestResolveWithReferencingRows · the FK regression ─────────────────────


class TestResolveWithReferencingRows:
    """Reproduces the bug behind test_linker::TestStopwordOnlyOverlap.

    An expiring arc that owns an ``arc_centroids`` row and is referenced
    by a ``clusters`` row must resolve without a DuckDB FK
    ``ConstraintException``. The naked ``UPDATE arcs SET state`` trips the
    incoming FK check because ``idx_arcs_state`` forces a delete+insert of
    the indexed row; the fix drops the index for the bulk update.
    """

    def test_expired_arc_with_centroid_resolves_no_fk_error(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        _insert_arc(
            db, "arc_c", ArcState.OPEN, NOW - timedelta(days=9), with_centroid=True
        )
        result = transition_states(db, NOW)
        assert result["expired_to_resolved"] == 1
        assert _state_of(db, "arc_c") == ArcState.RESOLVED.value
        # arc_centroids row survives the drop/recreate workaround.
        assert (
            db.execute(
                "SELECT COUNT(*) FROM arc_centroids WHERE arc_id = 'arc_c'"
            ).fetchone()[0]
            == 1
        )

    def test_expired_arc_with_centroid_and_cluster_resolves(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        _insert_arc(
            db, "arc_cc", ArcState.OPEN, NOW - timedelta(days=9), with_centroid=True
        )
        _insert_cluster_for_arc(db, "cl_cc", "arc_cc")
        result = transition_states(db, NOW)
        assert result["expired_to_resolved"] == 1
        assert _state_of(db, "arc_cc") == ArcState.RESOLVED.value
        # The referencing cluster keeps pointing at the (now resolved) arc.
        assert (
            db.execute("SELECT arc_id FROM clusters WHERE id = 'cl_cc'").fetchone()[0]
            == "arc_cc"
        )

    def test_state_index_recreated_after_resolution(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        _insert_arc(
            db, "arc_idx", ArcState.OPEN, NOW - timedelta(days=9), with_centroid=True
        )
        transition_states(db, NOW)
        idx = db.execute(
            "SELECT COUNT(*) FROM duckdb_indexes() WHERE index_name = 'idx_arcs_state'"
        ).fetchone()[0]
        assert idx == 1


# ── TestCounts ─────────────────────────────────────────────────────────────


class TestCounts:
    def test_no_transitions_returns_zero(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        result = transition_states(db, NOW)
        assert result == {"expired_to_resolved": 0}

    def test_constant_default_is_seven(self) -> None:
        assert EXPIRE_DAYS == 7

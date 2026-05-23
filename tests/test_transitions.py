"""Tests for musahit.arcs.transitions — automatic state transitions."""

from __future__ import annotations

import json
from collections.abc import Generator
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from musahit.arcs.transitions import (
    OPEN_TO_WATCH_DAYS,
    WATCH_TO_RESOLVED_DAYS,
    transition_states,
)
from musahit.common.migrations import init_db
from musahit.common.types import ArcState

NOW = datetime(2026, 5, 23, 12, 0, 0)


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


def _state_of(conn: duckdb.DuckDBPyConnection, arc_id: str) -> str:
    row = conn.execute("SELECT state FROM arcs WHERE id = ?", [arc_id]).fetchone()
    return row[0]


# ── TestOpenToWatch ────────────────────────────────────────────────────────


class TestOpenToWatch:
    def test_open_older_than_7_days_transitions(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        _insert_arc(
            db,
            "arc_old_open",
            ArcState.OPEN,
            NOW - timedelta(days=OPEN_TO_WATCH_DAYS + 1),
        )
        result = transition_states(db, NOW)
        assert result["open_to_watch"] == 1
        assert _state_of(db, "arc_old_open") == ArcState.WATCH.value

    def test_open_within_7_days_does_not_transition(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        _insert_arc(
            db,
            "arc_recent_open",
            ArcState.OPEN,
            NOW - timedelta(days=OPEN_TO_WATCH_DAYS - 1),
        )
        result = transition_states(db, NOW)
        assert result["open_to_watch"] == 0
        assert _state_of(db, "arc_recent_open") == ArcState.OPEN.value


# ── TestWatchToResolved ────────────────────────────────────────────────────


class TestWatchToResolved:
    def test_watch_older_than_30_days_transitions(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        _insert_arc(
            db,
            "arc_old_watch",
            ArcState.WATCH,
            NOW - timedelta(days=WATCH_TO_RESOLVED_DAYS + 1),
        )
        result = transition_states(db, NOW)
        assert result["watch_to_resolved"] == 1
        assert _state_of(db, "arc_old_watch") == ArcState.RESOLVED.value

    def test_watch_within_30_days_does_not_transition(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        _insert_arc(
            db,
            "arc_recent_watch",
            ArcState.WATCH,
            NOW - timedelta(days=WATCH_TO_RESOLVED_DAYS - 1),
        )
        result = transition_states(db, NOW)
        assert result["watch_to_resolved"] == 0
        assert _state_of(db, "arc_recent_watch") == ArcState.WATCH.value


# ── TestResolvedNeverAuto ──────────────────────────────────────────────────


class TestResolvedNeverAuto:
    def test_resolved_arcs_stay_resolved(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        _insert_arc(
            db,
            "arc_resolved",
            ArcState.RESOLVED,
            NOW - timedelta(days=60),
        )
        result = transition_states(db, NOW)
        # No counter for resolved→anything.
        assert result.get("resolved_to_open", 0) == 0
        assert _state_of(db, "arc_resolved") == ArcState.RESOLVED.value


# ── TestCounts ─────────────────────────────────────────────────────────────


class TestCounts:
    def test_returns_per_transition_counts(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        _insert_arc(db, "open_a", ArcState.OPEN, NOW - timedelta(days=10))
        _insert_arc(db, "open_b", ArcState.OPEN, NOW - timedelta(days=20))
        _insert_arc(db, "watch_a", ArcState.WATCH, NOW - timedelta(days=40))
        # In-window arcs that should not transition.
        _insert_arc(db, "open_recent", ArcState.OPEN, NOW - timedelta(days=2))

        result = transition_states(db, NOW)
        assert result == {"open_to_watch": 2, "watch_to_resolved": 1}

    def test_no_transitions_returns_zeros(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        result = transition_states(db, NOW)
        assert result == {"open_to_watch": 0, "watch_to_resolved": 0}


class TestConstants:
    def test_open_to_watch_days_is_seven(self) -> None:
        assert OPEN_TO_WATCH_DAYS == 7

    def test_watch_to_resolved_days_is_thirty(self) -> None:
        assert WATCH_TO_RESOLVED_DAYS == 30

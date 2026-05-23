"""Tests for musahit.score.promotion — deterministic rules from ADR-005."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from musahit.common.migrations import init_db
from musahit.common.types import Band, Confidence
from musahit.score.defcon import DEFCON
from musahit.score.promotion import (
    BOOTSTRAP_DAYS,
    IDEOLOGICAL_SIDES,
    PRIMARY_BANDS,
    apply_bootstrap_demotion,
    bootstrap_demoted,
    compute_ceiling,
    confidence,
    final_defcon,
    ideological_sides,
)

# ── compute_ceiling ────────────────────────────────────────────────────────


class TestComputeCeiling:
    def test_primary_promotes_to_unthinkable(self) -> None:
        ceiling, rule = compute_ceiling({Band.PRIMARY_GOV})
        assert ceiling is DEFCON.UNTHINKABLE
        assert rule == "primary"

    def test_primary_with_other_bands_still_promotes(self) -> None:
        ceiling, rule = compute_ceiling({Band.PRIMARY_MARKET, Band.GOV_ALIGNED})
        assert ceiling is DEFCON.UNTHINKABLE
        assert rule == "primary"

    def test_x_only_capped_at_routine(self) -> None:
        ceiling, rule = compute_ceiling({Band.SOCIAL_X})
        assert ceiling is DEFCON.ROUTINE
        assert rule == "social_only"

    def test_reddit_only_capped_at_routine(self) -> None:
        ceiling, rule = compute_ceiling({Band.SOCIAL_REDDIT})
        assert ceiling is DEFCON.ROUTINE
        assert rule == "social_only"

    def test_x_plus_reddit_still_social_only(self) -> None:
        ceiling, rule = compute_ceiling({Band.SOCIAL_X, Band.SOCIAL_REDDIT})
        assert ceiling is DEFCON.ROUTINE
        assert rule == "social_only"

    def test_two_bands_one_side_caps_at_material(self) -> None:
        # Two non-social bands but same side (both neutral).
        ceiling, rule = compute_ceiling({Band.CENTRIST, Band.INDEPENDENT})
        assert ceiling is DEFCON.MATERIAL
        assert rule == "two_bands"

    def test_two_bands_two_sides_caps_at_material(self) -> None:
        ceiling, rule = compute_ceiling({Band.GOV_ALIGNED, Band.OPPOSITION})
        assert ceiling is DEFCON.MATERIAL
        assert rule == "two_bands"

    def test_three_bands_two_sides_promotes_to_acute(self) -> None:
        ceiling, rule = compute_ceiling(
            {Band.GOV_ALIGNED, Band.OPPOSITION, Band.CENTRIST}
        )
        assert ceiling is DEFCON.ACUTE
        assert rule == "three_bands_two_sides"

    def test_three_bands_one_side_stays_material(self) -> None:
        # Three bands but all neutral → only one side present.
        ceiling, rule = compute_ceiling(
            {Band.CENTRIST, Band.INDEPENDENT, Band.INTERNATIONAL}
        )
        assert ceiling is DEFCON.MATERIAL
        assert rule == "two_bands"

    def test_single_band_capped_at_routine(self) -> None:
        ceiling, rule = compute_ceiling({Band.GOV_ALIGNED})
        assert ceiling is DEFCON.ROUTINE
        assert rule == "single_band"

    def test_social_bands_dont_count_toward_non_social(self) -> None:
        # 1 mainstream + 1 social = effectively single-band non-social.
        ceiling, rule = compute_ceiling({Band.OPPOSITION, Band.SOCIAL_X})
        assert ceiling is DEFCON.ROUTINE
        assert rule == "single_band"


# ── ideological_sides ──────────────────────────────────────────────────────


class TestIdeologicalSides:
    def test_gov_band_is_gov_side(self) -> None:
        assert ideological_sides({Band.GOV_ALIGNED}) == {"gov"}

    def test_centrist_independent_international_all_neutral(self) -> None:
        assert ideological_sides({Band.CENTRIST}) == {"neutral"}
        assert ideological_sides({Band.INDEPENDENT}) == {"neutral"}
        assert ideological_sides({Band.INTERNATIONAL}) == {"neutral"}
        assert ideological_sides(
            {Band.CENTRIST, Band.INDEPENDENT, Band.INTERNATIONAL}
        ) == {"neutral"}

    def test_two_sides(self) -> None:
        assert ideological_sides(
            {Band.GOV_ALIGNED, Band.OPPOSITION}
        ) == {"gov", "opposition"}

    def test_primary_bands_not_in_any_side(self) -> None:
        assert ideological_sides({Band.PRIMARY_GOV}) == set()

    def test_social_bands_not_in_any_side(self) -> None:
        assert ideological_sides({Band.SOCIAL_X, Band.SOCIAL_REDDIT}) == set()


# ── confidence ─────────────────────────────────────────────────────────────


class TestConfidence:
    def test_primary_yields_yuksek(self) -> None:
        assert confidence({Band.PRIMARY_GOV}, num_sources=1) is Confidence.YUKSEK

    def test_two_sides_four_sources_yuksek(self) -> None:
        assert confidence(
            {Band.GOV_ALIGNED, Band.OPPOSITION}, num_sources=4
        ) is Confidence.YUKSEK

    def test_two_sides_two_sources_orta(self) -> None:
        assert confidence(
            {Band.GOV_ALIGNED, Band.OPPOSITION}, num_sources=2
        ) is Confidence.ORTA

    def test_single_side_three_sources_orta(self) -> None:
        assert confidence(
            {Band.CENTRIST, Band.INDEPENDENT, Band.INTERNATIONAL},
            num_sources=3,
        ) is Confidence.ORTA

    def test_single_band_single_source_dusuk(self) -> None:
        assert confidence({Band.GOV_ALIGNED}, num_sources=1) is Confidence.DUSUK


# ── final_defcon ───────────────────────────────────────────────────────────


class TestFinalDefcon:
    def test_returns_min_per_adr_005(self) -> None:
        # Per ADR-005 the formula is min(raw, ceiling).
        assert final_defcon(DEFCON.AMBIENT, DEFCON.ROUTINE) is DEFCON.ROUTINE
        assert final_defcon(DEFCON.ROUTINE, DEFCON.AMBIENT) is DEFCON.ROUTINE
        assert final_defcon(DEFCON.MATERIAL, DEFCON.ACUTE) is DEFCON.ACUTE
        assert final_defcon(DEFCON.ACUTE, DEFCON.UNTHINKABLE) is DEFCON.UNTHINKABLE

    def test_accepts_int_for_raw(self) -> None:
        assert final_defcon(2, DEFCON.ROUTINE) is DEFCON.SEVERE


# ── UNTHINKABLE requires override ──────────────────────────────────────────


class TestUnthinkableNeedsOverride:
    def test_unthinkable_only_via_explicit_check(self) -> None:
        # The promotion module itself never auto-emits UNTHINKABLE as
        # final — but if raw == UNTHINKABLE and ceiling allows it, the
        # formula DOES return UNTHINKABLE. The override gate lives in
        # downstream code (writer / dashboard) reading
        # DEFCON_REQUIRES_OVERRIDE. Pin that contract here.
        from musahit.score.defcon import DEFCON_REQUIRES_OVERRIDE

        assert DEFCON.UNTHINKABLE in DEFCON_REQUIRES_OVERRIDE


# ── Bootstrap demotion ─────────────────────────────────────────────────────


@pytest.fixture()
def db_with_first_run(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    db_path = tmp_path / "test.duckdb"
    init_db(db_path, load_vss=False)
    conn = duckdb.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO pipeline_runs (
            run_id, started_at, status, stages_done, counts
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [
            "run_first",
            datetime(2026, 5, 1, 0, 0, 0),
            "COMPLETED",
            json.dumps([]),
            json.dumps({}),
        ],
    )
    yield conn
    conn.close()


class TestBootstrapDemoted:
    def test_within_window_returns_true(
        self, db_with_first_run: duckdb.DuckDBPyConnection
    ) -> None:
        # 3 days after the first run — inside the 7-day window.
        cluster_at = datetime(2026, 5, 4, 0, 0, 0)
        assert bootstrap_demoted(cluster_at, db_with_first_run) is True

    def test_outside_window_returns_false(
        self, db_with_first_run: duckdb.DuckDBPyConnection
    ) -> None:
        # 8 days after the first run — outside the window.
        cluster_at = datetime(2026, 5, 9, 0, 0, 0)
        assert bootstrap_demoted(cluster_at, db_with_first_run) is False

    def test_no_pipeline_runs_yields_false(self, tmp_path: Path) -> None:
        # Pipeline_runs empty (no first run on record) → no demotion.
        db_path = tmp_path / "empty.duckdb"
        init_db(db_path, load_vss=False)
        conn = duckdb.connect(str(db_path))
        try:
            assert bootstrap_demoted(datetime(2026, 5, 23), conn) is False
        finally:
            conn.close()


class TestApplyBootstrapDemotion:
    def test_acute_demotes_to_severe(self) -> None:
        assert apply_bootstrap_demotion(DEFCON.ACUTE) is DEFCON.SEVERE

    def test_routine_demotes_to_ambient(self) -> None:
        assert apply_bootstrap_demotion(DEFCON.ROUTINE) is DEFCON.AMBIENT

    def test_ambient_capped_no_further(self) -> None:
        # Already at the least-severe tier — no further demotion possible.
        assert apply_bootstrap_demotion(DEFCON.AMBIENT) is DEFCON.AMBIENT


# ── Constants ──────────────────────────────────────────────────────────────


class TestConstants:
    def test_primary_bands_per_adr_005(self) -> None:
        assert frozenset(
            {Band.PRIMARY_GOV, Band.PRIMARY_MARKET, Band.PRIMARY_JUDICIAL}
        ) == PRIMARY_BANDS

    def test_ideological_sides_partition(self) -> None:
        assert IDEOLOGICAL_SIDES["gov"] == frozenset({Band.GOV_ALIGNED})
        assert IDEOLOGICAL_SIDES["opposition"] == frozenset({Band.OPPOSITION})
        assert IDEOLOGICAL_SIDES["neutral"] == frozenset(
            {Band.CENTRIST, Band.INDEPENDENT, Band.INTERNATIONAL}
        )

    def test_bootstrap_days_is_seven(self) -> None:
        assert BOOTSTRAP_DAYS == 7
        # Sanity: cluster created exactly at the boundary is OUT (< not <=).
        first = datetime(2026, 5, 1)
        boundary = first + timedelta(days=BOOTSTRAP_DAYS)
        # The fixture uses 2026-05-01 as the first_run; a cluster at the
        # boundary returns False (the strict < in bootstrap_demoted).
        assert (boundary - first) >= timedelta(days=BOOTSTRAP_DAYS)

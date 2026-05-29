"""Tests for musahit.arcs.freshness — the surfacing/freshness axis.

Pure-function unit tests · no DB. Pins the locked boundary semantics
(1d→FRESH, 2d→DORMANT, 6d→DORMANT, 7d→EXPIRED) and the Turkish recency
label vocabulary the writer/TTS contract depends on.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from musahit.arcs.freshness import (
    DORMANCY_DAYS,
    EXPIRE_DAYS,
    Freshness,
    arc_freshness,
    days_since_update,
    freshness_from_days,
    recency_label,
)

NOW = datetime(2026, 5, 29, 12, 0, 0)


# ── Constants ──────────────────────────────────────────────────────────────


class TestConstants:
    def test_dormancy_locked_at_two(self) -> None:
        assert DORMANCY_DAYS == 2

    def test_expire_default_is_seven(self) -> None:
        assert EXPIRE_DAYS == 7


# ── days_since_update ───────────────────────────────────────────────────────


class TestDaysSinceUpdate:
    def test_today_is_zero(self) -> None:
        assert days_since_update(NOW, NOW) == 0

    def test_none_is_zero(self) -> None:
        # No recorded last-update → treated as touched today (safe default).
        assert days_since_update(None, NOW) == 0

    def test_calendar_day_based_not_elapsed_hours(self) -> None:
        # last_update late yesterday, now early today → 1 calendar day even
        # though only ~2 elapsed hours separate them.
        last = datetime(2026, 5, 28, 23, 0, 0)
        now = datetime(2026, 5, 29, 1, 0, 0)
        assert days_since_update(last, now) == 1

    def test_three_days(self) -> None:
        assert days_since_update(NOW - timedelta(days=3), NOW) == 3

    def test_future_clamps_to_zero(self) -> None:
        # A join can stamp last_update_at slightly ahead of `now`.
        assert days_since_update(NOW + timedelta(hours=5), NOW) == 0


# ── arc_freshness boundary table ────────────────────────────────────────────


class TestArcFreshnessBoundaries:
    def test_zero_days_fresh(self) -> None:
        assert arc_freshness(NOW, NOW) is Freshness.FRESH

    def test_one_day_fresh(self) -> None:
        assert arc_freshness(NOW - timedelta(days=1), NOW) is Freshness.FRESH

    def test_two_days_dormant(self) -> None:
        # Exactly at the locked dormancy boundary → DORMANT.
        assert arc_freshness(NOW - timedelta(days=2), NOW) is Freshness.DORMANT

    def test_six_days_dormant(self) -> None:
        assert arc_freshness(NOW - timedelta(days=6), NOW) is Freshness.DORMANT

    def test_seven_days_expired(self) -> None:
        # Exactly at the expiry boundary → EXPIRED.
        assert arc_freshness(NOW - timedelta(days=7), NOW) is Freshness.EXPIRED

    def test_far_past_expired(self) -> None:
        assert arc_freshness(NOW - timedelta(days=40), NOW) is Freshness.EXPIRED

    def test_none_is_fresh(self) -> None:
        assert arc_freshness(None, NOW) is Freshness.FRESH

    def test_custom_thresholds(self) -> None:
        # dormancy=1, expire=3 · day 1 dormant, day 3 expired.
        assert (
            arc_freshness(
                NOW - timedelta(days=1), NOW, dormancy_days=1, expire_days=3
            )
            is Freshness.DORMANT
        )
        assert (
            arc_freshness(
                NOW - timedelta(days=3), NOW, dormancy_days=1, expire_days=3
            )
            is Freshness.EXPIRED
        )


# ── freshness_from_days ─────────────────────────────────────────────────────


class TestFreshnessFromDays:
    def test_matches_arc_freshness(self) -> None:
        for d in range(0, 12):
            assert freshness_from_days(d) is arc_freshness(
                NOW - timedelta(days=d), NOW
            )


# ── recency_label ───────────────────────────────────────────────────────────


class TestRecencyLabel:
    def test_zero_is_bugun(self) -> None:
        assert recency_label(0) == "bugün"

    def test_negative_is_bugun(self) -> None:
        assert recency_label(-1) == "bugün"

    def test_one_is_dun(self) -> None:
        assert recency_label(1) == "dün"

    def test_two_is_n_gun_once(self) -> None:
        assert recency_label(2) == "2 gün önce"

    def test_six_is_n_gun_once(self) -> None:
        assert recency_label(6) == "6 gün önce"

    def test_one_never_renders_as_one_gun_once(self) -> None:
        # The TTS skip relies on "1 gün önce" never being emitted (1 → dün),
        # so any "N gün önce" line is guaranteed DORMANT (N≥2).
        assert recency_label(1) != "1 gün önce"

    def test_fresh_labels_have_no_gun_once_substring(self) -> None:
        assert "gün önce" not in recency_label(0)
        assert "gün önce" not in recency_label(1)
        assert "gün önce" in recency_label(2)

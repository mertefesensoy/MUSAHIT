"""Tests for musahit.common.time — UTC-naive timestamp helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from musahit.common.time import to_utc_naive, utcnow


class TestUtcNow:
    def test_returns_naive_datetime(self) -> None:
        now = utcnow()
        assert isinstance(now, datetime)
        assert now.tzinfo is None

    def test_returns_approximately_current_time(self) -> None:
        # Compare against the wall-clock UTC instant. Within 1 second is a
        # generous tolerance; in practice the gap is microseconds.
        before = datetime.now(UTC).replace(tzinfo=None)
        sampled = utcnow()
        after = datetime.now(UTC).replace(tzinfo=None)
        # sampled must fall inside [before, after] — and the whole window
        # must be <= 1s.
        assert before <= sampled <= after
        assert (after - before) <= timedelta(seconds=1)


class TestToUtcNaive:
    def test_none_passes_through(self) -> None:
        assert to_utc_naive(None) is None

    def test_naive_returned_unchanged(self) -> None:
        # Per the documented convention, naive datetimes are assumed to be
        # in UTC already. The helper must not silently mangle them.
        naive = datetime(2026, 5, 23, 8, 0, 0)
        result = to_utc_naive(naive)
        assert result == naive
        assert result is naive  # identity, not just equality

    def test_aware_utc_strips_tzinfo_only(self) -> None:
        aware = datetime(2026, 5, 23, 8, 0, 0, tzinfo=UTC)
        result = to_utc_naive(aware)
        assert result is not None
        assert result.tzinfo is None
        assert result == datetime(2026, 5, 23, 8, 0, 0)

    def test_aware_non_utc_is_converted_then_stripped(self) -> None:
        # +03:00 (Turkey). 11:00+03:00 == 08:00 UTC.
        plus_three = timezone(timedelta(hours=3))
        aware = datetime(2026, 5, 23, 11, 0, 0, tzinfo=plus_three)
        result = to_utc_naive(aware)
        assert result is not None
        assert result.tzinfo is None
        assert result == datetime(2026, 5, 23, 8, 0, 0)

    def test_aware_negative_offset_is_converted(self) -> None:
        # -05:00 (US Eastern, DST off). 03:00-05:00 == 08:00 UTC.
        minus_five = timezone(timedelta(hours=-5))
        aware = datetime(2026, 5, 23, 3, 0, 0, tzinfo=minus_five)
        result = to_utc_naive(aware)
        assert result is not None
        assert result.tzinfo is None
        assert result == datetime(2026, 5, 23, 8, 0, 0)

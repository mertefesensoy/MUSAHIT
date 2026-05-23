"""Timestamp helpers — the project-wide UTC-naive convention.

MÜŞAHİT is a single-machine pipeline (ADR-001) that stores every timestamp in
DuckDB. DuckDB's ``TIMESTAMP`` column is **timezone-naive**: writing a
timezone-aware Python datetime causes a silent conversion to the host's local
time before storage. On the operator's UTC+3 machine that means an 08:00 UTC
publish time is persisted as 11:00 — and reading it back gives a tz-naive
datetime that downstream code can no longer tell apart from an honest UTC
value. This was caught during step-4 RSS work and is the reason both
``raw_articles.fetched_at`` and ``raw_articles.canonical_timestamp`` had to
strip ``tzinfo`` before insert.

This module is the single source of that conversion. Every component that
writes a timestamp to DuckDB — every ingester (steps 4-7), the cluster stage
(step 9), the arc-link stage (step 10), the briefing renderer (step 13) —
imports from here. The convention:

- :func:`utcnow` is the only way to ask "what time is it now?" The result is
  a naive datetime whose value is the current UTC moment.
- :func:`to_utc_naive` normalizes a possibly-aware datetime (e.g. one parsed
  from an RSS feed) into the same naive-UTC shape before persisting it.

If a future stage needs timezone-aware datetimes (UI layer, audit exports,
etc.) it should re-attach ``UTC`` at the boundary — but **never** write a
tz-aware datetime back to DuckDB.

See ADR-001 (architecture overview, single-machine local pipeline).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

# Türkiye Standard Time is UTC+3 year-round. DST was abolished in 2016 and
# has not been reintroduced as of this writing. The offset is hard-coded
# here so callers do not have to deal with zoneinfo databases on Windows.
_TR_UTC_OFFSET: timedelta = timedelta(hours=3)


def utcnow() -> datetime:
    """Return the current UTC moment as a naive ``datetime``.

    Equivalent to ``datetime.now(UTC).replace(tzinfo=None)`` — wrapped here
    so callers do not have to remember the two-step idiom (and so a future
    change of clock source — e.g. a monotonic-injection point for tests —
    has exactly one place to land).
    """
    return datetime.now(UTC).replace(tzinfo=None)


def to_utc_naive(dt: datetime | None) -> datetime | None:
    """Normalize ``dt`` to a naive UTC datetime, preserving ``None``.

    Behavior matrix:

    - ``None`` → ``None``. Callers can pass through optional values without
      a guard.
    - Naive input → returned unchanged. The convention assumes naive
      datetimes are already in UTC; the helper does not second-guess the
      caller's bookkeeping (this matches how :class:`datetime` itself
      treats naive values — interpretation is the caller's job).
    - Tz-aware input → converted to UTC, then ``tzinfo`` stripped. The
      stored instant is the same; only the representation changes.

    Args:
        dt: A naive or tz-aware datetime, or ``None``.

    Returns:
        ``None`` if input was ``None``, otherwise a naive datetime whose
        value is the same UTC instant as the input.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(UTC).replace(tzinfo=None)


def tr_local_date() -> date:
    """Return today's date in Türkiye local time.

    Türkiye is UTC+3 year-round (no DST since 2016) so the conversion is a
    fixed three-hour shift on top of :func:`utcnow`. Used by ingesters
    whose source publishes on a Türkiye-local calendar (currently Resmî
    Gazete; Reddit relies on it via the rolling-window logic).

    Centralising the shift here means a future Türkiye DST decision is a
    one-line edit to this module rather than a hunt-and-fix across every
    ingester.
    """
    return (datetime.now(UTC) + _TR_UTC_OFFSET).date()


__all__ = ["to_utc_naive", "tr_local_date", "utcnow"]

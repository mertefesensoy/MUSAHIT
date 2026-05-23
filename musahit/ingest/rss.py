"""RSS/Atom feed ingester.

Per ADR-003 the ingest stage uses :mod:`feedparser` to parse feed bytes and
:mod:`httpx` to fetch them — feedparser's own ``urllib`` fetcher would block
the event loop, ignore ``rate_limit_seconds``, and surface only a small subset
of the failures we want to record. Per ADR-006 (as amended by ADR-014 and
ADR-015) every entry lands in ``raw_articles`` keyed by a deterministic id
with the universal metadata stored in typed columns; ingester-specific
metadata stays in the loose ``headers`` JSON column. Per ADR-012 the ``fetch``
method NEVER raises for expected failures; it returns a structured
:class:`IngestResult` so a single broken source does not abort the run.

The canonical article id is computed by :func:`musahit.common.ids.article_id`
— see ADR-014 for the formula and the rationale.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import duckdb
import feedparser
import httpx

from musahit.common.ids import article_id
from musahit.common.logging import get_logger
from musahit.common.time import to_utc_naive, utcnow
from musahit.common.types import IngestStatus
from musahit.ingest import USER_AGENT, IngestResult
from musahit.ingest.sources import Source

_log = get_logger("musahit.ingest.rss")

DEFAULT_TIMEOUT_SECONDS: float = 30.0


# ── Pure helpers ────────────────────────────────────────────────────────────


def _entry_url(entry: Any) -> str | None:
    """Return the entry's canonical URL or ``None`` if absent.

    feedparser exposes the entry link as ``entry.link`` for both RSS 2.0
    (``<link>``) and Atom (``<link href="…"/>``). Entries without a link
    cannot be normalized later and are skipped by the caller.
    """
    link = entry.get("link")
    return link or None


def _entry_feed_id(entry: Any) -> str | None:
    """Return the feed-provided identifier used for intra-fetch dedup.

    Tries ``id`` (Atom and RSS 2.0 ``<guid>``), then ``guid``, falling back
    to ``link``. ``None`` only if none of the three is present, in which case
    the caller treats the entry as un-identifiable and skips it.
    """
    return entry.get("id") or entry.get("guid") or entry.get("link")


def _canonical_timestamp(entry: Any) -> datetime | None:
    """``datetime`` of the earlier of ``published`` and ``updated`` (naive UTC).

    Some feeds emit only one of the two; some emit both with ``updated`` set
    to a stale value. We prefer the earlier timestamp because it represents
    the moment the article first became visible, which is what arc-linking
    in ADR-008 cares about. Returns ``None`` if neither field parses.

    The DuckDB tz-naive convention (see ``musahit.common.time``) is enforced
    via :func:`to_utc_naive` so the stored value matches the source feed
    regardless of the host's local timezone.
    """
    candidates: list[datetime] = []
    for field in ("published_parsed", "updated_parsed"):
        parsed = entry.get(field)
        if not parsed:
            continue
        try:
            candidates.append(datetime(*parsed[:6], tzinfo=UTC))
        except (TypeError, ValueError):
            continue
    if not candidates:
        return None
    return to_utc_naive(min(candidates))


# ── Ingester ────────────────────────────────────────────────────────────────


class RssIngester:
    """Default :class:`~musahit.ingest.Ingester` for ``SourceKind.RSS``.

    The ingester is *stateless* with respect to feed contents — every call
    to :meth:`fetch` re-reads the source URL. It holds two long-lived
    collaborators: a DuckDB connection (write target) and, optionally, an
    :class:`httpx.AsyncClient` (tests inject one with ``MockTransport``;
    production callers may omit it to get a fresh client per call).
    """

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._conn = conn
        self._client = client
        self._timeout_seconds = timeout_seconds

    async def fetch(self, source: Source) -> IngestResult:
        """Fetch ``source``, persist entries to ``raw_articles``, return outcome.

        The method never raises for expected failures — see module docstring.
        Unexpected exceptions during DB persistence (e.g. disk full) still
        propagate; per ADR-012 those are run-level concerns handled by the
        orchestrator's outer except.
        """
        if self._client is not None:
            return await self._fetch_with(self._client, source)
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=self._timeout_seconds,
        ) as client:
            return await self._fetch_with(client, source)

    async def _fetch_with(
        self, client: httpx.AsyncClient, source: Source
    ) -> IngestResult:
        log = _log.bind(source_id=source.id, url=source.url)

        try:
            response = await client.get(
                source.url,
                headers={"User-Agent": USER_AGENT},
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            log.warning("rss_timeout", error=str(exc))
            return IngestResult(
                status=IngestStatus.TIMEOUT,
                error=f"timeout: {exc!s}",
            )
        except httpx.HTTPError as exc:
            log.warning("rss_http_error", error=str(exc))
            return IngestResult(
                status=IngestStatus.HTTP_ERROR,
                error=f"{type(exc).__name__}: {exc}",
            )

        if response.status_code >= 400:
            log.warning("rss_bad_status", status=response.status_code)
            return IngestResult(
                status=IngestStatus.HTTP_ERROR,
                error=f"HTTP {response.status_code}",
            )

        feed = feedparser.parse(response.content)
        entries: list[Any] = list(getattr(feed, "entries", None) or [])
        bozo = bool(getattr(feed, "bozo", False))

        if bozo and not entries:
            bozo_exc = getattr(feed, "bozo_exception", None)
            log.warning("rss_parse_error", error=str(bozo_exc))
            return IngestResult(
                status=IngestStatus.PARSE_ERROR,
                error=f"feedparser bozo: {bozo_exc}",
            )

        # Naive UTC per musahit.common.time — DuckDB TIMESTAMP is tz-naive
        # and would silently shift tz-aware datetimes to local time.
        fetched_at = utcnow()
        inserted = self._persist(source, response, entries, fetched_at)
        log.info("rss_ok", inserted=inserted, total_entries=len(entries))
        return IngestResult(status=IngestStatus.OK, count=inserted)

    # ── Persistence ─────────────────────────────────────────────────────

    def _persist(
        self,
        source: Source,
        response: httpx.Response,
        entries: list[Any],
        fetched_at: datetime,
    ) -> int:
        """Dedup, project, and INSERT-OR-IGNORE entries into ``raw_articles``.

        Two dedup layers run here:
          1. Intra-fetch: a ``seen`` set on the feed-provided entry id drops
             duplicate ``<item>`` blocks within the same response.
          2. Inter-fetch: ``ON CONFLICT (id) DO NOTHING`` on the deterministic
             article id (per ADR-014) drops entries already present from a
             prior fetch.

        Returns the number of *new* rows actually written (computed as the
        delta in ``COUNT(*)``). This is what the caller surfaces as
        :attr:`IngestResult.count`.

        Per ADR-015 the universal metadata (``feed_entry_id``,
        ``canonical_timestamp``) is written to typed columns; RSS-specific
        metadata stays in the ``headers`` JSON column.
        """
        seen_entry_ids: set[str] = set()
        rows: list[tuple[Any, ...]] = []

        for entry in entries:
            url = _entry_url(entry)
            if not url:
                continue

            feed_entry_id = _entry_feed_id(entry) or url
            if feed_entry_id in seen_entry_ids:
                continue
            seen_entry_ids.add(feed_entry_id)

            row_id = article_id(source.id, url)
            canonical_ts = _canonical_timestamp(entry)
            # Body extraction for the normalize stage: prefer content:encoded
            # (feedparser exposes as entry.content list), then description,
            # then summary. The normalize RSS extractor runs trafilatura on
            # this value when it detects HTML markup.
            entry_content = entry.get("content") or []
            content_body = ""
            if entry_content:
                first = entry_content[0]
                content_body = first.get("value", "") if isinstance(first, dict) else ""
            body = (
                content_body
                or entry.get("description", "")
                or entry.get("summary", "")
                or ""
            )
            ingester_metadata = {
                # RSS-specific metadata — per ADR-015 examples.
                "title": entry.get("title"),
                "summary": entry.get("summary"),
                "body": body,
                "author": entry.get("author"),
                # Raw feed timestamp strings, kept for audit/debug; the
                # canonical value lives in the typed column.
                "published": entry.get("published"),
                "updated": entry.get("updated"),
                # HTTP cache validators for conditional re-fetches later.
                "etag": response.headers.get("etag"),
                "last_modified": response.headers.get("last-modified"),
            }
            rows.append(
                (
                    row_id,
                    source.id,
                    url,
                    fetched_at,
                    bytes(response.content),
                    response.headers.get("content-type"),
                    json.dumps(ingester_metadata, ensure_ascii=False),
                    response.status_code,
                    feed_entry_id,
                    canonical_ts,
                )
            )

        if not rows:
            return 0

        before = self._row_count()
        self._conn.executemany(
            """
            INSERT INTO raw_articles (
                id, source_id, url, fetched_at,
                raw_content, content_type, headers, fetch_status_code,
                feed_entry_id, canonical_timestamp
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO NOTHING
            """,
            rows,
        )
        return self._row_count() - before

    def _row_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM raw_articles").fetchone()
        return int(row[0]) if row else 0


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "RssIngester",
]

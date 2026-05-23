"""Resmî Gazete PDF ingester.

The Gazette is structurally unlike RSS or HTML: one PDF file per day expands
into many ``raw_articles`` rows — one per parsed item (law, decree,
regulation, communiqué, appointment, court decision). The Ingester Protocol
shape is preserved; the internals are PDF retrieval + delegation to the
pure :mod:`musahit.ingest.gazette_parsing` module.

URL construction (deterministic from the publication date):

* ``https://www.resmigazete.gov.tr/eskiler/YYYY/MM/YYYYMMDD.pdf`` for the
  main edition.
* ``…YYYYMMDD-1.pdf``, ``…YYYYMMDD-2.pdf``, … for the "Mükerrer" supplement
  editions. The ingester probes these in sequence and stops at the first
  404.

Date fallback: the Gazette is posted late evening Türkiye time for the
*next day's* dated edition. Running the pipeline at 01:00 TRT typically
finds today's URL already live; if not (delay, weekend, holiday) the
ingester falls back to yesterday's URL. Both 404s → ``HTTP_ERROR``.

The synthetic URL written into ``raw_articles.url``:
``resmi-gazete://YYYY-MM-DD/<ITEM_TYPE>/<reference_or_synthetic_id>`` —
the article_id formula (ADR-014) treats this exactly like any other URL
string, so cross-fetch dedup works without changes. The real HTTP URL is
preserved in ``headers.real_pdf_url`` for operator traceability.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import duckdb
import httpx

from musahit.common.ids import article_id
from musahit.common.logging import get_logger
from musahit.common.time import to_utc_naive, tr_local_date, utcnow
from musahit.common.types import IngestStatus
from musahit.ingest import USER_AGENT, IngestResult
from musahit.ingest.gazette_parsing import GazetteItem, parse_gazette_pdf
from musahit.ingest.sources import Source

_log = get_logger("musahit.ingest.resmi_gazete")

DEFAULT_TIMEOUT_SECONDS: float = 60.0  # PDF fetches are larger than HTML/RSS.
DEFAULT_MAX_MUKERRER: int = 5

ParsePdfFn = Callable[[bytes, date], list[GazetteItem]]
SleepFn = Callable[[float], Awaitable[None]]


def _build_pdf_url(target_date: date, mukerrer: int = 0) -> str:
    """Construct the canonical PDF URL for ``target_date`` and optional supplement.

    ``mukerrer=0`` → main edition (``…YYYYMMDD.pdf``).
    ``mukerrer>=1`` → numbered supplement (``…YYYYMMDD-1.pdf``, ``-2.pdf``, …).
    """
    yyyymmdd = target_date.strftime("%Y%m%d")
    suffix = f"-{mukerrer}" if mukerrer > 0 else ""
    return (
        f"https://www.resmigazete.gov.tr/eskiler/"
        f"{target_date.year:04d}/{target_date.month:02d}/{yyyymmdd}{suffix}.pdf"
    )


# ── Ingester ────────────────────────────────────────────────────────────────


class ResmiGazeteIngester:
    """:class:`~musahit.ingest.Ingester` for ``SourceKind.PDF`` (Resmî Gazete).

    Constructor dependencies (matching the html.py DI pattern):

    * ``client`` — pre-built :class:`httpx.AsyncClient`; tests pass one
      wrapping :class:`httpx.MockTransport`.
    * ``parse_pdf`` — callable that takes ``(bytes, date)`` and returns a
      list of :class:`GazetteItem`. Defaults to
      :func:`gazette_parsing.parse_gazette_pdf`; tests inject a mock so
      they don't need a real PDF for every scenario.
    * ``target_date`` — override "today"; defaults to Türkiye-local date.
    * ``max_mukerrer`` — cap on supplement-URL probes; default 5.
    * ``sleep`` — async sleep; injected for future rate-limit work.
    """

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        target_date: date | None = None,
        parse_pdf: ParsePdfFn | None = None,
        max_mukerrer: int = DEFAULT_MAX_MUKERRER,
        sleep: SleepFn | None = None,
    ) -> None:
        self._conn = conn
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._target_date = target_date
        self._parse_pdf: ParsePdfFn = parse_pdf if parse_pdf is not None else parse_gazette_pdf
        self._max_mukerrer = max_mukerrer
        # _sleep is currently unused (no in-source rate limiting) but kept on
        # the constructor for symmetry with html.py and future use.
        self._sleep = sleep

    async def fetch(self, source: Source) -> IngestResult:
        if self._client is not None:
            return await self._fetch_with(self._client, source)
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=self._timeout_seconds,
        ) as client:
            return await self._fetch_with(client, source)

    # ── Main flow ───────────────────────────────────────────────────────

    async def _fetch_with(
        self, client: httpx.AsyncClient, source: Source
    ) -> IngestResult:
        log = _log.bind(source_id=source.id)

        target_date = self._target_date or tr_local_date()
        candidates = [target_date, target_date - timedelta(days=1)]

        main_response: httpx.Response | None = None
        main_date: date | None = None
        last_error: IngestResult | None = None

        for candidate_date in candidates:
            url = _build_pdf_url(candidate_date, mukerrer=0)
            result = await self._http_get(client, url)
            if isinstance(result, IngestResult):
                last_error = result
                log.info(
                    "resmi_gazete_main_miss",
                    date=candidate_date.isoformat(),
                    status=result.status.value,
                )
                continue
            main_response = result
            main_date = candidate_date
            break

        if main_response is None or main_date is None:
            # Both candidates failed. last_error is set (HTTP_ERROR or TIMEOUT).
            return last_error or IngestResult(
                status=IngestStatus.HTTP_ERROR,
                error="all candidate dates returned non-200",
            )

        # Process the main edition. A parse failure here is the source-level
        # outcome — translate to PARSE_ERROR. (Mükerrer parse failures are
        # logged and skipped further down so the main edition's data survives.)
        fetched_at = utcnow()
        before_count = self._row_count()
        try:
            self._process_pdf(
                source,
                main_response,
                main_date,
                mukerrer=0,
                fetched_at=fetched_at,
                log=log,
            )
        except Exception as exc:
            log.warning(
                "resmi_gazete_main_parse_error",
                error=f"{type(exc).__name__}: {exc}",
            )
            return IngestResult(
                status=IngestStatus.PARSE_ERROR,
                error=f"{type(exc).__name__}: {exc}",
            )

        # Probe for Mükerrer supplements on the SAME date as the main edition.
        for mukerrer_n in range(1, self._max_mukerrer + 1):
            url = _build_pdf_url(main_date, mukerrer=mukerrer_n)
            result = await self._http_get(client, url)
            if isinstance(result, IngestResult):
                log.info(
                    "resmi_gazete_mukerrer_stop",
                    mukerrer=mukerrer_n,
                    status=result.status.value,
                )
                break
            try:
                self._process_pdf(
                    source,
                    result,
                    main_date,
                    mukerrer=mukerrer_n,
                    fetched_at=fetched_at,
                    log=log,
                )
            except Exception as exc:
                # Supplement parse failure does not unwind the main edition.
                log.warning(
                    "resmi_gazete_mukerrer_parse_error",
                    mukerrer=mukerrer_n,
                    error=f"{type(exc).__name__}: {exc}",
                )

        inserted = self._row_count() - before_count
        log.info("resmi_gazete_ok", inserted=inserted, date=main_date.isoformat())
        return IngestResult(status=IngestStatus.OK, count=inserted)

    # ── PDF processing ──────────────────────────────────────────────────

    def _process_pdf(
        self,
        source: Source,
        response: httpx.Response,
        publication_date: date,
        mukerrer: int,
        fetched_at: datetime,
        log: Any,
    ) -> None:
        """Parse one PDF response and INSERT-OR-IGNORE all its items.

        PDF-level failures (corrupted bytes, pdfplumber exception) propagate
        to the caller — which decides whether to translate them to
        ``PARSE_ERROR`` (main edition) or merely log them (Mükerrer
        supplement). The decision lives one level up; this method stays
        single-purpose.
        """
        items = self._parse_pdf(response.content, publication_date)

        canonical_ts = to_utc_naive(
            datetime(
                publication_date.year,
                publication_date.month,
                publication_date.day,
                tzinfo=UTC,
            )
        )
        real_pdf_url = _build_pdf_url(publication_date, mukerrer=mukerrer)

        for idx, item in enumerate(items, start=1):
            self._persist_item(
                source=source,
                item=item,
                idx=idx,
                publication_date=publication_date,
                mukerrer=mukerrer,
                canonical_ts=canonical_ts,
                fetched_at=fetched_at,
                real_pdf_url=real_pdf_url,
                raw_content=response.content,
            )

    def _persist_item(
        self,
        source: Source,
        item: GazetteItem,
        idx: int,
        publication_date: date,
        mukerrer: int,
        canonical_ts: datetime | None,
        fetched_at: datetime,
        real_pdf_url: str,
        raw_content: bytes,
    ) -> None:
        """Compute identifiers and INSERT-OR-IGNORE one row."""
        synthetic_id = item.reference_number or f"item-{idx}-p{item.page_start}"
        synthetic_url = (
            f"resmi-gazete://{publication_date.isoformat()}/"
            f"{item.item_type.name}/{synthetic_id}"
        )
        row_id = article_id(source.id, synthetic_url)

        ingester_metadata = {
            "section": item.section.name,
            "item_type": item.item_type.name,
            "page_start": item.page_start,
            "page_end": item.page_end,
            "real_pdf_url": real_pdf_url,
            "reference_number": item.reference_number,
            "mukerrer": mukerrer,
            "title": item.title,
            # Body lives in headers JSON rather than re-parsing the PDF in
            # the normalize stage. The normalize PDF extractor reads this
            # directly and applies whitespace normalisation.
            "body": item.body,
        }
        feed_entry_id = item.reference_number or None

        self._conn.execute(
            """
            INSERT INTO raw_articles (
                id, source_id, url, fetched_at,
                raw_content, content_type, headers, fetch_status_code,
                feed_entry_id, canonical_timestamp
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO NOTHING
            """,
            [
                row_id,
                source.id,
                synthetic_url,
                fetched_at,
                bytes(raw_content),
                "application/pdf",
                json.dumps(ingester_metadata, ensure_ascii=False),
                200,
                feed_entry_id,
                canonical_ts,
            ],
        )

    # ── HTTP helpers ────────────────────────────────────────────────────

    async def _http_get(
        self, client: httpx.AsyncClient, url: str
    ) -> httpx.Response | IngestResult:
        """One-shot GET. Returns the response or an early-exit IngestResult.

        Unlike rss/html, a 404 here is *expected* (date probing); the caller
        treats it as "try next candidate" rather than a hard failure.
        """
        try:
            response = await client.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            return IngestResult(
                status=IngestStatus.TIMEOUT,
                error=f"timeout: {exc!s}",
            )
        except httpx.HTTPError as exc:
            return IngestResult(
                status=IngestStatus.HTTP_ERROR,
                error=f"{type(exc).__name__}: {exc}",
            )

        if response.status_code == 404:
            return IngestResult(
                status=IngestStatus.HTTP_ERROR,
                error="HTTP 404",
            )
        if response.status_code >= 400:
            return IngestResult(
                status=IngestStatus.HTTP_ERROR,
                error=f"HTTP {response.status_code}",
            )
        return response

    def _row_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM raw_articles").fetchone()
        return int(row[0]) if row else 0


__all__ = [
    "DEFAULT_MAX_MUKERRER",
    "DEFAULT_TIMEOUT_SECONDS",
    "ResmiGazeteIngester",
]

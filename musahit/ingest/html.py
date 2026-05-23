"""HTML scrape ingester.

Two-phase ingestion: fetch the source's listing page, extract article URLs
using per-source CSS selectors (:mod:`musahit.ingest.html_selectors`), then
fetch and persist each article page. Per ADR-003 ``httpx`` handles HTTP and
:mod:`selectolax` parses the DOM; per ADR-012 individual article failures
are isolated — a single 503 or malformed page does not abort the run.

Failure isolation summary:

* **Listing failure** (timeout, HTTP ≥ 400, parse error) → return early with
  the appropriate :class:`IngestResult` status. Zero per-article fetches.
* **Per-article HTTP failure** (timeout, connection error, HTTP ≥ 400) →
  log a warning, skip that article, continue the loop.
* **Per-article parse failure** (selectolax raises, missing required field) →
  log a warning, skip that article, continue the loop.

``IngestResult.count`` reports the number of *new* rows written to
``raw_articles`` after dedup. Per ADR-015 the universal metadata columns are
populated: ``feed_entry_id`` is always ``NULL`` for HTML sources (no
source-native id), ``canonical_timestamp`` is the output of the four-step
extraction chain (JSON-LD → meta tags → Turkish-formatted date regex →
``fetched_at``), and the headers JSON records which method succeeded.

The article id is computed by :func:`musahit.common.ids.article_id`
(ADR-014); every TIMESTAMP value passes through
:func:`musahit.common.time.to_utc_naive` so DuckDB's tz-naive storage does
not silently shift the value (see ``musahit.common.time``).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Iterator
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import duckdb
import httpx
from selectolax.parser import HTMLParser

from musahit.common.ids import article_id
from musahit.common.logging import get_logger
from musahit.common.time import to_utc_naive, utcnow
from musahit.common.types import IngestStatus
from musahit.ingest import USER_AGENT, IngestResult
from musahit.ingest.html_selectors import SELECTORS, SelectorConfig
from musahit.ingest.sources import Source

_log = get_logger("musahit.ingest.html")

DEFAULT_TIMEOUT_SECONDS: float = 30.0

# Turkish month names → 1-12. Turkish text uses dotted İ / dotless ı,
# Ş, Ç, Ğ, Ü, Ö — be careful with lowercase/uppercase fold.
TURKISH_MONTHS: dict[str, int] = {
    "Ocak": 1,
    "Şubat": 2,
    "Mart": 3,
    "Nisan": 4,
    "Mayıs": 5,
    "Haziran": 6,
    "Temmuz": 7,
    "Ağustos": 8,
    "Eylül": 9,
    "Ekim": 10,
    "Kasım": 11,
    "Aralık": 12,
}

# Meta tags scanned for a published timestamp, in priority order.
# Each tuple is (attribute_name, attribute_value); content is taken from
# the matched <meta>'s ``content`` attribute.
_META_TAG_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("property", "article:published_time"),
    ("property", "article:modified_time"),
    ("property", "og:updated_time"),
    ("name", "datePublished"),
    ("name", "publishdate"),
    ("name", "publish-date"),
    ("name", "date"),
    ("name", "pubdate"),
    ("itemprop", "datePublished"),
    ("itemprop", "dateModified"),
)


# ── Pure helpers ────────────────────────────────────────────────────────────


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string into a ``datetime``; ``None`` on failure.

    Accepts ``Z`` suffix (which :meth:`datetime.fromisoformat` handles
    natively from Python 3.11) and a small fallback that strips ``Z`` and
    appends ``+00:00`` to cover formats that the stdlib parser rejects.
    """
    if not value:
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text)
    except (ValueError, TypeError):
        pass
    if text.endswith("Z"):
        try:
            return datetime.fromisoformat(text[:-1] + "+00:00")
        except (ValueError, TypeError):
            return None
    return None


def _walk_jsonld_for_timestamp(data: Any) -> Iterator[str]:
    """Yield candidate timestamp strings from a JSON-LD payload."""
    if isinstance(data, dict):
        for key in ("datePublished", "dateCreated", "dateModified"):
            value = data.get(key)
            if isinstance(value, str):
                yield value
        for nested in data.values():
            yield from _walk_jsonld_for_timestamp(nested)
    elif isinstance(data, list):
        for item in data:
            yield from _walk_jsonld_for_timestamp(item)


def _try_jsonld(tree: HTMLParser) -> datetime | None:
    """Step 1 of the chain — look in any JSON-LD ``<script>`` block."""
    for node in tree.css("script[type='application/ld+json']"):
        raw = node.text(deep=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        for ts in _walk_jsonld_for_timestamp(data):
            dt = _parse_iso(ts)
            if dt is not None:
                return dt
    return None


def _try_meta(tree: HTMLParser) -> datetime | None:
    """Step 2 of the chain — look at known meta-tag candidates."""
    for attr, value in _META_TAG_CANDIDATES:
        for node in tree.css(f"meta[{attr}='{value}']"):
            content = node.attributes.get("content")
            dt = _parse_iso(content)
            if dt is not None:
                return dt
    return None


def _try_turkish_regex(tree: HTMLParser, scope_selector: str | None) -> datetime | None:
    """Step 3 of the chain — Turkish-formatted date strings in the page text.

    If ``scope_selector`` is set the regex sees only text inside the matched
    element(s); this is a per-source tuning knob (kept inside step 3 rather
    than added as a fifth step — see the build-plan tripwire).
    """
    if scope_selector:
        nodes = tree.css(scope_selector)
        text = " ".join(n.text(deep=True) for n in nodes) if nodes else ""
    else:
        body = tree.body
        text = body.text(deep=True) if body is not None else ""

    name_match = re.search(
        r"(\d{1,2})\s+(Ocak|Şubat|Mart|Nisan|Mayıs|Haziran|"
        r"Temmuz|Ağustos|Eylül|Ekim|Kasım|Aralık)\s+(\d{4})",
        text,
    )
    if name_match:
        day_s, month_name, year_s = name_match.groups()
        try:
            return datetime(int(year_s), TURKISH_MONTHS[month_name], int(day_s))
        except (ValueError, KeyError):
            pass

    numeric_match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if numeric_match:
        day_s, month_s, year_s = numeric_match.groups()
        try:
            return datetime(int(year_s), int(month_s), int(day_s))
        except ValueError:
            return None

    return None


def extract_canonical_timestamp(
    tree: HTMLParser,
    fetched_at: datetime,
    scope_selector: str | None,
) -> tuple[datetime, str]:
    """Run the four-step canonical-timestamp chain.

    Returns ``(timestamp, method_name)``. ``timestamp`` is always a naive
    UTC datetime; ``method_name`` is one of ``"json-ld"``, ``"meta"``,
    ``"turkish-regex"``, ``"fetched-at"`` — surfaced into the row's
    ``headers`` JSON so the operator can audit which fallback fired.
    """
    chain: tuple[tuple[str, Callable[[], datetime | None]], ...] = (
        ("json-ld", lambda: _try_jsonld(tree)),
        ("meta", lambda: _try_meta(tree)),
        ("turkish-regex", lambda: _try_turkish_regex(tree, scope_selector)),
    )
    for method_name, finder in chain:
        raw = finder()
        if raw is None:
            continue
        normalized = to_utc_naive(raw)
        if normalized is None:
            continue
        return normalized, method_name
    return fetched_at, "fetched-at"


# ── Ingester ────────────────────────────────────────────────────────────────

SleepFn = Callable[[float], Awaitable[None]]


class HtmlIngester:
    """Default :class:`~musahit.ingest.Ingester` for ``SourceKind.HTML``.

    Constructor dependencies are explicit so tests can route every request
    through :class:`httpx.MockTransport`, inject a fake ``asyncio.sleep`` to
    assert rate limiting without waiting, and swap the selectors map for a
    test config without mutating module state.
    """

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        selectors: dict[str, SelectorConfig] | None = None,
        sleep: SleepFn | None = None,
    ) -> None:
        self._conn = conn
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._selectors = selectors if selectors is not None else SELECTORS
        self._sleep: SleepFn = sleep if sleep is not None else asyncio.sleep

    async def fetch(self, source: Source) -> IngestResult:
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

        config = self._selectors.get(source.id)
        if config is None:
            log.warning("html_no_selector_config")
            return IngestResult(
                status=IngestStatus.SKIPPED,
                error=f"no SelectorConfig for source_id={source.id}",
            )

        # Phase 1: listing fetch.
        listing = await self._http_get(client, source.url)
        if isinstance(listing, IngestResult):
            return listing

        # Phase 2: extract article URLs.
        try:
            raw_urls = self._extract_listing_urls(
                listing.content, source.url, config
            )
        except Exception as exc:
            log.warning("html_listing_parse_error", error=str(exc))
            return IngestResult(
                status=IngestStatus.PARSE_ERROR,
                error=f"listing parse: {type(exc).__name__}: {exc}",
            )

        # URL dedup before any per-article fetch (preserve first-seen order).
        article_urls = list(dict.fromkeys(raw_urls))
        if not article_urls:
            log.info("html_listing_empty")
            return IngestResult(status=IngestStatus.OK, count=0)

        # Phase 3: per-article fetch + persist with failure isolation.
        fetched_at = utcnow()
        before_count = self._row_count()

        for idx, article_url in enumerate(article_urls):
            if idx > 0 and source.rate_limit_seconds > 0:
                await self._sleep(float(source.rate_limit_seconds))

            try:
                resp = await client.get(
                    article_url,
                    headers={"User-Agent": USER_AGENT},
                    timeout=self._timeout_seconds,
                )
            except httpx.TimeoutException as exc:
                log.warning(
                    "html_article_timeout", url=article_url, error=str(exc)
                )
                continue
            except httpx.HTTPError as exc:
                log.warning(
                    "html_article_http_error",
                    url=article_url,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue

            if resp.status_code >= 400:
                log.warning(
                    "html_article_bad_status",
                    url=article_url,
                    status=resp.status_code,
                )
                continue

            try:
                self._persist_article(
                    source, article_url, resp, fetched_at, config
                )
            except Exception as exc:
                log.warning(
                    "html_article_parse_error",
                    url=article_url,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue

        inserted = self._row_count() - before_count
        log.info(
            "html_ok",
            inserted=inserted,
            listing_urls=len(article_urls),
        )
        return IngestResult(status=IngestStatus.OK, count=inserted)

    # ── HTTP helpers ────────────────────────────────────────────────────

    async def _http_get(
        self, client: httpx.AsyncClient, url: str
    ) -> httpx.Response | IngestResult:
        """One-shot GET that returns either the response or an early-exit result."""
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

        if response.status_code >= 400:
            return IngestResult(
                status=IngestStatus.HTTP_ERROR,
                error=f"HTTP {response.status_code}",
            )
        return response

    # ── Parsing helpers ─────────────────────────────────────────────────

    def _extract_listing_urls(
        self,
        content: bytes,
        base_url: str,
        config: SelectorConfig,
    ) -> list[str]:
        """Scope to ``listing_selector`` and collect ``article_link_selector`` hrefs."""
        tree = HTMLParser(content)
        urls: list[str] = []
        for scope in tree.css(config.listing_selector):
            for link in scope.css(config.article_link_selector):
                href = link.attributes.get("href")
                if href:
                    urls.append(urljoin(base_url, href))
        return urls

    def _extract_title(
        self, tree: HTMLParser, config: SelectorConfig
    ) -> str | None:
        if config.title_selector:
            node = tree.css_first(config.title_selector)
            if node is not None:
                text = (node.text(deep=True) or "").strip()
                if text:
                    return text
        title_node = tree.css_first("title")
        if title_node is not None:
            text = (title_node.text(deep=True) or "").strip()
            return text or None
        return None

    # ── Persistence ─────────────────────────────────────────────────────

    def _persist_article(
        self,
        source: Source,
        url: str,
        response: httpx.Response,
        fetched_at: datetime,
        config: SelectorConfig,
    ) -> None:
        """Parse and INSERT-OR-IGNORE one article into ``raw_articles``.

        Raises if HTML parsing itself fails so the caller can record the
        failure per-article. Successful inserts return ``None``; the caller
        derives the inserted-count from the table's ``COUNT(*)`` delta to
        match the rss.py pattern.
        """
        tree = HTMLParser(response.content)
        canonical_ts, method = extract_canonical_timestamp(
            tree, fetched_at, config.published_selector
        )
        title = self._extract_title(tree, config)

        ingester_metadata = {
            "title": title,
            "canonical_timestamp_method": method,
            "etag": response.headers.get("etag"),
            "last_modified": response.headers.get("last-modified"),
            "selector_listing": config.listing_selector,
            "selector_article_link": config.article_link_selector,
        }

        row_id = article_id(source.id, url)
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
                url,
                fetched_at,
                bytes(response.content),
                response.headers.get("content-type"),
                json.dumps(ingester_metadata, ensure_ascii=False),
                response.status_code,
                None,  # feed_entry_id: NULL for HTML sources per ADR-015 ingester table.
                canonical_ts,
            ],
        )

    def _row_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM raw_articles").fetchone()
        return int(row[0]) if row else 0


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "HtmlIngester",
    "extract_canonical_timestamp",
]

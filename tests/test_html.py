"""Tests for musahit.ingest.html — HTML scrape ingester.

All HTTP calls are mocked through :class:`httpx.MockTransport` — the suite
never touches the network. The DB fixture is the same temp-file DuckDB used
by ``test_rss.py``. Selectors are injected per-test rather than relying on
``SELECTORS`` module state.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Generator
from datetime import datetime
from pathlib import Path

import duckdb
import httpx
import pytest
from selectolax.parser import HTMLParser

from musahit.common.ids import article_id
from musahit.common.migrations import init_db
from musahit.common.types import IngestStatus
from musahit.ingest import IngestResult
from musahit.ingest import html as html_module
from musahit.ingest.html import HtmlIngester, extract_canonical_timestamp
from musahit.ingest.html_selectors import SelectorConfig
from musahit.ingest.sources import get_source, seed_sources

# ── Canned HTML bodies ─────────────────────────────────────────────────────

LISTING_HTML: bytes = b"""<!doctype html>
<html><body>
<nav><a href="/about">About</a></nav>
<main>
<a class="article-link" href="/article/one">Article One</a>
<a class="article-link" href="/article/two">Article Two</a>
<a class="article-link" href="/article/three">Article Three</a>
</main>
<footer><a href="/contact">Contact</a></footer>
</body></html>
"""

# Listing with the same URL appearing twice plus a non-article anchor that
# the scope-and-selector combo must filter out.
LISTING_HTML_WITH_DUPLICATES: bytes = b"""<!doctype html>
<html><body>
<main>
<a class="article-link" href="/article/one">Article One</a>
<a class="article-link" href="/article/one">Article One again</a>
<a class="other" href="/article/should-not-fetch">Not an article</a>
</main>
</body></html>
"""

LISTING_HTML_EMPTY: bytes = b"""<!doctype html>
<html><body><main></main></body></html>"""

# Article with a JSON-LD block carrying datePublished.
ARTICLE_JSONLD: bytes = b"""<!doctype html>
<html><head>
<title>JSON-LD article</title>
<script type="application/ld+json">
{"@type":"NewsArticle","datePublished":"2026-05-23T08:00:00Z"}
</script>
</head><body><h1>JSON-LD article</h1><p>Body.</p></body></html>
"""

# Article with a +03:00 (Turkey) meta-tag timestamp; canonical must be 07:00 UTC.
ARTICLE_META: bytes = b"""<!doctype html>
<html><head>
<title>Meta tag article</title>
<meta property="article:published_time" content="2026-05-23T10:00:00+03:00">
</head><body><h1>Meta tag article</h1><p>Body.</p></body></html>
"""

# Turkish date in plain text — should match the regex step.
ARTICLE_TURKISH_REGEX: bytes = b"""<!doctype html>
<html><head><title>Turkish date article</title></head>
<body><h1>Turkish date article</h1>
<p>Yayim tarihi: 23 May\xc4\xb1s 2026.</p></body></html>
"""

# No JSON-LD, no meta tag, no Turkish date in body → fetched_at fallback.
ARTICLE_NO_TIMESTAMP: bytes = b"""<!doctype html>
<html><head><title>No timestamp</title></head>
<body><h1>No timestamp</h1><p>Plain content.</p></body></html>
"""

# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture()
def ingest_db(tmp_path: Path) -> Generator[duckdb.DuckDBPyConnection, None, None]:
    """Temp-file DuckDB with v1 + v2 schema and the canonical sources seeded."""
    db_path = tmp_path / "test.duckdb"
    init_db(db_path, load_vss=False)
    conn = duckdb.connect(str(db_path))
    seed_sources(conn)
    try:
        yield conn
    finally:
        conn.close()


TEST_SELECTORS: dict[str, SelectorConfig] = {
    "ap_tr": SelectorConfig(
        listing_selector="main",
        article_link_selector="a.article-link",
        title_selector="h1",
    ),
}


def _make_client(responder: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(responder))


def _route_responder(
    routes: dict[str, tuple[bytes, int]],
    calls: list[str] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a responder that dispatches by request URL.

    ``routes`` maps absolute URL strings to ``(body, status)`` tuples.
    ``calls`` (when provided) collects every requested URL, in order, so
    tests can assert request counts and ordering.
    """

    def _responder(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if calls is not None:
            calls.append(url)
        if url in routes:
            body, status = routes[url]
            return httpx.Response(
                status,
                content=body,
                headers={"content-type": "text/html; charset=utf-8"},
            )
        return httpx.Response(404, content=b"unmapped")

    return _responder


def _ap_tr() -> object:
    """Convenience for the source used throughout the suite."""
    return get_source("ap_tr")


def _ingester(
    db: duckdb.DuckDBPyConnection,
    client: httpx.AsyncClient,
    *,
    sleep=None,
    selectors: dict[str, SelectorConfig] | None = None,
) -> HtmlIngester:
    return HtmlIngester(
        conn=db,
        client=client,
        selectors=selectors if selectors is not None else TEST_SELECTORS,
        sleep=sleep,
    )


# ── TestSuccessfulTwoPhase ─────────────────────────────────────────────────


class TestSuccessfulTwoPhase:
    async def test_two_phase_fetch_persists_articles(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = _ap_tr()
        routes = {
            source.url: (LISTING_HTML, 200),
            "https://apnews.com/article/one": (ARTICLE_JSONLD, 200),
            "https://apnews.com/article/two": (ARTICLE_META, 200),
            "https://apnews.com/article/three": (ARTICLE_NO_TIMESTAMP, 200),
        }
        calls: list[str] = []
        client = _make_client(_route_responder(routes, calls))

        async def fake_sleep(_: float) -> None:
            return None

        result = await _ingester(ingest_db, client, sleep=fake_sleep).fetch(source)

        assert result.status is IngestStatus.OK
        assert result.count == 3
        # Listing fetched once + three article fetches.
        assert calls[0] == source.url
        assert set(calls[1:]) == set(routes.keys()) - {source.url}

    async def test_rows_have_expected_columns(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = _ap_tr()
        routes = {
            source.url: (LISTING_HTML, 200),
            "https://apnews.com/article/one": (ARTICLE_JSONLD, 200),
            "https://apnews.com/article/two": (ARTICLE_META, 200),
            "https://apnews.com/article/three": (ARTICLE_NO_TIMESTAMP, 200),
        }

        async def fake_sleep(_: float) -> None:
            return None

        client = _make_client(_route_responder(routes))
        await _ingester(ingest_db, client, sleep=fake_sleep).fetch(source)

        rows = ingest_db.execute(
            """
            SELECT url, feed_entry_id, canonical_timestamp, headers, fetch_status_code
            FROM raw_articles ORDER BY url
            """
        ).fetchall()
        assert len(rows) == 3
        for url, feed_entry_id, canonical_ts, headers_json, status in rows:
            assert url.startswith("https://apnews.com/article/")
            assert feed_entry_id is None  # ADR-015: HTML sources have no native id.
            assert isinstance(canonical_ts, datetime)
            assert status == 200
            meta = json.loads(headers_json)
            assert meta["canonical_timestamp_method"] in {
                "json-ld",
                "meta",
                "turkish-regex",
                "fetched-at",
            }

    async def test_article_ids_match_shared_formula(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = _ap_tr()
        routes = {
            source.url: (LISTING_HTML, 200),
            "https://apnews.com/article/one": (ARTICLE_JSONLD, 200),
            "https://apnews.com/article/two": (ARTICLE_META, 200),
            "https://apnews.com/article/three": (ARTICLE_NO_TIMESTAMP, 200),
        }

        async def fake_sleep(_: float) -> None:
            return None

        client = _make_client(_route_responder(routes))
        await _ingester(ingest_db, client, sleep=fake_sleep).fetch(source)

        ids = {
            r[0] for r in ingest_db.execute("SELECT id FROM raw_articles").fetchall()
        }
        expected = {
            article_id("ap_tr", "https://apnews.com/article/one"),
            article_id("ap_tr", "https://apnews.com/article/two"),
            article_id("ap_tr", "https://apnews.com/article/three"),
        }
        assert ids == expected


# ── TestListingFailure ─────────────────────────────────────────────────────


class TestListingFailure:
    async def test_listing_503_returns_http_error_no_article_fetches(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = _ap_tr()
        calls: list[str] = []
        routes = {source.url: (b"upstream gone", 503)}
        client = _make_client(_route_responder(routes, calls))

        result = await _ingester(ingest_db, client).fetch(source)

        assert result.status is IngestStatus.HTTP_ERROR
        assert result.count == 0
        # Only the listing URL was attempted.
        assert calls == [source.url]
        assert _row_count(ingest_db) == 0


# ── TestPerArticleFailure ──────────────────────────────────────────────────


class TestPerArticleFailure:
    async def test_one_article_503_others_still_succeed(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = _ap_tr()
        routes = {
            source.url: (LISTING_HTML, 200),
            "https://apnews.com/article/one": (ARTICLE_JSONLD, 200),
            "https://apnews.com/article/two": (b"down", 503),
            "https://apnews.com/article/three": (ARTICLE_NO_TIMESTAMP, 200),
        }

        async def fake_sleep(_: float) -> None:
            return None

        client = _make_client(_route_responder(routes))
        result = await _ingester(ingest_db, client, sleep=fake_sleep).fetch(source)

        assert result.status is IngestStatus.OK
        assert result.count == 2  # Article 2 failed; 1 and 3 succeeded.
        assert _row_count(ingest_db) == 2


class TestPerArticleParseError:
    async def test_parse_error_on_one_article_others_processed(
        self,
        ingest_db: duckdb.DuckDBPyConnection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        source = _ap_tr()
        routes = {
            source.url: (LISTING_HTML, 200),
            "https://apnews.com/article/one": (ARTICLE_JSONLD, 200),
            "https://apnews.com/article/two": (b"<<broken-marker>>", 200),
            "https://apnews.com/article/three": (ARTICLE_NO_TIMESTAMP, 200),
        }

        async def fake_sleep(_: float) -> None:
            return None

        # Make HTMLParser raise specifically when given the broken article;
        # the listing parse and the other two articles must still succeed.
        real_parser = html_module.HTMLParser

        def maybe_failing_parser(content: bytes) -> HTMLParser:
            if b"<<broken-marker>>" in content:
                raise ValueError("simulated selectolax failure")
            return real_parser(content)

        monkeypatch.setattr(html_module, "HTMLParser", maybe_failing_parser)

        client = _make_client(_route_responder(routes))
        result = await _ingester(ingest_db, client, sleep=fake_sleep).fetch(source)

        assert result.status is IngestStatus.OK
        assert result.count == 2
        assert _row_count(ingest_db) == 2


# ── TestUrlDedup ───────────────────────────────────────────────────────────


class TestUrlDedup:
    async def test_same_url_twice_in_listing_only_fetched_once(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = _ap_tr()
        calls: list[str] = []
        routes = {
            source.url: (LISTING_HTML_WITH_DUPLICATES, 200),
            "https://apnews.com/article/one": (ARTICLE_JSONLD, 200),
        }

        async def fake_sleep(_: float) -> None:
            return None

        client = _make_client(_route_responder(routes, calls))
        result = await _ingester(ingest_db, client, sleep=fake_sleep).fetch(source)

        assert result.status is IngestStatus.OK
        assert result.count == 1
        # Listing fetched once + the deduplicated article URL fetched once.
        assert calls.count("https://apnews.com/article/one") == 1
        assert calls.count(source.url) == 1
        assert _row_count(ingest_db) == 1


# ── TestRateLimit ──────────────────────────────────────────────────────────


class TestRateLimit:
    async def test_sleep_called_between_each_article(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = _ap_tr()
        routes = {
            source.url: (LISTING_HTML, 200),
            "https://apnews.com/article/one": (ARTICLE_JSONLD, 200),
            "https://apnews.com/article/two": (ARTICLE_META, 200),
            "https://apnews.com/article/three": (ARTICLE_NO_TIMESTAMP, 200),
        }
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        client = _make_client(_route_responder(routes))
        await _ingester(ingest_db, client, sleep=fake_sleep).fetch(source)

        # 3 article URLs → 2 sleeps (between them), each = source.rate_limit_seconds.
        assert sleeps == [
            float(source.rate_limit_seconds),
            float(source.rate_limit_seconds),
        ]


# ── TestCanonicalTimestamp ─────────────────────────────────────────────────


class TestCanonicalTimestamp:
    def test_jsonld_path(self) -> None:
        tree = HTMLParser(ARTICLE_JSONLD)
        ts, method = extract_canonical_timestamp(
            tree, fetched_at=datetime(2030, 1, 1), scope_selector=None
        )
        assert method == "json-ld"
        # JSON-LD value was 2026-05-23T08:00:00Z; naive UTC means 08:00.
        assert ts == datetime(2026, 5, 23, 8, 0, 0)
        assert ts.tzinfo is None

    def test_meta_path_converts_tz_to_utc(self) -> None:
        tree = HTMLParser(ARTICLE_META)
        ts, method = extract_canonical_timestamp(
            tree, fetched_at=datetime(2030, 1, 1), scope_selector=None
        )
        assert method == "meta"
        # Source value 10:00+03:00 → 07:00 UTC.
        assert ts == datetime(2026, 5, 23, 7, 0, 0)
        assert ts.tzinfo is None

    def test_turkish_regex_path(self) -> None:
        tree = HTMLParser(ARTICLE_TURKISH_REGEX)
        ts, method = extract_canonical_timestamp(
            tree, fetched_at=datetime(2030, 1, 1), scope_selector=None
        )
        assert method == "turkish-regex"
        assert ts == datetime(2026, 5, 23)

    def test_fetched_at_fallback(self) -> None:
        tree = HTMLParser(ARTICLE_NO_TIMESTAMP)
        sentinel = datetime(2026, 5, 23, 12, 30, 45)
        ts, method = extract_canonical_timestamp(
            tree, fetched_at=sentinel, scope_selector=None
        )
        assert method == "fetched-at"
        assert ts == sentinel

    def test_jsonld_beats_meta(self) -> None:
        # If both are present, json-ld step runs first.
        page = b"""<!doctype html><html><head>
<meta property="article:published_time" content="2099-12-31T00:00:00Z">
<script type="application/ld+json">
{"datePublished":"2026-05-23T08:00:00Z"}
</script>
</head><body></body></html>"""
        tree = HTMLParser(page)
        ts, method = extract_canonical_timestamp(
            tree, fetched_at=datetime(2030, 1, 1), scope_selector=None
        )
        assert method == "json-ld"
        assert ts == datetime(2026, 5, 23, 8, 0, 0)

    def test_naive_input_unchanged(self) -> None:
        # The Turkish-regex step produces a naive datetime; the chain must
        # not silently shift it by a tz conversion.
        tree = HTMLParser(ARTICLE_TURKISH_REGEX)
        ts, _ = extract_canonical_timestamp(
            tree, fetched_at=datetime(2030, 1, 1), scope_selector=None
        )
        assert ts.tzinfo is None
        assert ts.hour == 0
        assert ts.minute == 0


# ── TestEmptyListing ───────────────────────────────────────────────────────


class TestEmptyListing:
    async def test_empty_listing_returns_ok_zero_count(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = _ap_tr()
        routes = {source.url: (LISTING_HTML_EMPTY, 200)}
        calls: list[str] = []
        client = _make_client(_route_responder(routes, calls))

        result = await _ingester(ingest_db, client).fetch(source)

        assert result.status is IngestStatus.OK
        assert result.count == 0
        assert calls == [source.url]  # No per-article fetches attempted.


# ── TestProtocolReturnType ─────────────────────────────────────────────────


class TestProtocolReturnType:
    async def test_returns_ingest_result_instance(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = _ap_tr()
        routes = {source.url: (LISTING_HTML_EMPTY, 200)}
        client = _make_client(_route_responder(routes))
        result = await _ingester(ingest_db, client).fetch(source)
        assert isinstance(result, IngestResult)


# ── Helpers ────────────────────────────────────────────────────────────────


def _row_count(conn: duckdb.DuckDBPyConnection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM raw_articles").fetchone()
    return int(row[0]) if row else 0

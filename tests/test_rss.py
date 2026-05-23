"""Tests for musahit.ingest.rss — RSS/Atom feed ingester.

All HTTP calls are mocked through :class:`httpx.MockTransport`. The test
suite never touches the network. DB writes go to an in-memory DuckDB with
the v1 schema applied and the canonical source registry seeded.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Generator
from datetime import datetime
from pathlib import Path

import duckdb
import httpx
import pytest

from musahit.common.ids import article_id
from musahit.common.migrations import init_db
from musahit.common.types import IngestStatus
from musahit.ingest import USER_AGENT, Ingester, IngestResult
from musahit.ingest.rss import RssIngester
from musahit.ingest.sources import get_source, seed_sources

# ── Canned response bytes ──────────────────────────────────────────────────

# Minimal RSS 2.0 feed: two distinct entries, valid pubDate, deterministic.
RSS_TWO_ITEMS: bytes = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>Bianet test</title>
<link>https://bianet.org</link>
<description>Test feed</description>
<item>
<title>Article One</title>
<link>https://bianet.org/article-one</link>
<guid isPermaLink="false">guid-1</guid>
<pubDate>Sat, 23 May 2026 10:00:00 +0000</pubDate>
<description>Body of article one</description>
</item>
<item>
<title>Article Two</title>
<link>https://bianet.org/article-two</link>
<guid isPermaLink="false">guid-2</guid>
<pubDate>Sat, 23 May 2026 11:30:00 +0000</pubDate>
<description>Body of article two</description>
</item>
</channel>
</rss>
"""

# Same feed shape but with one entry repeated twice (same guid + same link).
RSS_DUPLICATE_GUIDS: bytes = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>Bianet test</title>
<link>https://bianet.org</link>
<description>Test feed</description>
<item>
<title>Article One v1</title>
<link>https://bianet.org/article-one</link>
<guid isPermaLink="false">guid-1</guid>
<pubDate>Sat, 23 May 2026 10:00:00 +0000</pubDate>
</item>
<item>
<title>Article One v2 (duplicate)</title>
<link>https://bianet.org/article-one</link>
<guid isPermaLink="false">guid-1</guid>
<pubDate>Sat, 23 May 2026 10:05:00 +0000</pubDate>
</item>
<item>
<title>Article Two</title>
<link>https://bianet.org/article-two</link>
<guid isPermaLink="false">guid-2</guid>
<pubDate>Sat, 23 May 2026 11:30:00 +0000</pubDate>
</item>
</channel>
</rss>
"""

# Valid RSS skeleton with zero <item> entries — feedparser sets bozo=0 here.
RSS_EMPTY: bytes = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>Empty feed</title>
<link>https://example.com</link>
<description>No items</description>
</channel>
</rss>
"""

# Malformed XML — unclosed tags, truncated content. feedparser returns
# bozo=1 with empty entries.
RSS_MALFORMED: bytes = b"<?xml version='1.0'?><rss><channel><title>broken"

# Not XML at all — an HTML page where the operator expected a feed.
NOT_XML: bytes = (
    b"<!doctype html><html><head><title>Not a feed</title></head>"
    b"<body><h1>Welcome</h1></body></html>"
)

# Atom-style entry with both published and updated; canonical = the earlier.
ATOM_PUBLISHED_BEFORE_UPDATED: bytes = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Atom test</title>
<link href="https://bianet.org"/>
<id>urn:uuid:test</id>
<updated>2026-05-23T12:00:00Z</updated>
<entry>
<title>Entry with both timestamps</title>
<link href="https://bianet.org/entry-a"/>
<id>entry-a</id>
<published>2026-05-23T08:00:00Z</published>
<updated>2026-05-23T11:00:00Z</updated>
<summary>Published at 08:00, updated at 11:00</summary>
</entry>
</feed>
"""


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture()
def ingest_db(tmp_path: Path) -> Generator[duckdb.DuckDBPyConnection, None, None]:
    """Temp-file DuckDB with schema applied and the canonical sources seeded.

    Uses a file-backed DB (rather than ``:memory:``) so :func:`init_db` and
    the test share the same store across the open/reopen cycle. VSS is off
    because no test touches embedding tables.
    """
    db_path = tmp_path / "test.duckdb"
    init_db(db_path, load_vss=False)
    conn = duckdb.connect(str(db_path))
    seed_sources(conn)
    try:
        yield conn
    finally:
        conn.close()


def _make_client(responder: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    """Async httpx client routed entirely through MockTransport — no network."""
    return httpx.AsyncClient(transport=httpx.MockTransport(responder))


def _static_response(body: bytes, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    """Build a responder that returns ``body`` and ``status`` for every request."""

    def _responder(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=status,
            content=body,
            headers={"content-type": "application/rss+xml; charset=utf-8"},
        )

    return _responder


# ── TestProtocolCompliance ─────────────────────────────────────────────────


class TestProtocolCompliance:
    def test_rss_ingester_satisfies_protocol(self) -> None:
        """Structural — RssIngester instances are usable as Ingester."""
        ingester: Ingester = RssIngester(conn=duckdb.connect(":memory:"))
        assert hasattr(ingester, "fetch")


# ── TestFetchOk ────────────────────────────────────────────────────────────


class TestFetchOk:
    async def test_returns_ok_with_count(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = get_source("bianet")
        client = _make_client(_static_response(RSS_TWO_ITEMS))
        ingester = RssIngester(conn=ingest_db, client=client)

        result = await ingester.fetch(source)

        assert isinstance(result, IngestResult)
        assert result.status is IngestStatus.OK
        assert result.count == 2
        assert result.error is None

    async def test_rows_persisted_correctly(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = get_source("bianet")
        client = _make_client(_static_response(RSS_TWO_ITEMS))
        ingester = RssIngester(conn=ingest_db, client=client)

        await ingester.fetch(source)

        rows = ingest_db.execute(
            """
            SELECT id, source_id, url, fetch_status_code, content_type,
                   feed_entry_id, canonical_timestamp, headers
            FROM raw_articles ORDER BY url
            """
        ).fetchall()
        assert len(rows) == 2
        urls = {r[2] for r in rows}
        assert urls == {
            "https://bianet.org/article-one",
            "https://bianet.org/article-two",
        }
        for row in rows:
            assert row[1] == "bianet"
            assert row[3] == 200
            assert "rss" in row[4]
            # Typed columns (ADR-015).
            assert row[5] in {"guid-1", "guid-2"}
            assert row[6] is not None  # canonical_timestamp populated
            # headers JSON now contains only RSS-specific metadata.
            meta = json.loads(row[7])
            assert "feed_entry_id" not in meta
            assert "canonical_published_at" not in meta
            assert meta["title"] in {"Article One", "Article Two"}

    async def test_article_ids_are_deterministic(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = get_source("bianet")
        client = _make_client(_static_response(RSS_TWO_ITEMS))
        await RssIngester(conn=ingest_db, client=client).fetch(source)

        ids = {r[0] for r in ingest_db.execute("SELECT id FROM raw_articles").fetchall()}
        expected = {
            article_id("bianet", "https://bianet.org/article-one"),
            article_id("bianet", "https://bianet.org/article-two"),
        }
        assert ids == expected

    async def test_user_agent_header_is_set(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        captured: dict[str, str] = {}

        def responder(request: httpx.Request) -> httpx.Response:
            captured["ua"] = request.headers.get("user-agent", "")
            return httpx.Response(200, content=RSS_TWO_ITEMS)

        source = get_source("bianet")
        ingester = RssIngester(conn=ingest_db, client=_make_client(responder))
        await ingester.fetch(source)

        assert captured["ua"] == USER_AGENT


# ── TestHttpErrors ────────────────────────────────────────────────────────


class TestHttpErrors:
    async def test_503_returns_http_error_and_writes_no_rows(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = get_source("bianet")
        client = _make_client(_static_response(b"upstream down", status=503))
        ingester = RssIngester(conn=ingest_db, client=client)

        result = await ingester.fetch(source)

        assert result.status is IngestStatus.HTTP_ERROR
        assert result.count == 0
        assert "503" in (result.error or "")
        assert _row_count(ingest_db) == 0

    async def test_404_returns_http_error_and_writes_no_rows(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = get_source("bianet")
        client = _make_client(_static_response(b"not found", status=404))
        ingester = RssIngester(conn=ingest_db, client=client)

        result = await ingester.fetch(source)

        assert result.status is IngestStatus.HTTP_ERROR
        assert result.count == 0
        assert "404" in (result.error or "")
        assert _row_count(ingest_db) == 0


# ── TestTimeout ───────────────────────────────────────────────────────────


class TestTimeout:
    async def test_httpx_timeout_returns_timeout_status(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        def responder(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("simulated read timeout", request=request)

        source = get_source("bianet")
        ingester = RssIngester(conn=ingest_db, client=_make_client(responder))

        result = await ingester.fetch(source)

        assert result.status is IngestStatus.TIMEOUT
        assert result.count == 0
        assert _row_count(ingest_db) == 0


# ── TestParseErrors ───────────────────────────────────────────────────────


class TestParseErrors:
    async def test_bozo_with_empty_entries_is_parse_error(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = get_source("bianet")
        client = _make_client(_static_response(RSS_MALFORMED))
        ingester = RssIngester(conn=ingest_db, client=client)

        result = await ingester.fetch(source)

        assert result.status is IngestStatus.PARSE_ERROR
        assert result.count == 0
        assert _row_count(ingest_db) == 0

    async def test_non_xml_bytes_return_parse_error_without_raising(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = get_source("bianet")
        client = _make_client(_static_response(NOT_XML))
        ingester = RssIngester(conn=ingest_db, client=client)

        # No exception expected — the call must complete cleanly.
        result = await ingester.fetch(source)

        assert result.status is IngestStatus.PARSE_ERROR
        assert result.count == 0
        assert _row_count(ingest_db) == 0


# ── TestEmptyFeed ─────────────────────────────────────────────────────────


class TestEmptyFeed:
    async def test_valid_empty_feed_returns_ok_count_zero(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = get_source("bianet")
        client = _make_client(_static_response(RSS_EMPTY))
        ingester = RssIngester(conn=ingest_db, client=client)

        result = await ingester.fetch(source)

        assert result.status is IngestStatus.OK
        assert result.count == 0
        assert _row_count(ingest_db) == 0


# ── TestDedup ─────────────────────────────────────────────────────────────


class TestDedup:
    async def test_intra_fetch_duplicate_guids_collapse(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = get_source("bianet")
        client = _make_client(_static_response(RSS_DUPLICATE_GUIDS))
        ingester = RssIngester(conn=ingest_db, client=client)

        result = await ingester.fetch(source)

        # 3 raw items, but two share guid-1 → only 2 unique entries persisted.
        assert result.status is IngestStatus.OK
        assert result.count == 2
        assert _row_count(ingest_db) == 2

    async def test_rerun_against_same_bytes_writes_no_duplicates(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = get_source("bianet")
        client = _make_client(_static_response(RSS_TWO_ITEMS))
        ingester = RssIngester(conn=ingest_db, client=client)

        first = await ingester.fetch(source)
        second = await ingester.fetch(source)

        assert first.status is IngestStatus.OK
        assert first.count == 2
        assert second.status is IngestStatus.OK
        assert second.count == 0
        assert _row_count(ingest_db) == 2


# ── TestCanonicalTimestamp ────────────────────────────────────────────────


class TestCanonicalTimestamp:
    async def test_prefers_earlier_of_published_and_updated(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = get_source("bianet")
        client = _make_client(_static_response(ATOM_PUBLISHED_BEFORE_UPDATED))
        ingester = RssIngester(conn=ingest_db, client=client)

        await ingester.fetch(source)

        row = ingest_db.execute(
            "SELECT canonical_timestamp FROM raw_articles"
        ).fetchone()
        assert row is not None
        ts = row[0]
        # 08:00 is earlier than 11:00 → canonical timestamp is published.
        # DuckDB returns a python datetime for TIMESTAMP columns.
        assert isinstance(ts, datetime)
        assert ts.year == 2026
        assert ts.month == 5
        assert ts.day == 23
        assert ts.hour == 8
        assert ts.minute == 0


# ── Helpers ───────────────────────────────────────────────────────────────


def _row_count(conn: duckdb.DuckDBPyConnection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM raw_articles").fetchone()
    return int(row[0]) if row else 0

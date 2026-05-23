"""Tests for musahit.ingest.resmi_gazete — daily PDF ingester.

Every test routes HTTP through :class:`httpx.MockTransport`. The parser
itself is exercised in :mod:`tests.test_gazette_parsing`; this suite uses
either the real fixture PDF or an injected ``parse_pdf`` mock — whichever
makes the scenario clearest.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Generator
from datetime import date, datetime
from pathlib import Path

import duckdb
import httpx
import pytest

from musahit.common.ids import article_id
from musahit.common.migrations import init_db
from musahit.common.types import IngestStatus
from musahit.ingest.gazette_parsing import (
    GazetteItem,
    GazetteItemType,
    GazetteSection,
)
from musahit.ingest.resmi_gazete import ResmiGazeteIngester, _build_pdf_url
from musahit.ingest.sources import get_source, seed_sources

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "resmi_gazete"
SAMPLE_PDF: bytes = (FIXTURE_DIR / "sample_gazette.pdf").read_bytes()
MUKERRER_PDF: bytes = (FIXTURE_DIR / "mukerrer_supplement.pdf").read_bytes()
CORRUPTED: bytes = (FIXTURE_DIR / "corrupted.bin").read_bytes()

TARGET_DATE = date(2026, 5, 23)
YESTERDAY = date(2026, 5, 22)


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture()
def ingest_db(tmp_path: Path) -> Generator[duckdb.DuckDBPyConnection, None, None]:
    db_path = tmp_path / "test.duckdb"
    init_db(db_path, load_vss=False)
    conn = duckdb.connect(str(db_path))
    seed_sources(conn)
    try:
        yield conn
    finally:
        conn.close()


def _make_client(responder: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(responder))


def _route_responder(
    routes: dict[str, tuple[bytes, int]],
    calls: list[str] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def _responder(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if calls is not None:
            calls.append(url)
        if url in routes:
            body, status = routes[url]
            return httpx.Response(
                status, content=body, headers={"content-type": "application/pdf"}
            )
        return httpx.Response(404, content=b"not found")

    return _responder


def _gazette_source() -> object:
    return get_source("resmi_gazete")


# ── TestSuccessfulFetchParse ───────────────────────────────────────────────


class TestSuccessfulFetchParse:
    async def test_today_pdf_parses_to_multiple_rows(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = _gazette_source()
        today_url = _build_pdf_url(TARGET_DATE, mukerrer=0)
        routes = {today_url: (SAMPLE_PDF, 200)}
        client = _make_client(_route_responder(routes))

        ingester = ResmiGazeteIngester(
            conn=ingest_db, client=client, target_date=TARGET_DATE
        )
        result = await ingester.fetch(source)

        assert result.status is IngestStatus.OK
        assert result.count == 4
        rows = ingest_db.execute(
            """
            SELECT url, feed_entry_id, canonical_timestamp, headers
            FROM raw_articles ORDER BY url
            """
        ).fetchall()
        assert len(rows) == 4
        for url, feed_entry_id, canonical_ts, headers_json in rows:
            assert url.startswith("resmi-gazete://2026-05-23/")
            assert feed_entry_id  # all four items have references
            assert canonical_ts == datetime(2026, 5, 23, 0, 0, 0)
            meta = json.loads(headers_json)
            assert meta["section"] in {"EXECUTIVE", "JUDICIAL", "ANNOUNCEMENT"}
            assert meta["real_pdf_url"] == today_url
            assert meta["mukerrer"] == 0

    async def test_article_id_uses_synthetic_url(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = _gazette_source()
        today_url = _build_pdf_url(TARGET_DATE, mukerrer=0)
        routes = {today_url: (SAMPLE_PDF, 200)}
        client = _make_client(_route_responder(routes))

        await ResmiGazeteIngester(
            conn=ingest_db, client=client, target_date=TARGET_DATE
        ).fetch(source)

        ids = {
            r[0] for r in ingest_db.execute("SELECT id FROM raw_articles").fetchall()
        }
        expected = {
            article_id("resmi_gazete", "resmi-gazete://2026-05-23/LAW/7460"),
            article_id(
                "resmi_gazete",
                "resmi-gazete://2026-05-23/PRESIDENTIAL_DECREE/152",
            ),
            article_id(
                "resmi_gazete",
                "resmi-gazete://2026-05-23/COURT_DECISION/2026/123",
            ),
            article_id(
                "resmi_gazete", "resmi-gazete://2026-05-23/COMMUNIQUE/2026/89"
            ),
        }
        assert ids == expected


# ── TestDateFallback ───────────────────────────────────────────────────────


class TestDateFallback:
    async def test_today_404_falls_back_to_yesterday(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = _gazette_source()
        today_url = _build_pdf_url(TARGET_DATE, mukerrer=0)
        yesterday_url = _build_pdf_url(YESTERDAY, mukerrer=0)
        routes = {
            today_url: (b"not found", 404),
            yesterday_url: (SAMPLE_PDF, 200),
        }
        calls: list[str] = []
        client = _make_client(_route_responder(routes, calls))

        result = await ResmiGazeteIngester(
            conn=ingest_db, client=client, target_date=TARGET_DATE
        ).fetch(source)

        assert result.status is IngestStatus.OK
        assert result.count == 4
        # Both candidate URLs were probed; yesterday's was successful.
        assert today_url in calls
        assert yesterday_url in calls
        rows = ingest_db.execute(
            "SELECT headers FROM raw_articles LIMIT 1"
        ).fetchone()
        meta = json.loads(rows[0])
        assert meta["real_pdf_url"] == yesterday_url
        # The publication date came from yesterday, not today.
        ts = ingest_db.execute(
            "SELECT canonical_timestamp FROM raw_articles LIMIT 1"
        ).fetchone()[0]
        assert ts == datetime(2026, 5, 22, 0, 0, 0)

    async def test_both_dates_404_returns_http_error(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = _gazette_source()
        client = _make_client(_route_responder({}))  # every URL → 404

        result = await ResmiGazeteIngester(
            conn=ingest_db, client=client, target_date=TARGET_DATE
        ).fetch(source)

        assert result.status is IngestStatus.HTTP_ERROR
        assert result.count == 0
        assert _row_count(ingest_db) == 0


# ── TestTimeoutAndCorruption ──────────────────────────────────────────────


class TestTimeout:
    async def test_timeout_returns_timeout_status(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        def responder(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("simulated", request=request)

        client = _make_client(responder)
        result = await ResmiGazeteIngester(
            conn=ingest_db, client=client, target_date=TARGET_DATE
        ).fetch(_gazette_source())

        # Both date candidates time out → both branch errors are TIMEOUT.
        # The ingester preserves the last error of the chain.
        assert result.status is IngestStatus.TIMEOUT
        assert result.count == 0


class TestCorruptedPdf:
    async def test_corrupted_main_pdf_returns_parse_error(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = _gazette_source()
        today_url = _build_pdf_url(TARGET_DATE, mukerrer=0)
        routes = {today_url: (CORRUPTED, 200)}
        client = _make_client(_route_responder(routes))

        result = await ResmiGazeteIngester(
            conn=ingest_db, client=client, target_date=TARGET_DATE
        ).fetch(source)

        assert result.status is IngestStatus.PARSE_ERROR
        assert result.count == 0
        assert _row_count(ingest_db) == 0


# ── TestMukerrerSupplement ─────────────────────────────────────────────────


class TestMukerrerSupplement:
    async def test_mukerrer_processed_when_present(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = _gazette_source()
        today_url = _build_pdf_url(TARGET_DATE, mukerrer=0)
        mukerrer_1_url = _build_pdf_url(TARGET_DATE, mukerrer=1)
        # Mükerrer 2 missing → probing stops there.
        routes = {
            today_url: (SAMPLE_PDF, 200),
            mukerrer_1_url: (MUKERRER_PDF, 200),
        }
        client = _make_client(_route_responder(routes))

        result = await ResmiGazeteIngester(
            conn=ingest_db, client=client, target_date=TARGET_DATE
        ).fetch(source)

        # Main: 4 items; Mükerrer: 1 item (YÖNETMELİK).
        assert result.status is IngestStatus.OK
        assert result.count == 5
        # Confirm the supplement row carries mukerrer=1 in headers.
        rows = ingest_db.execute(
            """
            SELECT json_extract_string(headers, '$.mukerrer') AS m
            FROM raw_articles
            """
        ).fetchall()
        m_values = {r[0] for r in rows}
        # DuckDB returns the string value for an integer JSON field.
        assert {"0", "1"} == m_values


# ── TestRerunIdempotent ────────────────────────────────────────────────────


class TestRerunIdempotent:
    async def test_second_fetch_produces_no_duplicates(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = _gazette_source()
        today_url = _build_pdf_url(TARGET_DATE, mukerrer=0)
        routes = {today_url: (SAMPLE_PDF, 200)}
        client = _make_client(_route_responder(routes))

        ingester = ResmiGazeteIngester(
            conn=ingest_db, client=client, target_date=TARGET_DATE
        )
        first = await ingester.fetch(source)
        second = await ingester.fetch(source)

        assert first.count == 4
        assert second.status is IngestStatus.OK
        assert second.count == 0
        assert _row_count(ingest_db) == 4


# ── TestParserInjection (zero-PDF tests) ──────────────────────────────────


class TestParserInjection:
    """A mock parse_pdf makes assertions about persistence shape simpler."""

    async def test_n_canned_items_produce_n_rows(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = _gazette_source()
        today_url = _build_pdf_url(TARGET_DATE, mukerrer=0)
        routes = {today_url: (b"%PDF-... (mocked)", 200)}

        def fake_parser(_pdf_bytes: bytes, _pub: date) -> list[GazetteItem]:
            return [
                GazetteItem(
                    section=GazetteSection.EXECUTIVE,
                    item_type=GazetteItemType.LAW,
                    reference_number="9999",
                    title="Fake",
                    body="...",
                    page_start=1,
                    page_end=1,
                ),
                GazetteItem(
                    section=GazetteSection.EXECUTIVE,
                    item_type=GazetteItemType.REGULATION,
                    reference_number="",  # tests synthetic-id fallback
                    title="No ref",
                    body="...",
                    page_start=2,
                    page_end=2,
                ),
            ]

        client = _make_client(_route_responder(routes))
        result = await ResmiGazeteIngester(
            conn=ingest_db,
            client=client,
            target_date=TARGET_DATE,
            parse_pdf=fake_parser,
        ).fetch(source)

        assert result.status is IngestStatus.OK
        assert result.count == 2
        rows = ingest_db.execute(
            "SELECT url, feed_entry_id FROM raw_articles ORDER BY url"
        ).fetchall()
        # Synthetic id is used when reference_number is empty.
        urls = sorted(r[0] for r in rows)
        assert urls[0] == "resmi-gazete://2026-05-23/LAW/9999"
        assert urls[1] == "resmi-gazete://2026-05-23/REGULATION/item-2-p2"
        # feed_entry_id is NULL when reference_number is empty.
        feed_ids = sorted((r[1] or "") for r in rows)
        assert feed_ids == ["", "9999"]


# ── Helpers ────────────────────────────────────────────────────────────────


def _row_count(conn: duckdb.DuckDBPyConnection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM raw_articles").fetchone()
    return int(row[0]) if row else 0

"""Integration tests for the Normalizer — DB I/O, dispatch, idempotence."""

from __future__ import annotations

import json
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from musahit.common.ids import article_id
from musahit.common.migrations import init_db
from musahit.common.types import SourceKind
from musahit.ingest.sources import seed_sources
from musahit.normalize.normalizer import (
    LEAD_MAX_CHARS,
    ExtractedArticle,
    Normalizer,
    RawArticleRow,
)


@pytest.fixture()
def ingest_db(tmp_path: Path) -> Generator[duckdb.DuckDBPyConnection, None, None]:
    db_path = tmp_path / "test.duckdb"
    init_db(db_path, load_vss=False)
    conn = duckdb.connect(str(db_path))
    seed_sources(conn)
    # Insert a parent pipeline_runs row so the FK on ingest_log holds.
    conn.execute(
        """
        INSERT INTO pipeline_runs (
            run_id, started_at, status, stages_done, counts
        ) VALUES (?, ?, ?, ?, ?)
        """,
        ["run_test", datetime(2026, 5, 23, 0, 0, 0), "RUNNING",
         json.dumps(["ingest"]), json.dumps({"articles": 0})],
    )
    try:
        yield conn
    finally:
        conn.close()


def _insert_ingest_log(conn: duckdb.DuckDBPyConnection, source_id: str) -> None:
    """Insert a minimal ingest_log row so the Normalizer's JOIN sees it."""
    conn.execute(
        """
        INSERT INTO ingest_log (
            run_id, source_id, started_at, completed_at, status, articles_fetched
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        ["run_test", source_id, datetime(2026, 5, 23), datetime(2026, 5, 23), "OK", 1],
    )


def _insert_raw_article(
    conn: duckdb.DuckDBPyConnection,
    source_id: str,
    url: str,
    raw_content: bytes,
    content_type: str,
    headers: dict,
    canonical_ts: datetime | None = None,
) -> str:
    row_id = article_id(source_id, url)
    conn.execute(
        """
        INSERT INTO raw_articles (
            id, source_id, url, fetched_at, raw_content, content_type,
            headers, fetch_status_code, feed_entry_id, canonical_timestamp
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            row_id,
            source_id,
            url,
            datetime(2026, 5, 23, 1, 0, 0),
            raw_content,
            content_type,
            json.dumps(headers),
            200,
            None,
            canonical_ts,
        ],
    )
    return row_id


# ── TestDispatchAndPersistence ─────────────────────────────────────────────


class TestDispatchAndPersistence:
    async def test_one_rss_row_normalizes_to_articles(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        _insert_ingest_log(ingest_db, "bianet")
        row_id = _insert_raw_article(
            ingest_db,
            source_id="bianet",
            url="https://bianet.org/article-one",
            raw_content=b"<rss>...</rss>",
            content_type="application/rss+xml",
            headers={
                "title": "Test başlığı",
                "body": (
                    "Türkiye'de bugün yeni bir gelişme yaşandı. "
                    "İkinci cümle yeterince uzun olmalı ki dil tespiti çalışsın."
                ),
            },
            canonical_ts=datetime(2026, 5, 23, 8, 0, 0),
        )

        summary = await Normalizer(conn=ingest_db).run(run_id="run_test")

        assert summary == {"normalized": 1, "skipped": 0}
        row = ingest_db.execute(
            "SELECT id, title, body, lead, language, word_count, published_at FROM articles"
        ).fetchone()
        assert row is not None
        article_id_db, title, body, lead, language, wc, published_at = row
        assert article_id_db == row_id
        assert title == "Test başlığı"
        assert "Türkiye'de bugün" in body
        assert lead.startswith("Türkiye'de bugün")
        assert language == "tr"
        assert wc == len(body.split())
        assert published_at == datetime(2026, 5, 23, 8, 0, 0)

    async def test_dispatches_by_source_kind(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        # One row per kind, each with a distinct expected output.
        # RSS (bianet)
        _insert_ingest_log(ingest_db, "bianet")
        _insert_raw_article(
            ingest_db, "bianet", "https://bianet.org/a",
            b"", "application/rss+xml",
            {"title": "RSS makalesi", "body": "RSS gövdesi yeterince uzundur burada."},
            datetime(2026, 5, 23),
        )
        # PDF (resmi_gazete)
        _insert_ingest_log(ingest_db, "resmi_gazete")
        _insert_raw_article(
            ingest_db, "resmi_gazete", "resmi-gazete://2026-05-23/LAW/7460",
            b"%PDF-mock", "application/pdf",
            {"title": "Kanun başlığı", "body": "Kanun gövdesi yeterince uzundur burada."},
            datetime(2026, 5, 23),
        )
        # Reddit (reddit_turkey)
        _insert_ingest_log(ingest_db, "reddit_turkey")
        _insert_raw_article(
            ingest_db, "reddit_turkey", "https://www.reddit.com/r/Turkey/comments/abc/",
            json.dumps({
                "title": "Reddit başlığı",
                "selftext": "Reddit selftext yeterince uzun bir metindir burada.",
                "comments": [],
            }).encode("utf-8"),
            "application/json",
            {"subreddit": "Turkey"},
            datetime(2026, 5, 23),
        )

        summary = await Normalizer(conn=ingest_db).run(run_id="run_test")

        assert summary["normalized"] == 3
        rows = ingest_db.execute(
            "SELECT source_id, title FROM articles ORDER BY source_id"
        ).fetchall()
        by_src = {r[0]: r[1] for r in rows}
        assert by_src["bianet"] == "RSS makalesi"
        assert by_src["resmi_gazete"] == "Kanun başlığı"
        assert by_src["reddit_turkey"] == "Reddit başlığı"


# ── TestEnrichment ─────────────────────────────────────────────────────────


class TestEnrichment:
    async def test_lead_is_first_500_chars(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        _insert_ingest_log(ingest_db, "bianet")
        body_text = "a" * 800
        _insert_raw_article(
            ingest_db, "bianet", "https://bianet.org/long",
            b"", "application/rss+xml",
            {"title": "Long", "body": body_text},
            datetime(2026, 5, 23),
        )
        await Normalizer(conn=ingest_db).run(run_id="run_test")
        lead = ingest_db.execute("SELECT lead FROM articles").fetchone()[0]
        assert len(lead) == LEAD_MAX_CHARS
        assert lead == "a" * LEAD_MAX_CHARS

    async def test_word_count_matches_split(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        _insert_ingest_log(ingest_db, "bianet")
        body = "bir iki üç dört beş altı yedi sekiz dokuz on on bir on iki on üç on dört"
        _insert_raw_article(
            ingest_db, "bianet", "https://bianet.org/words",
            b"", "application/rss+xml",
            {"title": "wc", "body": body},
            datetime(2026, 5, 23),
        )
        await Normalizer(conn=ingest_db).run(run_id="run_test")
        wc = ingest_db.execute("SELECT word_count FROM articles").fetchone()[0]
        assert wc == len(body.split())

    async def test_entities_are_json_array(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        _insert_ingest_log(ingest_db, "bianet")
        _insert_raw_article(
            ingest_db, "bianet", "https://bianet.org/ents",
            b"", "application/rss+xml",
            {
                "title": "Politik haber",
                "body": "AKP ve CHP arasındaki tartışma TBMM'de devam etti uzun bir süredir.",
            },
            datetime(2026, 5, 23),
        )
        await Normalizer(conn=ingest_db).run(run_id="run_test")
        entities_json = ingest_db.execute("SELECT entities FROM articles").fetchone()[0]
        ents = json.loads(entities_json)
        texts = {e["text"] for e in ents}
        assert {"AKP", "CHP", "TBMM"}.issubset(texts)


# ── TestIdempotence ────────────────────────────────────────────────────────


class TestIdempotence:
    async def test_rerun_does_not_duplicate_articles(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        _insert_ingest_log(ingest_db, "bianet")
        _insert_raw_article(
            ingest_db, "bianet", "https://bianet.org/idem",
            b"", "application/rss+xml",
            {"title": "Idempotent", "body": "Bu metin yeterince uzun olduğu için çalışacak."},
            datetime(2026, 5, 23),
        )

        first = await Normalizer(conn=ingest_db).run(run_id="run_test")
        second = await Normalizer(conn=ingest_db).run(run_id="run_test")

        assert first == {"normalized": 1, "skipped": 0}
        # Second run finds no pending rows (article already inserted).
        assert second == {"normalized": 0, "skipped": 0}
        count = ingest_db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        assert count == 1


# ── TestFailureIsolation ───────────────────────────────────────────────────


class TestFailureIsolation:
    async def test_extractor_exception_logs_and_skips(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        _insert_ingest_log(ingest_db, "bianet")
        _insert_ingest_log(ingest_db, "diken")
        _insert_raw_article(
            ingest_db, "bianet", "https://bianet.org/ok",
            b"", "application/rss+xml",
            {"title": "OK", "body": "Bu satır normal şekilde işlenecektir uzun bir metin."},
            datetime(2026, 5, 23),
        )
        _insert_raw_article(
            ingest_db, "diken", "https://www.diken.com.tr/bad",
            b"", "application/rss+xml",
            {"title": "BAD", "body": "trigger-exception-marker yeterince uzun bir metin."},
            datetime(2026, 5, 23),
        )

        def factory(kind: SourceKind):
            from musahit.normalize.normalizer import ExtractedArticle as EA

            def _ext(row):
                body = (row.headers.get("body") or "").strip()
                if "trigger-exception-marker" in body:
                    raise RuntimeError("simulated extractor failure")
                return EA(
                    title=row.headers.get("title", ""),
                    body=body,
                    published_at=row.canonical_timestamp,
                )

            return _ext

        summary = await Normalizer(conn=ingest_db, extractor_factory=factory).run("run_test")
        assert summary == {"normalized": 1, "skipped": 1}
        ids = [r[0] for r in ingest_db.execute("SELECT source_id FROM articles").fetchall()]
        assert "bianet" in ids
        assert "diken" not in ids


# ── TestStagesDone ─────────────────────────────────────────────────────────


class TestStagesDone:
    async def test_stages_done_appends_normalize(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        await Normalizer(conn=ingest_db).run(run_id="run_test")
        row = ingest_db.execute(
            "SELECT stages_done, counts FROM pipeline_runs WHERE run_id = 'run_test'"
        ).fetchone()
        stages = json.loads(row[0])
        counts = json.loads(row[1])
        assert "ingest" in stages  # preserved from the pre-seeded row
        assert "normalize" in stages
        assert stages.index("normalize") == len(stages) - 1
        assert "articles_normalized" in counts


# ── TestDataclassDefaults ──────────────────────────────────────────────────


class TestDataclassDefaults:
    def test_extracted_article_minimal_construction(self) -> None:
        a = ExtractedArticle(title="t", body="b")
        assert a.lead == ""
        assert a.published_at is None
        assert a.language == ""
        assert a.entities == []
        assert a.word_count == 0

    def test_raw_article_row_construction(self) -> None:
        row = RawArticleRow(
            id="x",
            source_id="bianet",
            url="https://example.com",
            fetched_at=datetime(2026, 5, 23, tzinfo=UTC).replace(tzinfo=None),
            raw_content=b"",
            content_type="text/html",
            headers={},
            canonical_timestamp=None,
        )
        assert row.id == "x"

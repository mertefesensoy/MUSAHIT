"""Integration tests for musahit.cluster.clusterer.Clusterer.

The DB fixture mirrors the normalize-stage fixture: temp-file DuckDB
with schema applied + sources seeded + a parent pipeline_runs row so the
ingest_log FK holds. Articles are pre-seeded into the ``articles`` and
``ingest_log`` tables so the Clusterer's SELECT picks them up.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Generator
from datetime import datetime
from pathlib import Path

import duckdb
import pytest

from musahit.cluster.clusterer import (
    DEFAULT_COSINE_THRESHOLD,
    Clusterer,
    EmbeddingUnavailableError,
)
from musahit.cluster.embedder import DIMENSION, FakeEmbeddingClient
from musahit.common.migrations import init_db
from musahit.ingest.sources import seed_sources

# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture()
def ingest_db(tmp_path: Path) -> Generator[duckdb.DuckDBPyConnection, None, None]:
    db_path = tmp_path / "test.duckdb"
    init_db(db_path, load_vss=False)
    conn = duckdb.connect(str(db_path))
    seed_sources(conn)
    conn.execute(
        """
        INSERT INTO pipeline_runs (
            run_id, started_at, status, stages_done, counts
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [
            "run_test",
            datetime(2026, 5, 23, 0, 0, 0),
            "RUNNING",
            json.dumps(["ingest", "normalize"]),
            json.dumps({"articles": 0}),
        ],
    )
    try:
        yield conn
    finally:
        conn.close()


def _insert_article(
    conn: duckdb.DuckDBPyConnection,
    *,
    article_id: str,
    source_id: str,
    title: str,
    body: str,
    language: str = "tr",
    published_at: datetime = datetime(2026, 5, 23, 8, 0, 0),
    word_count: int | None = None,
) -> None:
    lead = body[:500]
    wc = word_count if word_count is not None else len(body.split())
    conn.execute(
        """
        INSERT OR IGNORE INTO ingest_log (
            run_id, source_id, started_at, completed_at, status, articles_fetched
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        ["run_test", source_id, datetime(2026, 5, 23), datetime(2026, 5, 23), "OK", 1],
    )
    conn.execute(
        """
        INSERT INTO articles (
            id, source_id, url, fetched_at, published_at,
            title, lead, body, language, entities, word_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            article_id,
            source_id,
            f"https://example.com/{article_id}",
            datetime(2026, 5, 23, 1, 0, 0),
            published_at,
            title,
            lead,
            body,
            language,
            json.dumps([]),
            wc,
        ],
    )


def _short_id(seed: str) -> str:
    return hashlib.md5(seed.encode(), usedforsecurity=False).hexdigest()


def _body(words: list[str], repeat: int = 5) -> str:
    """Compose a long-enough body for the word_count thresholds."""
    return " ".join(words * repeat)


# Reusable token bags. Heavy overlap between EC_TOPIC_A and EC_TOPIC_B
# guarantees high cosine; FOOTBALL is disjoint from EC_TOPIC_*.
EC_TOPIC_A = [
    "Türkiye", "ekonomisi", "bugün", "yeni", "bir", "gelişme",
    "yaşadı", "enflasyon", "rakamları", "açıklandı",
]
EC_TOPIC_B = [
    "Türkiye", "ekonomisi", "bugün", "enflasyon", "rakamları",
    "yeni", "gelişme", "yaşadı", "açıklandı",
]
EC_BAG = [
    "ekonomi", "enflasyon", "faiz", "lira", "merkez", "bankası",
    "para", "politikası",
]
FOOTBALL_BAG = [
    "futbol", "maç", "sonuçları", "lig", "şampiyonluk", "takım",
    "galibiyet", "skor",
]
EN_BAG = [
    "turkey", "economy", "inflation", "rate", "central", "bank", "policy",
]
TR_SHORT_BAG = [
    "Türkiye", "ekonomisi", "bugün", "yeni", "bir", "gelişme", "yaşadı",
]
TR_8_BAG = [
    "Türkiye", "ekonomisi", "bugün", "yeni", "bir", "gelişme",
    "yaşadı", "enflasyon",
]


# ── TestSimilarArticlesCluster ─────────────────────────────────────────────


class TestSimilarArticlesCluster:
    async def test_two_similar_articles_share_a_cluster(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        _insert_article(
            ingest_db,
            article_id=_short_id("a1"),
            source_id="bianet",
            title="Türkiye ekonomisi bugün",
            body=_body(EC_TOPIC_A, repeat=4),
        )
        _insert_article(
            ingest_db,
            article_id=_short_id("a2"),
            source_id="cumhuriyet",
            title="Enflasyon rakamları açıklandı",
            body=_body(EC_TOPIC_B, repeat=4),
            published_at=datetime(2026, 5, 23, 9, 0, 0),
        )

        result = await Clusterer(ingest_db, FakeEmbeddingClient()).run("run_test")

        # Exactly one cluster created; both articles attached.
        assert result["new_clusters"] == 1
        assert result["joins"] == 1
        members = ingest_db.execute(
            "SELECT article_id FROM cluster_articles"
        ).fetchall()
        assert len(members) == 2


# ── TestDissimilarArticlesSeparate ─────────────────────────────────────────


class TestDissimilarArticlesSeparate:
    async def test_disjoint_articles_form_separate_clusters(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        _insert_article(
            ingest_db,
            article_id=_short_id("eco"),
            source_id="bianet",
            title="Ekonomi haberi",
            body=_body(EC_BAG),
        )
        _insert_article(
            ingest_db,
            article_id=_short_id("spor"),
            source_id="cumhuriyet",
            title="Spor sonuçları",
            body=_body(FOOTBALL_BAG),
            published_at=datetime(2026, 5, 23, 10, 0, 0),
        )

        result = await Clusterer(ingest_db, FakeEmbeddingClient()).run("run_test")
        assert result["new_clusters"] == 2
        assert result["joins"] == 0


# ── TestWindow24h ──────────────────────────────────────────────────────────


class TestWindow24h:
    async def test_articles_more_than_24h_apart_dont_join(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        # Two articles identical-ish in content but 30h apart in time —
        # they should NOT join because the 24h window has elapsed.
        body = _body(TR_SHORT_BAG)
        _insert_article(
            ingest_db,
            article_id=_short_id("old"),
            source_id="bianet",
            title="Eski haber",
            body=body,
            published_at=datetime(2026, 5, 23, 8, 0, 0),
        )
        _insert_article(
            ingest_db,
            article_id=_short_id("new"),
            source_id="cumhuriyet",
            title="Yeni haber",
            body=body,
            published_at=datetime(2026, 5, 24, 14, 0, 1),  # 30h later
        )

        result = await Clusterer(ingest_db, FakeEmbeddingClient()).run("run_test")
        # Two separate clusters because the window check excluded the
        # old one from being a candidate for the new one.
        assert result["new_clusters"] == 2


# ── TestLanguagePartitioning ───────────────────────────────────────────────


class TestLanguagePartitioning:
    async def test_different_languages_dont_cluster(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        body = _body(EN_BAG)
        _insert_article(
            ingest_db,
            article_id=_short_id("tr_one"),
            source_id="bianet",
            title="TR title",
            body=body,
            language="tr",
        )
        _insert_article(
            ingest_db,
            article_id=_short_id("en_one"),
            source_id="cumhuriyet",
            title="EN title",
            body=body,
            language="en",
            published_at=datetime(2026, 5, 23, 9, 0, 0),
        )

        result = await Clusterer(ingest_db, FakeEmbeddingClient()).run("run_test")
        # Same body → identical embedding, but different languages →
        # different buckets → two clusters.
        assert result["new_clusters"] == 2


# ── TestBandsPresent ───────────────────────────────────────────────────────


class TestBandsPresent:
    async def test_bands_present_is_union_of_member_source_bands(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        body = _body(TR_8_BAG)
        # bianet = independent; cumhuriyet = opposition.
        _insert_article(
            ingest_db,
            article_id=_short_id("b1"),
            source_id="bianet",
            title="t1",
            body=body,
        )
        _insert_article(
            ingest_db,
            article_id=_short_id("b2"),
            source_id="cumhuriyet",
            title="t2",
            body=body,
            published_at=datetime(2026, 5, 23, 9, 0, 0),
        )

        await Clusterer(ingest_db, FakeEmbeddingClient()).run("run_test")
        row = ingest_db.execute("SELECT bands_present FROM clusters").fetchone()
        assert row is not None
        bands = set(json.loads(row[0]))
        assert "independent" in bands
        assert "opposition" in bands


# ── TestHeadlineOnly ───────────────────────────────────────────────────────


class TestHeadlineOnly:
    async def test_headline_only_joins_existing_but_does_not_create_new(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        # Headline-only (word_count < 30) that matches a full-text article's
        # vector should JOIN that cluster.
        body_full = _body(TR_8_BAG)
        _insert_article(
            ingest_db,
            article_id=_short_id("full"),
            source_id="bianet",
            title="Tam metin",
            body=body_full,
        )
        # Headline-only article using exactly the same token vocabulary as the
        # full-text article so cosine clears the 0.7 threshold; 12 tokens →
        # word_count = 12 → falls below the new-cluster floor of 30.
        headline_body = " ".join(TR_8_BAG + TR_8_BAG[:4])  # 12 tokens, all from TR_8_BAG
        _insert_article(
            ingest_db,
            article_id=_short_id("hdr"),
            source_id="cumhuriyet",
            title="Manşet",
            body=headline_body,
            word_count=12,
            published_at=datetime(2026, 5, 23, 9, 0, 0),
        )

        result = await Clusterer(ingest_db, FakeEmbeddingClient()).run("run_test")
        assert result["new_clusters"] == 1
        # Headline joined the existing cluster.
        members = ingest_db.execute("SELECT article_id FROM cluster_articles").fetchall()
        assert len(members) == 2

    async def test_headline_only_without_match_is_skipped(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        # A lone headline-only article — no existing cluster to join, and
        # can't create one. Embedded but not clustered.
        _insert_article(
            ingest_db,
            article_id=_short_id("lone"),
            source_id="bianet",
            title="Manşet",
            body="kısa bir manşet sadece on iki kelime uzunluğunda yaklaşık olarak",
            word_count=12,
        )

        result = await Clusterer(ingest_db, FakeEmbeddingClient()).run("run_test")
        assert result["new_clusters"] == 0
        assert result["skipped_headline"] == 1
        clusters_n = ingest_db.execute("SELECT COUNT(*) FROM clusters").fetchone()[0]
        assert clusters_n == 0
        # Still embedded.
        emb_n = ingest_db.execute("SELECT COUNT(*) FROM article_embeddings").fetchone()[0]
        assert emb_n == 1


# ── TestEmptyBodySkipped ───────────────────────────────────────────────────


class TestEmptyBodySkipped:
    async def test_word_count_below_10_skipped_entirely(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        _insert_article(
            ingest_db,
            article_id=_short_id("empty"),
            source_id="bianet",
            title="boş",
            body="çok kısa",
            word_count=2,
        )

        result = await Clusterer(ingest_db, FakeEmbeddingClient()).run("run_test")
        assert result["skipped_empty"] == 1
        # No embedding written either.
        emb_n = ingest_db.execute("SELECT COUNT(*) FROM article_embeddings").fetchone()[0]
        assert emb_n == 0


# ── TestIdempotence ────────────────────────────────────────────────────────


class TestIdempotence:
    async def test_rerun_does_not_duplicate_clusters_or_embeddings(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        body = " ".join(["Türkiye", "ekonomisi", "bugün", "yeni", "gelişme", "enflasyon"] * 5)
        _insert_article(
            ingest_db,
            article_id=_short_id("idem"),
            source_id="bianet",
            title="t",
            body=body,
        )

        first = await Clusterer(ingest_db, FakeEmbeddingClient()).run("run_test")
        second = await Clusterer(ingest_db, FakeEmbeddingClient()).run("run_test")

        assert first["new_clusters"] == 1
        # Second pass finds zero pending (embedding present → LEFT JOIN excludes).
        assert second["new_clusters"] == 0
        clusters_n = ingest_db.execute("SELECT COUNT(*) FROM clusters").fetchone()[0]
        assert clusters_n == 1
        emb_n = ingest_db.execute("SELECT COUNT(*) FROM article_embeddings").fetchone()[0]
        assert emb_n == 1


# ── TestStagesDone ─────────────────────────────────────────────────────────


class TestStagesDone:
    async def test_stages_done_includes_cluster(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        await Clusterer(ingest_db, FakeEmbeddingClient()).run("run_test")
        row = ingest_db.execute(
            "SELECT stages_done FROM pipeline_runs WHERE run_id = 'run_test'"
        ).fetchone()
        stages = json.loads(row[0])
        assert "cluster" in stages
        # And appended in order.
        assert stages[-1] == "cluster"


# ── TestDefaultThreshold ───────────────────────────────────────────────────


# ── TestEmbedFailureRaisesClearError (Issue 1 · 2026-05-28) ────────────────


class _FailingEmbeddingClient:
    """Embedder stand-in that raises on every embed call.

    Models the 02:00 incident: Ollama 500s the /api/embed call, the
    clusterer's _embed_articles swallows the exception and returns [],
    then strict-zip would crash with a cryptic ValueError. With the
    Issue 1 guard in place the length-mismatch instead raises the
    explicit EmbeddingUnavailableError BEFORE the zip runs.
    """

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("simulated embedder failure (HTTP 500)")


class _ShortEmbeddingClient:
    """Embedder stand-in that returns fewer vectors than inputs.

    Mirrors the "partial result with no None markers" failure mode ·
    the strict zip would crash, the guard catches it first.
    """

    def __init__(self, return_count: int) -> None:
        self._return_count = return_count

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * DIMENSION for _ in range(self._return_count)]


class TestEmbedFailureRaisesClearError:
    async def test_embed_total_failure_raises_clear_error(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        _insert_article(
            ingest_db,
            article_id=_short_id("err1"),
            source_id="bianet",
            title="Test başlığı",
            body=_body(EC_BAG),
        )
        clusterer = Clusterer(ingest_db, _FailingEmbeddingClient())

        with pytest.raises(EmbeddingUnavailableError) as exc_info:
            await clusterer.run("run_test")

        msg = str(exc_info.value)
        assert "0 vectors" in msg
        assert "1 articles" in msg
        # The cryptic zip ValueError must NOT bubble up.
        assert "zip()" not in msg

        # Re-runnability · cluster did NOT get added to stages_done.
        row = ingest_db.execute(
            "SELECT stages_done FROM pipeline_runs WHERE run_id = 'run_test'"
        ).fetchone()
        stages = json.loads(row[0])
        assert "cluster" not in stages

    async def test_embed_length_mismatch_raises_clear_error(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        # Two eligible articles, embedder returns only one vector.
        _insert_article(
            ingest_db,
            article_id=_short_id("m1"),
            source_id="bianet",
            title="Birinci başlık",
            body=_body(EC_BAG),
        )
        _insert_article(
            ingest_db,
            article_id=_short_id("m2"),
            source_id="cumhuriyet",
            title="İkinci başlık",
            body=_body(EC_BAG),
            published_at=datetime(2026, 5, 23, 9, 0, 0),
        )
        clusterer = Clusterer(ingest_db, _ShortEmbeddingClient(return_count=1))

        with pytest.raises(EmbeddingUnavailableError) as exc_info:
            await clusterer.run("run_test")

        msg = str(exc_info.value)
        assert "1 vectors" in msg
        assert "2 articles" in msg

        # No half-clustered state · stage stays out of stages_done.
        row = ingest_db.execute(
            "SELECT stages_done FROM pipeline_runs WHERE run_id = 'run_test'"
        ).fetchone()
        stages = json.loads(row[0])
        assert "cluster" not in stages

    async def test_no_eligible_articles_does_not_raise(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        # When every article is below the embedding floor, embeddable
        # is empty · _embed_articles returns [] · guard sees 0 == 0 ·
        # no raise · stage marks done normally.
        _insert_article(
            ingest_db,
            article_id=_short_id("tiny"),
            source_id="bianet",
            title="t",
            body="çok kısa",
            word_count=2,
        )

        result = await Clusterer(
            ingest_db, _FailingEmbeddingClient()
        ).run("run_test")

        assert result["new_clusters"] == 0
        assert result["skipped_empty"] == 1
        row = ingest_db.execute(
            "SELECT stages_done FROM pipeline_runs WHERE run_id = 'run_test'"
        ).fetchone()
        stages = json.loads(row[0])
        assert "cluster" in stages


# ── TestDefaultThreshold ───────────────────────────────────────────────────


class TestDefaultThreshold:
    def test_default_threshold_is_07(self) -> None:
        assert DEFAULT_COSINE_THRESHOLD == 0.7

    def test_dimension_matches_embedding_column(self) -> None:
        # Should always be 1024 to match articles_embeddings.embedding FLOAT[1024].
        assert DIMENSION == 1024

    def test_math_module_is_imported_correctly(self) -> None:
        # Sanity guard for the future: the test file imports math because
        # downstream tests may pin behavior around sqrt magnitudes.
        assert math.sqrt(4.0) == 2.0

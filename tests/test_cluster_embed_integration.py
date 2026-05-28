"""Tier 2 · integration test for Solution B end-to-end against real code.

Wires the REAL :class:`OllamaEmbeddingClient` and the REAL
:class:`Clusterer` together with ONLY the httpx boundary mocked via
:class:`httpx.MockTransport`. The mock returns:

* HTTP 500 with body ``{"error":"...json: unsupported value: NaN"}``
  for the designated poison input string (matches the bge-m3 incident),
* HTTP 200 with a valid embedding for every other input.

The point of this tier · exercise the full
``retry → halving → single-item None → partial alignment`` chain
against PRODUCTION code paths, not fakes. The previous autonomous
session was unit-test-only and shipped a partial-discard bug
(``got: 0`` despite successful embeds) that only a live run exposed.
This integration test pins the contract end-to-end so that bug class
cannot recur.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Generator
from datetime import datetime
from pathlib import Path

import duckdb
import httpx
import pytest

from musahit.cluster.clusterer import Clusterer, EmbeddingUnavailableError
from musahit.cluster.embedder import DIMENSION, OllamaEmbeddingClient
from musahit.common.migrations import init_db
from musahit.ingest.sources import seed_sources

# The poison input that bge-m3 emits NaN for · matches the
# 2026-05-28 NaN-poison-article incident specimen. The "Sezon açıldı"
# title + a benign lead reproduces the exact "title + \n\n + lead"
# input shape the production clusterer constructs.
POISON_TITLE = "Sezon açıldı"
POISON_INPUT_SUBSTRING = POISON_TITLE  # any input containing this is the poison


def _short_id(seed: str) -> str:
    return hashlib.md5(seed.encode(), usedforsecurity=False).hexdigest()


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture()
def ingest_db(tmp_path: Path) -> Generator[duckdb.DuckDBPyConnection, None, None]:
    """Standalone DuckDB fixture matching tests/test_clusterer.py's shape."""
    db_path = tmp_path / "integration.duckdb"
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
            "run_integration",
            datetime(2026, 5, 28, 0, 0, 0),
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
    published_at: datetime = datetime(2026, 5, 28, 8, 0, 0),
    word_count: int | None = None,
) -> None:
    lead = body[:500]
    wc = word_count if word_count is not None else len(body.split())
    conn.execute(
        "INSERT OR IGNORE INTO ingest_log (run_id, source_id, started_at, "
        "completed_at, status, articles_fetched) VALUES (?, ?, ?, ?, ?, ?)",
        [
            "run_integration", source_id, datetime(2026, 5, 28),
            datetime(2026, 5, 28), "OK", 1,
        ],
    )
    conn.execute(
        """
        INSERT INTO articles (
            id, source_id, url, fetched_at, published_at,
            title, lead, body, language, entities, word_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            article_id, source_id, f"https://example.com/{article_id}",
            datetime(2026, 5, 28, 1, 0, 0), published_at,
            title, lead, body, language, json.dumps([]), wc,
        ],
    )


def _make_nan_500_transport() -> tuple[httpx.MockTransport, list[dict]]:
    """Return a MockTransport that 500s the poison input and 200s the rest.

    Also returns the captured-requests log so tests can assert on
    retry/halving behavior. The 500 body matches the literal Ollama
    error message from the incident specimen.
    """
    requests_log: list[dict] = []

    def responder(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        inputs = body["input"]
        requests_log.append({"size": len(inputs), "inputs": list(inputs)})
        # Any input containing the poison substring → 500.
        poison_present = any(
            POISON_INPUT_SUBSTRING in t for t in inputs
        )
        if poison_present:
            return httpx.Response(
                500,
                content=json.dumps({
                    "error": "failed to encode response: "
                             "json: unsupported value: NaN"
                }),
                headers={"content-type": "application/json"},
            )
        # 200 path · return a deterministic non-zero embedding per input.
        embeddings: list[list[float]] = []
        for i, _t in enumerate(inputs):
            # Use a position-derived non-zero pattern so each item has a
            # distinct vector but all pass _is_valid_vector.
            vec = [0.1] * DIMENSION
            vec[(i * 7) % DIMENSION] = 0.9
            embeddings.append(vec)
        return httpx.Response(
            200,
            content=json.dumps({"model": "bge-m3", "embeddings": embeddings}),
            headers={"content-type": "application/json"},
        )

    return httpx.MockTransport(responder), requests_log


def _make_httpx_client(transport: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport)


# ── Tests ──────────────────────────────────────────────────────────────────


class TestSolutionBIntegration:
    async def test_poison_article_dropped_others_clustered(
        self,
        ingest_db: duckdb.DuckDBPyConnection,
    ) -> None:
        """The full retry → halving → None → partial-alignment chain runs
        end-to-end against REAL OllamaEmbeddingClient + REAL Clusterer.

        Seed 5 articles, one with the poison title. The mock transport
        500s every batch containing the poison and 200s the rest. After
        the run:

        * the poison article id is in the cluster_embed_partial log
        * the other 4 articles' embeddings are persisted
        * the run COMPLETES (cluster in stages_done · no EmbeddingUnavailableError)
        * article_embeddings has exactly 4 rows
        * the dropped article has NO embedding row
        """
        poison_id = _short_id("poison_3efe17cd")
        good_ids = {
            _short_id(f"good_{i}"): f"good_{i}" for i in range(4)
        }

        # Bodies long enough that all 5 articles clear
        # MIN_WORD_COUNT_FOR_EMBEDDING (10) and the goods clear
        # MIN_WORD_COUNT_FOR_NEW_CLUSTER (30). The real poison from the
        # 2026-05-28 incident was wc=17 · use comparable length here.
        poison_body = (
            "Ünlü isimler bayramda yaz tatili sezonunu açtı bu hafta "
            "ailelerle birlikte sahil kasabalarında olmaya başladılar. "
            "Tatilciler özellikle Akdeniz bölgesini tercih etti ve otel "
            "rezervasyonları rekor seviyeye yükseldi haberlere göre."
        )
        good_body = (
            "Türkiye ekonomisi bugün yeni bir gelişme yaşadı ve "
            "enflasyon rakamları açıklandı. Detaylar bilinmiyor ancak "
            "yetkililer açıklama yaptı. Merkez bankası faiz kararını "
            "yarın paylaşacak ve piyasa beklentileri çeşitli senaryolar "
            "etrafında şekilleniyor son verilere göre uzmanlar yorum "
            "yaptı."
        )

        _insert_article(
            ingest_db,
            article_id=poison_id,
            source_id="bianet",
            title=POISON_TITLE,
            body=poison_body,
        )
        for i, gid in enumerate(good_ids):
            _insert_article(
                ingest_db,
                article_id=gid,
                source_id=("cumhuriyet" if i % 2 == 0 else "sabah"),
                title=f"Ekonomi haberi {i}",
                body=good_body,
                published_at=datetime(2026, 5, 28, 8 + i, 0, 0),
            )

        transport, requests_log = _make_nan_500_transport()
        client = _make_httpx_client(transport)
        embedder = OllamaEmbeddingClient(
            client=client,
            batch_size=8,  # 5 articles all fit one batch → halving exercised
            backoff_base_seconds=0.0,  # keep test fast
        )

        import structlog.testing
        with structlog.testing.capture_logs() as captured:
            result = await Clusterer(ingest_db, embedder).run("run_integration")

        # 1 · The run COMPLETED.
        row = ingest_db.execute(
            "SELECT stages_done, status FROM pipeline_runs "
            "WHERE run_id = 'run_integration'"
        ).fetchone()
        stages = json.loads(row[0])
        assert "cluster" in stages, "cluster stage must be marked done"

        # 2 · The retry → halving → single-item path actually ran.
        # The transport saw the poison batch at least once, then halves.
        captured_sizes = [r["size"] for r in requests_log]
        # Initial batch=5 (the original batch · 5 articles in one batch_size=8 slice)
        assert 5 in captured_sizes, (
            f"expected the original batch of size 5 to be POSTed once; "
            f"observed sizes: {captured_sizes}"
        )
        # Halving must have decomposed the failing batch toward singletons.
        assert 1 in captured_sizes, (
            f"expected the halving to reach a single-item batch (the "
            f"poison); observed sizes: {captured_sizes}"
        )

        # 3 · cluster_embed_partial log carries the poison id + dropped=1.
        partial_events = [
            e for e in captured if e.get("event") == "cluster_embed_partial"
        ]
        assert partial_events, (
            f"expected cluster_embed_partial event · captured events: "
            f"{[e.get('event') for e in captured]}"
        )
        partial = partial_events[0]
        assert partial.get("dropped") == 1
        assert partial.get("clustered") == 4
        assert poison_id in (partial.get("dropped_ids") or [])

        # 4 · article_embeddings has exactly 4 rows · NaN article omitted.
        rows = ingest_db.execute(
            "SELECT article_id FROM article_embeddings"
        ).fetchall()
        embedded_ids = {r[0] for r in rows}
        assert len(embedded_ids) == 4
        assert poison_id not in embedded_ids
        for gid in good_ids:
            assert gid in embedded_ids

        # 5 · No NaN/Inf in the persisted embeddings (defensive net).
        emb_rows = ingest_db.execute(
            "SELECT article_id, embedding FROM article_embeddings"
        ).fetchall()
        for aid, embedding in emb_rows:
            assert embedding is not None, f"NULL embedding for {aid}"
            assert len(embedding) == DIMENSION
            for x in embedding:
                # DuckDB returns floats · NaN is not equal to itself.
                assert x == x, f"NaN in persisted embedding for {aid}"

        # 6 · No EmbeddingUnavailableError propagated (would have been a
        # raise above · just pin the success-path return).
        assert result is not None
        assert isinstance(result, dict)
        # At least one cluster was created from the 4 good articles ·
        # the precise count depends on cosine threshold but must be ≥ 1.
        assert result["new_clusters"] >= 1

    async def test_total_outage_still_raises_clean_error(
        self,
        ingest_db: duckdb.DuckDBPyConnection,
    ) -> None:
        """The Issue-1 contract survives Solution B end-to-end: if the
        httpx transport fails entirely (Ollama process down), the
        embedder's wrapper returns [] and the clusterer raises a clean
        EmbeddingUnavailableError. No partial alignment kicks in
        because there are no Nones · the per-item path is for per-item
        model failures, not transport outages."""
        _insert_article(
            ingest_db,
            article_id=_short_id("any"),
            source_id="bianet",
            title="başlık",
            body=(
                "Türkiye ekonomisi bugün yeni bir gelişme yaşadı ve "
                "enflasyon rakamları açıklandı."
            ),
        )

        def transport_fail(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        transport = httpx.MockTransport(transport_fail)
        client = _make_httpx_client(transport)
        embedder = OllamaEmbeddingClient(
            client=client,
            batch_size=4,
            backoff_base_seconds=0.0,
        )

        with pytest.raises(EmbeddingUnavailableError):
            await Clusterer(ingest_db, embedder).run("run_integration")

        # Cluster NOT in stages_done · re-runnable.
        row = ingest_db.execute(
            "SELECT stages_done FROM pipeline_runs "
            "WHERE run_id = 'run_integration'"
        ).fetchone()
        stages = json.loads(row[0])
        assert "cluster" not in stages

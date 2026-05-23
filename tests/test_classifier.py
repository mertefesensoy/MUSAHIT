"""Integration tests for musahit.score.classifier.Classifier.

The fixture mirrors the cluster-stage fixture: in-memory DuckDB with
schema applied, sources seeded, a parent pipeline_runs row, and a small
set of articles + clusters pre-inserted. The classifier reads them,
calls the injected FakeLlmClient, and writes back to the clusters /
promotion_log tables.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from datetime import datetime
from pathlib import Path

import duckdb
import pytest

from musahit.common.migrations import init_db
from musahit.common.types import Category, Confidence
from musahit.ingest.sources import seed_sources
from musahit.score.classifier import Classifier
from musahit.score.defcon import DEFCON
from musahit.score.llm_client import FakeLlmClient

RUN_ID = "run_test"
# Cluster creation date — used by every helper insert.
CLUSTER_DATE = datetime(2026, 5, 23, 8, 0, 0)
# Place the system's first run well before CLUSTER_DATE so the bootstrap
# window (7 days from first run) has elapsed by the time the cluster is
# created. Tests that need to exercise bootstrap demotion seed an earlier
# pipeline_runs row of their own.
PRE_BOOTSTRAP_FIRST_RUN = datetime(2026, 1, 1, 0, 0, 0)


# ── Fixture ────────────────────────────────────────────────────────────────


@pytest.fixture()
def db_with_clusters(
    tmp_path: Path,
) -> Generator[duckdb.DuckDBPyConnection, None, None]:
    db_path = tmp_path / "test.duckdb"
    init_db(db_path, load_vss=False)
    conn = duckdb.connect(str(db_path))
    seed_sources(conn)
    # Establish a "first run" well before the cluster date so the default
    # case is OUT of the bootstrap window. Per-test overrides (the
    # bootstrap test seeds an earlier pipeline_runs row) bring the window
    # in if needed.
    conn.execute(
        """
        INSERT INTO pipeline_runs (
            run_id, started_at, status, stages_done, counts
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [
            "run_first",
            PRE_BOOTSTRAP_FIRST_RUN,
            "COMPLETED",
            json.dumps(["ingest", "normalize", "cluster", "score", "arc", "write"]),
            json.dumps({}),
        ],
    )
    conn.execute(
        """
        INSERT INTO pipeline_runs (
            run_id, started_at, status, stages_done, counts
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [
            RUN_ID,
            CLUSTER_DATE,
            "RUNNING",
            json.dumps(["ingest", "normalize", "cluster"]),
            json.dumps({}),
        ],
    )
    try:
        yield conn
    finally:
        conn.close()


def _seed_cluster_with_articles(
    conn: duckdb.DuckDBPyConnection,
    cluster_id: str,
    source_ids: list[str],
    created_at: datetime = CLUSTER_DATE,
) -> None:
    """Insert a cluster + N articles + N ingest_log + N cluster_articles rows."""
    conn.execute(
        """
        INSERT INTO clusters (
            id, created_at, headline, summary, category,
            raw_defcon, ceiling_defcon, final_defcon, confidence,
            bands_present, arc_id, operator_override
        )
        VALUES (?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)
        """,
        [cluster_id, created_at],
    )
    for i, source_id in enumerate(source_ids):
        article_id = f"{cluster_id}_a{i}"
        conn.execute(
            """
            INSERT OR IGNORE INTO ingest_log (
                run_id, source_id, started_at, completed_at, status, articles_fetched
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [RUN_ID, source_id, created_at, created_at, "OK", 1],
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
                created_at,
                created_at,
                f"Title from {source_id}",
                f"Lead text from {source_id}, long enough to matter.",
                "Body.",
                "tr",
                json.dumps([]),
                40,
            ],
        )
        conn.execute(
            """
            INSERT INTO cluster_articles (cluster_id, article_id)
            VALUES (?, ?)
            """,
            [cluster_id, article_id],
        )


def _valid_worker_json(
    defcon: int = 3,
    category: str = "POLİTİKA",
    confidence_self: str = "medium",
    headline: str = "Test başlığı",
    summary: str = "Test özeti.",
) -> str:
    return json.dumps(
        {
            "defcon": defcon,
            "category": category,
            "confidence_self": confidence_self,
            "entities": ["Türkiye"],
            "summary": summary,
            "headline": headline,
        },
        ensure_ascii=False,
    )


# ── TestHappyPath ──────────────────────────────────────────────────────────


class TestHappyPath:
    async def test_one_cluster_scored_end_to_end(
        self, db_with_clusters: duckdb.DuckDBPyConnection
    ) -> None:
        _seed_cluster_with_articles(
            db_with_clusters,
            cluster_id="cl_20260523_0001",
            source_ids=["bianet", "cumhuriyet", "sabah"],  # 2 sides
        )
        llm = FakeLlmClient(default=_valid_worker_json(defcon=2))

        result = await Classifier(db_with_clusters, llm).run(RUN_ID)

        assert result == {"scored": 1, "fallbacks": 0, "errors": 0}
        row = db_with_clusters.execute(
            """
            SELECT raw_defcon, ceiling_defcon, final_defcon,
                   confidence, headline, summary, category, bands_present
              FROM clusters WHERE id = 'cl_20260523_0001'
            """
        ).fetchone()
        raw, ceiling, final, conf, headline, summary, cat, bands_json = row
        assert raw == int(DEFCON.SEVERE)
        # 3 bands across 2 sides (bianet=independent → neutral, cumhuriyet=opposition,
        # sabah=gov_aligned → gov) → 3 sides → ACUTE ceiling.
        assert ceiling == int(DEFCON.ACUTE)
        # max(raw=2, ceiling=1) = 2 (SEVERE). Worker's claim stands; the
        # ACUTE ceiling permits more severe but doesn't force it.
        assert final == int(DEFCON.SEVERE)
        # confidence: 2+ sides BUT only 3 sources → ORTA (needs 4+ for YUKSEK).
        assert conf == Confidence.ORTA.value
        assert headline == "Test başlığı"
        assert summary == "Test özeti."
        assert cat == Category.POLITIKA.value
        bands = set(json.loads(bands_json))
        assert {"independent", "opposition", "gov_aligned"} == bands

    async def test_promotion_log_row_written(
        self, db_with_clusters: duckdb.DuckDBPyConnection
    ) -> None:
        _seed_cluster_with_articles(
            db_with_clusters,
            cluster_id="cl_20260523_0002",
            source_ids=["bianet"],
        )
        llm = FakeLlmClient(default=_valid_worker_json(defcon=3))
        await Classifier(db_with_clusters, llm).run(RUN_ID)

        log_row = db_with_clusters.execute(
            """
            SELECT raw_defcon, ceiling_defcon, final_defcon,
                   bands_present, sides_present, confidence, rule_applied,
                   computed_at
              FROM promotion_log WHERE cluster_id = 'cl_20260523_0002'
            """
        ).fetchone()
        assert log_row is not None
        raw, ceil, final, bands, sides, conf, rule, computed = log_row
        assert raw == int(DEFCON.MATERIAL)
        assert ceil == int(DEFCON.ROUTINE)  # single band → ROUTINE ceiling
        # max(raw=3, ceiling=4) = 4 → ROUTINE. Worker says MATERIAL but a
        # single band can only support ROUTINE; ceiling caps severity.
        # First pipeline run is months before cluster_date so no bootstrap.
        assert final == int(DEFCON.ROUTINE)
        assert rule == "single_band"
        assert json.loads(bands) == ["independent"]
        assert "neutral" in json.loads(sides)
        assert conf == Confidence.DUSUK.value
        assert computed is not None


# ── TestRetryOnMalformedJson ───────────────────────────────────────────────


class TestRetryOnMalformedJson:
    async def test_malformed_first_attempt_then_valid_succeeds(
        self, db_with_clusters: duckdb.DuckDBPyConnection
    ) -> None:
        _seed_cluster_with_articles(
            db_with_clusters,
            cluster_id="cl_20260523_0003",
            source_ids=["bianet"],
        )

        def responder(_prompt: str, attempt: int) -> str:
            if attempt == 0:
                return "not json at all"
            return _valid_worker_json(defcon=4)

        llm = FakeLlmClient(responder=responder)
        result = await Classifier(db_with_clusters, llm).run(RUN_ID)
        assert result["scored"] == 1
        assert result["fallbacks"] == 0
        row = db_with_clusters.execute(
            "SELECT raw_defcon FROM clusters WHERE id = 'cl_20260523_0003'"
        ).fetchone()
        assert row[0] == int(DEFCON.ROUTINE)

    async def test_all_retries_fail_uses_fallback(
        self, db_with_clusters: duckdb.DuckDBPyConnection
    ) -> None:
        _seed_cluster_with_articles(
            db_with_clusters,
            cluster_id="cl_20260523_0004",
            source_ids=["bianet"],
        )

        def responder(_prompt: str, _attempt: int) -> str:
            return "irrecoverable garbage"

        llm = FakeLlmClient(responder=responder)
        result = await Classifier(db_with_clusters, llm).run(RUN_ID)

        assert result["scored"] == 1
        assert result["fallbacks"] == 1
        row = db_with_clusters.execute(
            """
            SELECT raw_defcon, ceiling_defcon, final_defcon, category, confidence
              FROM clusters WHERE id = 'cl_20260523_0004'
            """
        ).fetchone()
        raw, ceil, final, cat, conf = row
        # Fallback shape: AMBIENT + UNCLASSIFIED + low.
        assert raw == int(DEFCON.AMBIENT)
        assert cat == Category.UNCLASSIFIED.value
        # raw=5 (AMBIENT), single band ceiling=4 (ROUTINE), max(5,4) = 5 (AMBIENT).
        # The ceiling does not escalate worker-noise to routine — fallback
        # noise stays noise. No bootstrap demotion applies (already at AMBIENT).
        assert final == int(DEFCON.AMBIENT)


# ── TestBootstrapDemotion ──────────────────────────────────────────────────


class TestBootstrapDemotion:
    async def test_cluster_within_bootstrap_window_demoted_by_one(
        self, db_with_clusters: duckdb.DuckDBPyConnection
    ) -> None:
        # Remove the early-2026 first-run row so MIN(pipeline_runs.started_at)
        # is CLUSTER_DATE (from the run_test row). Cluster is created the same
        # day → inside the 7-day window → demotion fires.
        db_with_clusters.execute(
            "DELETE FROM pipeline_runs WHERE run_id = 'run_first'"
        )
        _seed_cluster_with_articles(
            db_with_clusters,
            cluster_id="cl_20260523_0005",
            source_ids=["sabah", "bianet", "cumhuriyet"],  # 3 bands, 2 sides
        )
        llm = FakeLlmClient(default=_valid_worker_json(defcon=1))
        await Classifier(db_with_clusters, llm).run(RUN_ID)

        row = db_with_clusters.execute(
            "SELECT raw_defcon, ceiling_defcon, final_defcon FROM clusters "
            "WHERE id = 'cl_20260523_0005'"
        ).fetchone()
        raw, ceiling, final = row
        # raw=1 (ACUTE), ceiling=1 (ACUTE per 3 bands 2 sides), max(1,1)=1.
        # Bootstrap demotion: 1 → 2 (SEVERE). Final unchanged from the
        # min→max formula switch in this case because raw and ceiling are equal.
        assert raw == int(DEFCON.ACUTE)
        assert ceiling == int(DEFCON.ACUTE)
        assert final == int(DEFCON.SEVERE)


# ── TestIdempotence ────────────────────────────────────────────────────────


class TestIdempotence:
    async def test_rerun_does_not_rescore_already_scored_clusters(
        self, db_with_clusters: duckdb.DuckDBPyConnection
    ) -> None:
        _seed_cluster_with_articles(
            db_with_clusters,
            cluster_id="cl_20260523_0006",
            source_ids=["bianet"],
        )
        llm = FakeLlmClient(default=_valid_worker_json(defcon=4))

        first = await Classifier(db_with_clusters, llm).run(RUN_ID)
        second = await Classifier(db_with_clusters, llm).run(RUN_ID)

        assert first["scored"] == 1
        # Second pass sees no pending clusters (final_defcon IS NOT NULL).
        assert second["scored"] == 0

        # Still exactly one promotion_log row, matching the cluster_id PK.
        log_count = db_with_clusters.execute(
            "SELECT COUNT(*) FROM promotion_log WHERE cluster_id = 'cl_20260523_0006'"
        ).fetchone()[0]
        assert log_count == 1


# ── TestStagesDone ─────────────────────────────────────────────────────────


class TestStagesDone:
    async def test_stages_done_appends_score(
        self, db_with_clusters: duckdb.DuckDBPyConnection
    ) -> None:
        _seed_cluster_with_articles(
            db_with_clusters,
            cluster_id="cl_20260523_0007",
            source_ids=["bianet"],
        )
        llm = FakeLlmClient(default=_valid_worker_json())
        await Classifier(db_with_clusters, llm).run(RUN_ID)

        row = db_with_clusters.execute(
            "SELECT stages_done, counts FROM pipeline_runs WHERE run_id = ?",
            [RUN_ID],
        ).fetchone()
        stages = json.loads(row[0])
        counts = json.loads(row[1])
        assert "score" in stages
        assert stages[-1] == "score"
        assert counts.get("clusters_scored") == 1


# ── TestBandAggregation ────────────────────────────────────────────────────


class TestBandAggregation:
    async def test_primary_source_yields_unthinkable_ceiling(
        self, db_with_clusters: duckdb.DuckDBPyConnection
    ) -> None:
        _seed_cluster_with_articles(
            db_with_clusters,
            cluster_id="cl_20260523_0008",
            source_ids=["resmi_gazete"],  # primary_gov band
        )
        llm = FakeLlmClient(default=_valid_worker_json(defcon=2))
        await Classifier(db_with_clusters, llm).run(RUN_ID)
        row = db_with_clusters.execute(
            "SELECT ceiling_defcon FROM clusters WHERE id = 'cl_20260523_0008'"
        ).fetchone()
        assert row[0] == int(DEFCON.UNTHINKABLE)

"""Tests for the score-stage fallback placeholder (2026-05-25 fix).

The classifier's ``_FALLBACK_RESPONSE`` previously held ``headline=""`` and
``summary=""``, which propagated through ``_seed_arc`` to produce arcs that
rendered as ``(başlıksız)`` in the briefing. The 2026-05-25 fix replaces
both with non-empty Turkish placeholders so the operator hears (and sees)
a recognisable "classification failed" signal rather than a generic
"untitled" filler.

See ``docs/investigations/2026-05-25-empty-headlines.md`` for root cause and
``docs/implementations/2026-05-25-empty-headline-fix.md`` for the fix.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from datetime import datetime
from pathlib import Path

import duckdb
import pytest

from musahit.common.migrations import init_db
from musahit.common.types import Category
from musahit.ingest.sources import seed_sources
from musahit.score.classifier import _FALLBACK_RESPONSE, Classifier
from musahit.score.defcon import DEFCON
from musahit.score.llm_client import FakeLlmClient

RUN_ID = "run_test"
CLUSTER_DATE = datetime(2026, 5, 25, 8, 0, 0)
PRE_BOOTSTRAP_FIRST_RUN = datetime(2026, 1, 1, 0, 0, 0)


# ── Pinned placeholder text ────────────────────────────────────────────────


EXPECTED_HEADLINE = "(sınıflandırılamadı)"
EXPECTED_SUMMARY = (
    "Skorlama modeli bu kümede geçerli yanıt üretemedi. "
    "Operatör incelemesi bekliyor."
)


class TestFallbackResponseShape:
    """The static ``_FALLBACK_RESPONSE`` instance carries the placeholders.

    These assertions pin the EXACT text · changing the strings is a UX
    decision (the operator hears these words in the audio) and must be
    accompanied by a test update.
    """

    def test_headline_is_pinned_placeholder(self) -> None:
        assert _FALLBACK_RESPONSE.headline == EXPECTED_HEADLINE

    def test_summary_is_pinned_placeholder(self) -> None:
        assert _FALLBACK_RESPONSE.summary == EXPECTED_SUMMARY

    def test_headline_non_empty(self) -> None:
        # Defence: the pre-2026-05-25 bug was specifically empty strings.
        # Guarantee that any future placeholder change keeps non-empty.
        assert _FALLBACK_RESPONSE.headline.strip() != ""
        assert _FALLBACK_RESPONSE.summary.strip() != ""

    def test_defcon_stays_ambient(self) -> None:
        """Fallback still routes to AMBIENT · the placeholder is text-only,
        the severity treatment is unchanged."""
        assert _FALLBACK_RESPONSE.defcon == int(DEFCON.AMBIENT)

    def test_category_stays_unclassified(self) -> None:
        assert _FALLBACK_RESPONSE.category == Category.UNCLASSIFIED

    def test_confidence_self_low(self) -> None:
        """``confidence_self`` is structurally required by
        :class:`WorkerResponse` but is not consumed by ``_persist`` ·
        the persisted ``confidence`` column comes from
        :func:`promotion.confidence`. The literal "low" is correct in
        spirit (the worker failed) and pinned here so a casual edit to
        the dataclass would surface."""
        assert _FALLBACK_RESPONSE.confidence_self == "low"

    def test_headline_within_pydantic_max_length(self) -> None:
        # WorkerResponse.headline has max_length=200, summary max_length=500.
        # The placeholder must not silently violate the pydantic bound.
        assert len(_FALLBACK_RESPONSE.headline) <= 200
        assert len(_FALLBACK_RESPONSE.summary) <= 500


# ── Fixture mirrors tests/test_classifier.py ───────────────────────────────


@pytest.fixture()
def db_with_clusters(
    tmp_path: Path,
) -> Generator[duckdb.DuckDBPyConnection, None, None]:
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
            "run_first",
            PRE_BOOTSTRAP_FIRST_RUN,
            "COMPLETED",
            json.dumps(["ingest", "normalize", "cluster", "score", "arc-link", "write"]),
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


def _seed_unscored_cluster(
    conn: duckdb.DuckDBPyConnection, cluster_id: str, source_id: str = "bianet"
) -> None:
    """Single-article cluster with NULL DEFCON · score stage will process it."""
    conn.execute(
        """
        INSERT INTO clusters (
            id, created_at, headline, summary, category,
            raw_defcon, ceiling_defcon, final_defcon, confidence,
            bands_present, arc_id, operator_override
        )
        VALUES (?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)
        """,
        [cluster_id, CLUSTER_DATE],
    )
    conn.execute(
        """
        INSERT INTO ingest_log (
            run_id, source_id, started_at, completed_at, status, articles_fetched
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [RUN_ID, source_id, CLUSTER_DATE, CLUSTER_DATE, "OK", 1],
    )
    article_id = f"art_{cluster_id}"
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
            f"https://example.com/{cluster_id}",
            CLUSTER_DATE,
            CLUSTER_DATE,
            f"T-{cluster_id}",
            f"L-{cluster_id}",
            "B",
            "tr",
            json.dumps([]),
            50,
        ],
    )
    conn.execute(
        "INSERT INTO cluster_articles (cluster_id, article_id) VALUES (?, ?)",
        [cluster_id, article_id],
    )


class TestFallbackPersistsPlaceholderToCluster:
    """End-to-end: LLM fails repeatedly → cluster row carries the placeholder.

    This is the integration that the arc-link filter + the renderer both
    rely on: the score stage must produce a non-empty headline so the
    cluster passes ``_select_pending``'s ``coalesce(trim(headline),'')
    != ''`` guard."""

    async def test_irrecoverable_garbage_writes_placeholder_headline(
        self, db_with_clusters: duckdb.DuckDBPyConnection
    ) -> None:
        _seed_unscored_cluster(db_with_clusters, "cl_fallback_target")

        def responder(_prompt: str, _attempt: int) -> str:
            return "this is not JSON · keep failing"

        llm = FakeLlmClient(responder=responder)
        result = await Classifier(db_with_clusters, llm).run(RUN_ID)

        assert result == {"scored": 1, "fallbacks": 1, "errors": 0}

        row = db_with_clusters.execute(
            "SELECT headline, summary, category, final_defcon "
            "FROM clusters WHERE id = 'cl_fallback_target'"
        ).fetchone()
        headline, summary, category, final = row
        assert headline == EXPECTED_HEADLINE
        assert summary == EXPECTED_SUMMARY
        assert category == Category.UNCLASSIFIED.value
        assert final == int(DEFCON.AMBIENT)

    async def test_placeholder_headline_passes_linker_filter_predicate(
        self, db_with_clusters: duckdb.DuckDBPyConnection
    ) -> None:
        """Sanity guard: the placeholder is non-empty after ``trim`` ·
        i.e. ``coalesce(trim(c.headline), '') != ''`` is True · so the
        arc-link stage WILL pick up fallback rows. This documents the
        intentional interaction between Option A (linker filter) and
        Option B (non-empty placeholder)."""
        # The predicate is encoded in the placeholder constant; the
        # linker SQL re-applies it. We test the predicate directly here
        # so a future placeholder edit that re-introduces whitespace-
        # only text would surface."""
        assert EXPECTED_HEADLINE.strip() != ""

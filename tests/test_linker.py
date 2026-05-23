"""Integration tests for musahit.arcs.linker.ArcLinker.

DB fixture mirrors the cluster/score-stage fixtures: in-memory DuckDB
with schema + sources + pipeline_runs + sample clusters with
embeddings and entities. No Ollama call (embeddings are read straight
from the DB). The cluster embeddings here are pure-Python vectors —
similarity is computed via the same arithmetic the production code
uses.
"""

from __future__ import annotations

import json
import math
from collections.abc import Generator
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from musahit.arcs.linker import ArcLinker
from musahit.common.migrations import init_db
from musahit.common.types import ArcState
from musahit.ingest.sources import seed_sources
from musahit.score.defcon import DEFCON

RUN_ID = "run_test"
NOW = datetime(2026, 5, 23, 12, 0, 0)
# The arc_centroids.centroid column is FLOAT[1024] (fixed length) so every
# vector inserted into the DB must be exactly 1024-dim. We use one-hot
# unit vectors at different indices to control cosine similarity.
DIM = 1024


# ── Fixture ────────────────────────────────────────────────────────────────


@pytest.fixture()
def db(tmp_path: Path) -> Generator[duckdb.DuckDBPyConnection, None, None]:
    db_path = tmp_path / "x.duckdb"
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
            RUN_ID,
            NOW,
            "RUNNING",
            json.dumps(["ingest", "normalize", "cluster", "score"]),
            json.dumps({}),
        ],
    )
    try:
        yield conn
    finally:
        conn.close()


# ── Test helpers ───────────────────────────────────────────────────────────


def _norm_vec(values: list[float]) -> list[float]:
    pad = values + [0.0] * (DIM - len(values))
    n = math.sqrt(sum(x * x for x in pad))
    if n == 0:
        return pad
    return [x / n for x in pad]


def _unit(idx: int) -> list[float]:
    """Unit vector with a 1.0 at ``idx`` (the dim is :data:`DIM`)."""
    v = [0.0] * DIM
    v[idx] = 1.0
    return v


def _insert_article(
    conn: duckdb.DuckDBPyConnection,
    *,
    article_id: str,
    source_id: str,
    entities: list[str],
    published_at: datetime = NOW,
) -> None:
    """Insert a minimal article with its source's ingest_log row."""
    conn.execute(
        """
        INSERT OR IGNORE INTO ingest_log (
            run_id, source_id, started_at, completed_at, status, articles_fetched
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [RUN_ID, source_id, published_at, published_at, "OK", 1],
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
            published_at,
            published_at,
            f"T-{article_id}",
            f"L-{article_id}",
            "B",
            "tr",
            json.dumps([{"text": e, "type": "PERSON"} for e in entities]),
            40,
        ],
    )


def _insert_cluster(
    conn: duckdb.DuckDBPyConnection,
    *,
    cluster_id: str,
    final_defcon: int,
    centroid: list[float],
    article_ids: list[str],
    created_at: datetime = NOW,
    headline: str = "",
    summary: str = "",
) -> None:
    """Insert a scored cluster + its embedding + its membership rows."""
    conn.execute(
        """
        INSERT INTO clusters (
            id, created_at, headline, summary, category,
            raw_defcon, ceiling_defcon, final_defcon, confidence,
            bands_present, arc_id, operator_override
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
        """,
        [
            cluster_id,
            created_at,
            headline or f"H-{cluster_id}",
            summary or f"S-{cluster_id}",
            "POLİTİKA",
            final_defcon,
            final_defcon,
            final_defcon,
            "ORTA",
            json.dumps(["independent"]),
        ],
    )
    conn.execute(
        """
        INSERT INTO cluster_embeddings (cluster_id, centroid, embedded_at)
        VALUES (?, ?, ?)
        """,
        [cluster_id, centroid, created_at],
    )
    for article_id in article_ids:
        conn.execute(
            "INSERT INTO cluster_articles (cluster_id, article_id) VALUES (?, ?)",
            [cluster_id, article_id],
        )


def _insert_arc(
    conn: duckdb.DuckDBPyConnection,
    *,
    arc_id: str,
    state: ArcState,
    centroid: list[float],
    entity_set: list[str],
    last_update_at: datetime,
    peak_defcon: int = int(DEFCON.MATERIAL),
) -> None:
    conn.execute(
        """
        INSERT INTO arcs (
            id, created_at, headline, summary, state, last_update_at,
            category, peak_defcon, entity_set
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            arc_id,
            last_update_at - timedelta(days=1),
            f"H-{arc_id}",
            f"S-{arc_id}",
            state.value,
            last_update_at,
            "POLİTİKA",
            peak_defcon,
            json.dumps(entity_set),
        ],
    )
    conn.execute(
        """
        INSERT INTO arc_centroids (arc_id, centroid, updated_at)
        VALUES (?, ?, ?)
        """,
        [arc_id, centroid, last_update_at],
    )


# ── TestClusterMatchesExistingArc ──────────────────────────────────────────


class TestClusterMatchesExistingArc:
    async def test_match_sets_arc_id_and_updates_arc(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        # Existing OPEN arc about İmamoğlu.
        arc_vec = _unit(0)
        _insert_arc(
            db,
            arc_id="arc_20260518_0001",
            state=ArcState.OPEN,
            centroid=arc_vec,
            entity_set=["İmamoğlu", "İstanbul Büyükşehir", "Yargıtay"],
            last_update_at=NOW - timedelta(days=2),
            peak_defcon=int(DEFCON.MATERIAL),
        )
        # New cluster about the same story.
        _insert_article(
            db,
            article_id="a1",
            source_id="cumhuriyet",
            entities=["İmamoğlu", "İstanbul Büyükşehir"],
        )
        _insert_cluster(
            db,
            cluster_id="cl_20260523_0001",
            final_defcon=int(DEFCON.SEVERE),
            centroid=arc_vec,  # identical centroid → cosine=1
            article_ids=["a1"],
        )

        result = await ArcLinker(db).run(RUN_ID)
        assert result["joined"] == 1
        assert result["seeded"] == 0

        # Cluster now points at the arc.
        cluster_row = db.execute(
            "SELECT arc_id FROM clusters WHERE id = 'cl_20260523_0001'"
        ).fetchone()
        assert cluster_row[0] == "arc_20260518_0001"

        # Arc state preserved; peak_defcon promoted to MORE severe (lower int).
        arc_row = db.execute(
            "SELECT state, peak_defcon, last_update_at FROM arcs "
            "WHERE id = 'arc_20260518_0001'"
        ).fetchone()
        state_val, peak, last_update = arc_row
        assert state_val == ArcState.OPEN.value
        # peak update: min(MATERIAL=3, SEVERE=2) = SEVERE=2 because lower int = more severe.
        assert peak == int(DEFCON.SEVERE)
        # last_update_at refreshed to the cluster's created_at.
        assert last_update == NOW

    async def test_fk_workaround_preserves_cluster_membership(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        arc_vec = _unit(1)
        _insert_arc(
            db,
            arc_id="arc_20260518_0002",
            state=ArcState.OPEN,
            centroid=arc_vec,
            entity_set=["Erdoğan", "Saray"],
            last_update_at=NOW - timedelta(days=1),
        )
        _insert_article(db, article_id="art1", source_id="bianet",
                        entities=["Erdoğan", "Saray"])
        _insert_article(db, article_id="art2", source_id="diken",
                        entities=["Erdoğan", "Saray"])
        _insert_cluster(
            db,
            cluster_id="cl_with_two_members",
            final_defcon=int(DEFCON.MATERIAL),
            centroid=arc_vec,
            article_ids=["art1", "art2"],
        )

        await ArcLinker(db).run(RUN_ID)

        # cluster_articles preserved (FK workaround re-INSERTs them).
        members = [
            r[0]
            for r in db.execute(
                "SELECT article_id FROM cluster_articles "
                "WHERE cluster_id = 'cl_with_two_members' ORDER BY article_id"
            ).fetchall()
        ]
        assert members == ["art1", "art2"]

        # cluster_embeddings still present.
        emb_row = db.execute(
            "SELECT centroid FROM cluster_embeddings WHERE cluster_id = 'cl_with_two_members'"
        ).fetchone()
        assert emb_row is not None


# ── TestWatchReturnsToOpen ─────────────────────────────────────────────────


class TestWatchReturnsToOpen:
    async def test_watch_arc_with_matching_cluster_returns_to_open(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        arc_vec = _unit(2)
        _insert_arc(
            db,
            arc_id="arc_watch_one",
            state=ArcState.WATCH,
            centroid=arc_vec,
            entity_set=["KAP", "Borsa"],
            last_update_at=NOW - timedelta(days=10),
        )
        _insert_article(db, article_id="kap1", source_id="bianet",
                        entities=["KAP", "Borsa"])
        _insert_cluster(
            db,
            cluster_id="cl_kap_today",
            final_defcon=int(DEFCON.MATERIAL),
            centroid=arc_vec,
            article_ids=["kap1"],
        )

        await ArcLinker(db).run(RUN_ID)
        state_val = db.execute(
            "SELECT state FROM arcs WHERE id = 'arc_watch_one'"
        ).fetchone()[0]
        assert state_val == ArcState.OPEN.value


# ── TestNoMatchSeedsArc ────────────────────────────────────────────────────


class TestNoMatchSeedsArc:
    async def test_unmatched_cluster_creates_new_arc(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        # No existing arcs at all.
        _insert_article(
            db,
            article_id="newart",
            source_id="diken",
            entities=["AYM", "Anayasa Mahkemesi Kararı"],
        )
        _insert_cluster(
            db,
            cluster_id="cl_new_topic",
            final_defcon=int(DEFCON.SEVERE),
            centroid=_unit(3),
            article_ids=["newart"],
            headline="AYM kararı",
            summary="Anayasa Mahkemesi yeni karar.",
        )

        result = await ArcLinker(db).run(RUN_ID)
        assert result["seeded"] == 1
        assert result["joined"] == 0

        rows = db.execute("SELECT id, state, peak_defcon FROM arcs").fetchall()
        assert len(rows) == 1
        arc_id, state_val, peak = rows[0]
        assert arc_id.startswith("arc_")
        assert state_val == ArcState.OPEN.value
        assert peak == int(DEFCON.SEVERE)
        # Cluster now points at the new arc.
        cluster_row = db.execute(
            "SELECT arc_id FROM clusters WHERE id = 'cl_new_topic'"
        ).fetchone()
        assert cluster_row[0] == arc_id


# ── TestSeverityOrdering ───────────────────────────────────────────────────


class TestSeverityOrdering:
    async def test_more_severe_cluster_seeds_arc_before_less_severe(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        # Two clusters, similar centroids and entities. The MORE severe
        # one (DEFCON 1 = ACUTE) should be processed first and seed the
        # arc; the other (DEFCON 4 = ROUTINE) then matches into it.
        shared = _unit(4)
        _insert_article(db, article_id="severe_art", source_id="bianet",
                        entities=["İmamoğlu", "Yargıtay", "Karar"])
        _insert_article(db, article_id="routine_art", source_id="diken",
                        entities=["İmamoğlu", "Yargıtay"])

        # Insert the routine cluster first so processing order is
        # determined by the query's ORDER BY, not by insert order.
        _insert_cluster(
            db,
            cluster_id="cl_routine",
            final_defcon=int(DEFCON.ROUTINE),  # 4
            centroid=shared,
            article_ids=["routine_art"],
            headline="Routine update",
        )
        _insert_cluster(
            db,
            cluster_id="cl_severe",
            final_defcon=int(DEFCON.ACUTE),  # 1
            centroid=shared,
            article_ids=["severe_art"],
            headline="Acute development",
        )

        result = await ArcLinker(db).run(RUN_ID)
        assert result["seeded"] == 1
        assert result["joined"] == 1

        # Both clusters now share the same arc id; arc's headline came
        # from the severe seed cluster.
        arc_ids = {
            r[0]
            for r in db.execute(
                "SELECT arc_id FROM clusters WHERE id IN ('cl_severe', 'cl_routine')"
            ).fetchall()
        }
        assert len(arc_ids) == 1
        arc_id = next(iter(arc_ids))
        headline = db.execute(
            "SELECT headline FROM arcs WHERE id = ?", [arc_id]
        ).fetchone()[0]
        assert headline == "Acute development"


# ── TestStopwordOnlyOverlapDoesNotLink ─────────────────────────────────────


class TestStopwordOnlyOverlap:
    async def test_arcs_sharing_only_stopwords_do_not_link(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        arc_vec = _unit(5)
        # Arc with stopwords-only entity set after filtering on the linker side.
        # Note: arc_centroids stores whatever entities the seeding cluster had
        # post-filter; we simulate an arc that genuinely has no signal entities
        # by setting entity_set to stopwords-only.
        _insert_arc(
            db,
            arc_id="arc_stopwords_only",
            state=ArcState.OPEN,
            centroid=arc_vec,
            entity_set=["Türkiye", "Devlet", "AKP"],
            last_update_at=NOW - timedelta(days=2),
        )
        _insert_article(
            db,
            article_id="stop_art",
            source_id="bianet",
            entities=["Türkiye", "AKP", "CHP"],  # all stopwords
        )
        _insert_cluster(
            db,
            cluster_id="cl_stopwords",
            final_defcon=int(DEFCON.ROUTINE),
            centroid=arc_vec,
            article_ids=["stop_art"],
        )

        result = await ArcLinker(db).run(RUN_ID)
        # Cluster has zero non-stopword entities → match_arc returns None →
        # a new arc is seeded but it carries an empty entity_set.
        assert result["joined"] == 0
        assert result["seeded"] == 1


# ── TestIdempotence ────────────────────────────────────────────────────────


class TestIdempotence:
    async def test_rerun_does_not_re_link_already_linked_clusters(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        _insert_article(db, article_id="art_idem", source_id="bianet",
                        entities=["İmamoğlu", "İstanbul Büyükşehir"])
        _insert_cluster(
            db,
            cluster_id="cl_idem",
            final_defcon=int(DEFCON.MATERIAL),
            centroid=_unit(6),
            article_ids=["art_idem"],
        )

        first = await ArcLinker(db).run(RUN_ID)
        second = await ArcLinker(db).run(RUN_ID)

        assert first["seeded"] == 1
        # Second pass sees no pending clusters (arc_id IS NOT NULL).
        assert second["seeded"] == 0
        assert second["joined"] == 0
        arc_count = db.execute("SELECT COUNT(*) FROM arcs").fetchone()[0]
        assert arc_count == 1


# ── TestStagesDone ─────────────────────────────────────────────────────────


class TestStagesDone:
    async def test_stages_done_appends_arc_link(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        await ArcLinker(db).run(RUN_ID)
        row = db.execute(
            "SELECT stages_done, counts FROM pipeline_runs WHERE run_id = ?",
            [RUN_ID],
        ).fetchone()
        stages = json.loads(row[0])
        counts = json.loads(row[1])
        assert "arc-link" in stages
        assert stages[-1] == "arc-link"
        assert "arcs_joined" in counts
        assert "arcs_seeded" in counts


# ── TestPeakDefconMinDirection ─────────────────────────────────────────────


class TestPeakDefconMinDirection:
    async def test_peak_uses_min_for_severity(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        # Arc starts at MATERIAL (3). New cluster is SEVERE (2 — more severe).
        # peak_defcon should become 2 (min of 3 and 2) because lower int = more severe.
        arc_vec = _unit(7)
        _insert_arc(
            db,
            arc_id="arc_peak_test",
            state=ArcState.OPEN,
            centroid=arc_vec,
            entity_set=["Erdoğan", "Saray"],
            last_update_at=NOW - timedelta(days=1),
            peak_defcon=int(DEFCON.MATERIAL),
        )
        _insert_article(db, article_id="severe1", source_id="cumhuriyet",
                        entities=["Erdoğan", "Saray"])
        _insert_cluster(
            db,
            cluster_id="cl_severe_link",
            final_defcon=int(DEFCON.SEVERE),
            centroid=arc_vec,
            article_ids=["severe1"],
        )
        await ArcLinker(db).run(RUN_ID)
        peak = db.execute(
            "SELECT peak_defcon FROM arcs WHERE id = 'arc_peak_test'"
        ).fetchone()[0]
        assert peak == int(DEFCON.SEVERE)

    async def test_peak_does_not_regress_to_less_severe(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        # Arc already at SEVERE (2). A ROUTINE (4 — less severe) cluster
        # joining must NOT regress the peak; peak stays at SEVERE.
        arc_vec = _unit(0)
        _insert_arc(
            db,
            arc_id="arc_peak_no_regress",
            state=ArcState.OPEN,
            centroid=arc_vec,
            entity_set=["KAP", "Borsa"],
            last_update_at=NOW - timedelta(days=1),
            peak_defcon=int(DEFCON.SEVERE),
        )
        _insert_article(db, article_id="rt1", source_id="diken",
                        entities=["KAP", "Borsa"])
        _insert_cluster(
            db,
            cluster_id="cl_routine_link",
            final_defcon=int(DEFCON.ROUTINE),
            centroid=arc_vec,
            article_ids=["rt1"],
        )
        await ArcLinker(db).run(RUN_ID)
        peak = db.execute(
            "SELECT peak_defcon FROM arcs WHERE id = 'arc_peak_no_regress'"
        ).fetchone()[0]
        # min(SEVERE=2, ROUTINE=4) = 2 → SEVERE preserved.
        assert peak == int(DEFCON.SEVERE)


def _vector_helper_smoke() -> None:
    """Sanity guard that _norm_vec stays a no-op for unit vectors."""
    v = _unit(0)
    assert _norm_vec(v) == v

"""End-to-end tests for the 2026-05-25 arc-evolution feature.

Arc summaries used to freeze at seed time · the MİT Syria arc read identical
sentence + identical "Açıldı" on day 1, 2, 3. This module pins the four-layer
behavior that makes arcs render differently when they have new clusters today
vs when they're stalled:

1. ``arcs`` schema · migration 004 adds ``last_update_summary``,
   ``last_update_headline``, ``last_update_cluster_id`` and backfills existing
   rows from ``summary``/``headline`` plus the most recently linked cluster.
2. ``musahit.arcs.linker`` · seeding writes the triplet alongside the seed
   columns; every joining cluster overwrites the triplet (last write wins).
3. ``musahit.writer.payload`` · ``ArcView.is_active_today`` is True when the
   arc's ``last_update_at`` falls within this run's window. ``ArcView.
   days_since_last_update`` is ``briefing_date − last_update_at.date()``.
4. ``musahit.writer.fallback`` · active-today arcs render with a
   ``**Güncelleme** · {last_update_summary}`` body; stalled arcs add a
   ``**Son güncelleme** · X gün önce`` header line and an italic stalled
   marker (``*Bu arc'da bugün yeni gelişme yok.*``).
5. ``musahit.tts.extractor`` · drops the italic stalled marker from voiced
   text; keeps the Güncelleme prefix voiced.
"""

from __future__ import annotations

import json
import math
from collections.abc import Generator
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from musahit.arcs.linker import ArcLinker
from musahit.common.migrations import init_db
from musahit.common.types import ArcState
from musahit.ingest.sources import seed_sources
from musahit.score.defcon import DEFCON
from musahit.tts.extractor import extract_voiced_briefing
from musahit.writer.fallback import (
    ARC_STALLED_MARKER,
    ARC_UPDATE_PREFIX,
    render_fallback_briefing,
)
from musahit.writer.payload import (
    ArcView,
    BriefingPayload,
    build_payload,
)

RUN_ID = "run_test"
NOW = datetime(2026, 5, 25, 12, 0, 0)
DIM = 1024


# ── Fixtures ──────────────────────────────────────────────────────────────


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


# ── Test helpers ──────────────────────────────────────────────────────────


def _unit(idx: int) -> list[float]:
    v = [0.0] * DIM
    v[idx] = 1.0
    return v


def _norm_check() -> None:
    """Sanity check the unit-vector helper · cosine self-similarity = 1.0."""
    v = _unit(0)
    assert math.isclose(sum(x * x for x in v), 1.0)


def _insert_article(
    conn: duckdb.DuckDBPyConnection,
    *,
    article_id: str,
    source_id: str,
    entities: list[str],
    published_at: datetime = NOW,
) -> None:
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
    arc_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO clusters (
            id, created_at, headline, summary, category,
            raw_defcon, ceiling_defcon, final_defcon, confidence,
            bands_present, arc_id, operator_override
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
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
            arc_id,
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
    headline: str | None = None,
    summary: str | None = None,
) -> None:
    """Insert an arc the way the linker would · all six writable columns."""
    conn.execute(
        """
        INSERT INTO arcs (
            id, created_at, headline, summary, state, last_update_at,
            category, peak_defcon, entity_set,
            last_update_summary, last_update_headline, last_update_cluster_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            arc_id,
            last_update_at - timedelta(days=1),
            headline or f"H-{arc_id}",
            summary or f"S-{arc_id}",
            state.value,
            last_update_at,
            "POLİTİKA",
            peak_defcon,
            json.dumps(entity_set),
            summary or f"S-{arc_id}",
            headline or f"H-{arc_id}",
            None,
        ],
    )
    conn.execute(
        """
        INSERT INTO arc_centroids (arc_id, centroid, updated_at)
        VALUES (?, ?, ?)
        """,
        [arc_id, centroid, last_update_at],
    )


# ── TestMigrationBackfill ─────────────────────────────────────────────────


class TestMigrationBackfill:
    """Migration 004 backfill should copy seed values into the new columns.

    Verified by running ``init_db`` against a fresh DB (which applies
    migration 004 at the end), inserting an arc with only the original
    columns, then re-applying migration 004 manually to exercise the
    backfill UPDATE statements against rows that pre-date the new schema
    is impossible in a fresh DB · so we test the equivalent shape by
    inserting an arc with NULL last_update_* and re-running the backfill
    statements explicitly. Migration apply itself is idempotent (already
    in schema_version) so the runner won't fire it twice."""

    def test_existing_columns_added(self, db: duckdb.DuckDBPyConnection) -> None:
        cols = {
            row[1]
            for row in db.execute("PRAGMA table_info(arcs)").fetchall()
        }
        assert "last_update_summary" in cols
        assert "last_update_headline" in cols
        assert "last_update_cluster_id" in cols

    def test_backfill_copies_summary_and_headline_when_null(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        # Simulate a row that pre-dated migration 004 (NULL last_update_*).
        db.execute(
            """
            INSERT INTO arcs (
                id, created_at, headline, summary, state, last_update_at,
                category, peak_defcon, entity_set
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                "arc_legacy_one",
                NOW - timedelta(days=5),
                "Yargıtay kararı",
                "Yargıtay karar açıkladı.",
                ArcState.OPEN.value,
                NOW - timedelta(days=3),
                "YARGI",
                int(DEFCON.MATERIAL),
                json.dumps(["Yargıtay"]),
            ],
        )
        # Re-run the backfill statements (idempotent · only touches NULL).
        db.execute(
            "UPDATE arcs SET last_update_summary = summary "
            "WHERE last_update_summary IS NULL"
        )
        db.execute(
            "UPDATE arcs SET last_update_headline = headline "
            "WHERE last_update_headline IS NULL"
        )
        row = db.execute(
            "SELECT last_update_summary, last_update_headline "
            "FROM arcs WHERE id = 'arc_legacy_one'"
        ).fetchone()
        assert row[0] == "Yargıtay karar açıkladı."
        assert row[1] == "Yargıtay kararı"

    def test_backfill_picks_most_recent_linked_cluster(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        # Insert an arc + two linked clusters with different created_at.
        # The backfill UPDATE should pick the more recent one.
        _insert_arc(
            db,
            arc_id="arc_backfill_cluster",
            state=ArcState.OPEN,
            centroid=_unit(0),
            entity_set=["A"],
            last_update_at=NOW - timedelta(days=2),
        )
        _insert_article(db, article_id="art_old", source_id="bianet",
                        entities=["A"])
        _insert_article(db, article_id="art_new", source_id="diken",
                        entities=["A"])
        _insert_cluster(
            db,
            cluster_id="cl_old",
            final_defcon=int(DEFCON.MATERIAL),
            centroid=_unit(0),
            article_ids=["art_old"],
            created_at=NOW - timedelta(days=4),
            arc_id="arc_backfill_cluster",
        )
        _insert_cluster(
            db,
            cluster_id="cl_new",
            final_defcon=int(DEFCON.MATERIAL),
            centroid=_unit(0),
            article_ids=["art_new"],
            created_at=NOW - timedelta(days=1),
            arc_id="arc_backfill_cluster",
        )
        # Null out the column so the backfill statement has something to do.
        db.execute(
            "UPDATE arcs SET last_update_cluster_id = NULL "
            "WHERE id = 'arc_backfill_cluster'"
        )
        db.execute(
            """
            UPDATE arcs
               SET last_update_cluster_id = (
                   SELECT c.id FROM clusters c
                    WHERE c.arc_id = arcs.id
                    ORDER BY c.created_at DESC LIMIT 1
               )
             WHERE last_update_cluster_id IS NULL
            """
        )
        row = db.execute(
            "SELECT last_update_cluster_id FROM arcs "
            "WHERE id = 'arc_backfill_cluster'"
        ).fetchone()
        assert row[0] == "cl_new"

    def test_backfill_leaves_cluster_id_null_with_no_linked_clusters(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        _insert_arc(
            db,
            arc_id="arc_no_clusters",
            state=ArcState.OPEN,
            centroid=_unit(1),
            entity_set=["X"],
            last_update_at=NOW - timedelta(days=2),
        )
        # Re-run backfill · no clusters linked, subquery returns NULL.
        db.execute(
            "UPDATE arcs SET last_update_cluster_id = NULL "
            "WHERE id = 'arc_no_clusters'"
        )
        db.execute(
            """
            UPDATE arcs
               SET last_update_cluster_id = (
                   SELECT c.id FROM clusters c
                    WHERE c.arc_id = arcs.id
                    ORDER BY c.created_at DESC LIMIT 1
               )
             WHERE last_update_cluster_id IS NULL
            """
        )
        row = db.execute(
            "SELECT last_update_cluster_id FROM arcs "
            "WHERE id = 'arc_no_clusters'"
        ).fetchone()
        assert row[0] is None  # safe NULL


# ── TestLinkerSeedWritesTriplet ───────────────────────────────────────────


class TestLinkerSeedWritesTriplet:
    async def test_seed_writes_last_update_fields(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        _insert_article(
            db, article_id="seed_art", source_id="bianet",
            entities=["AYM", "Karar"],
        )
        _insert_cluster(
            db,
            cluster_id="cl_seed",
            final_defcon=int(DEFCON.SEVERE),
            centroid=_unit(3),
            article_ids=["seed_art"],
            headline="AYM iptal kararı",
            summary="AYM düzenlemenin iptaline karar verdi.",
        )

        result = await ArcLinker(db).run(RUN_ID)
        assert result["seeded"] == 1

        arc_row = db.execute(
            "SELECT id, headline, summary, last_update_headline, "
            "last_update_summary, last_update_cluster_id FROM arcs"
        ).fetchone()
        arc_id, h, s, luh, lus, lucid = arc_row
        # Seed: last_update_* equals the seed columns.
        assert h == "AYM iptal kararı"
        assert s == "AYM düzenlemenin iptaline karar verdi."
        assert luh == h
        assert lus == s
        assert lucid == "cl_seed"


# ── TestLinkerJoinUpdatesTriplet ──────────────────────────────────────────


class TestLinkerJoinUpdatesTriplet:
    async def test_joining_cluster_overwrites_triplet(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        # Existing arc with seed-era last_update_*.
        arc_vec = _unit(5)
        _insert_arc(
            db,
            arc_id="arc_evolve",
            state=ArcState.OPEN,
            centroid=arc_vec,
            entity_set=["İmamoğlu", "İstanbul"],
            last_update_at=NOW - timedelta(days=2),
            headline="İmamoğlu davası",
            summary="Yargılama başladı.",
        )
        # New cluster joining today.
        _insert_article(
            db, article_id="evolve_art", source_id="cumhuriyet",
            entities=["İmamoğlu", "İstanbul"],
        )
        _insert_cluster(
            db,
            cluster_id="cl_evolve_today",
            final_defcon=int(DEFCON.MATERIAL),
            centroid=arc_vec,
            article_ids=["evolve_art"],
            headline="İmamoğlu davasında yeni gelişme",
            summary="Mahkeme bugün tanık dinledi.",
        )

        result = await ArcLinker(db).run(RUN_ID)
        assert result["joined"] == 1

        row = db.execute(
            "SELECT headline, summary, last_update_headline, "
            "last_update_summary, last_update_cluster_id, last_update_at "
            "FROM arcs WHERE id = 'arc_evolve'"
        ).fetchone()
        seed_h, seed_s, lu_h, lu_s, lu_cid, lu_at = row
        # Seed columns unchanged (headline stable across days).
        assert seed_h == "İmamoğlu davası"
        assert seed_s == "Yargılama başladı."
        # last_update_* overwritten with the joining cluster's data.
        assert lu_h == "İmamoğlu davasında yeni gelişme"
        assert lu_s == "Mahkeme bugün tanık dinledi."
        assert lu_cid == "cl_evolve_today"
        assert lu_at == NOW

    async def test_multiple_joins_same_run_last_wins(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        """Two clusters join the same arc in one run · the second wins.

        ``ArcLinker.run`` iterates by ``final_defcon ASC``. We feed two
        clusters at the same severity so the iteration order is
        deterministic by ``id``. The second one in iteration order
        overwrites the triplet (last call wins · matches existing
        last_update_at semantics)."""
        arc_vec = _unit(6)
        _insert_arc(
            db,
            arc_id="arc_multi",
            state=ArcState.OPEN,
            centroid=arc_vec,
            entity_set=["KAP", "Borsa"],
            last_update_at=NOW - timedelta(days=1),
        )
        _insert_article(db, article_id="mart_a", source_id="bianet",
                        entities=["KAP", "Borsa"])
        _insert_article(db, article_id="mart_b", source_id="diken",
                        entities=["KAP", "Borsa"])
        # Same final_defcon · ORDER BY final_defcon, then arbitrary; use
        # ids that sort lexically (cl_multi_a < cl_multi_b).
        _insert_cluster(
            db,
            cluster_id="cl_multi_a",
            final_defcon=int(DEFCON.MATERIAL),
            centroid=arc_vec,
            article_ids=["mart_a"],
            summary="İlk haber.",
            headline="İlk başlık",
        )
        _insert_cluster(
            db,
            cluster_id="cl_multi_b",
            final_defcon=int(DEFCON.MATERIAL),
            centroid=arc_vec,
            article_ids=["mart_b"],
            summary="İkinci haber.",
            headline="İkinci başlık",
        )

        result = await ArcLinker(db).run(RUN_ID)
        assert result["joined"] == 2

        row = db.execute(
            "SELECT last_update_cluster_id, last_update_summary "
            "FROM arcs WHERE id = 'arc_multi'"
        ).fetchone()
        # Whichever cluster the linker processed last wins · because the
        # SQL ORDER BY is "final_defcon ASC" only (no secondary key), the
        # ordering between the two equal-severity clusters depends on
        # DuckDB's row-ordering for ties. Assert that the winner is ONE
        # of the two and its summary matches its cluster id.
        last_cid, last_sum = row
        assert last_cid in {"cl_multi_a", "cl_multi_b"}
        expected_sum = (
            "İlk haber." if last_cid == "cl_multi_a" else "İkinci haber."
        )
        assert last_sum == expected_sum


# ── TestPayloadActiveStalledTags ──────────────────────────────────────────


class TestPayloadActiveStalledTags:
    """``build_payload`` should populate ``is_active_today`` and
    ``days_since_last_update`` correctly against the run window."""

    def test_arc_updated_this_run_is_active(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        # Arc updated AFTER started_at · is_active_today=True.
        _insert_arc(
            db,
            arc_id="arc_active",
            state=ArcState.OPEN,
            centroid=_unit(0),
            entity_set=["E1"],
            last_update_at=NOW + timedelta(minutes=5),
        )
        _insert_article(db, article_id="aart", source_id="bianet",
                        entities=["E1"])
        _insert_cluster(
            db, cluster_id="cl_active_payload",
            final_defcon=int(DEFCON.MATERIAL),
            centroid=_unit(0),
            article_ids=["aart"],
            arc_id="arc_active",
        )

        payload = build_payload(db, RUN_ID, target_date=NOW.date())
        active = next(a for a in payload.open_arc_updates if a.id == "arc_active")
        assert active.is_active_today is True
        assert active.days_since_last_update == 0

    def test_arc_updated_before_run_is_stalled(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        # Arc whose last_update_at is 3 days before run start.
        old_update = NOW - timedelta(days=3)
        _insert_arc(
            db,
            arc_id="arc_stalled",
            state=ArcState.OPEN,
            centroid=_unit(1),
            entity_set=["E2"],
            last_update_at=old_update,
        )
        # Link a cluster from THIS run so the arc surfaces in
        # open_arc_updates (the JOIN filters by ingest_log.run_id).
        _insert_article(db, article_id="sart", source_id="bianet",
                        entities=["E2"])
        _insert_cluster(
            db, cluster_id="cl_stalled_payload",
            final_defcon=int(DEFCON.MATERIAL),
            centroid=_unit(1),
            article_ids=["sart"],
            arc_id="arc_stalled",
        )

        payload = build_payload(db, RUN_ID, target_date=NOW.date())
        stalled = next(
            a for a in payload.open_arc_updates if a.id == "arc_stalled"
        )
        assert stalled.is_active_today is False
        assert stalled.days_since_last_update == 3

    def test_briefing_date_drives_days_since(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        """days_since_last_update uses briefing_date, not utcnow()."""
        _insert_arc(
            db,
            arc_id="arc_days",
            state=ArcState.OPEN,
            centroid=_unit(2),
            entity_set=["E3"],
            last_update_at=NOW - timedelta(days=7),
        )
        _insert_article(db, article_id="dart", source_id="bianet",
                        entities=["E3"])
        _insert_cluster(
            db, cluster_id="cl_days_payload",
            final_defcon=int(DEFCON.MATERIAL),
            centroid=_unit(2),
            article_ids=["dart"],
            arc_id="arc_days",
        )

        briefing_date = NOW.date() + timedelta(days=2)  # arbitrary future date
        payload = build_payload(db, RUN_ID, target_date=briefing_date)
        a = next(x for x in payload.open_arc_updates if x.id == "arc_days")
        # 7 days stale at run time + 2 more days for the briefing date.
        assert a.days_since_last_update == 9


# ── TestFallbackRendererActiveStalled ─────────────────────────────────────


def _payload_with_one_arc(arc: ArcView) -> BriefingPayload:
    return BriefingPayload(
        date=date(2026, 5, 25),
        run_id="run_render_test",
        clusters_by_defcon={},
        open_arc_updates=[arc],
        resolved_arcs=[],
        peak_defcon=int(DEFCON.AMBIENT),
        cluster_count=0,
        arc_count=1,
        open_arc_count=1,
        ambient_count=0,
        failed_sources=[],
        stages_done=["ingest", "normalize", "cluster", "score", "arc-link"],
    )


class TestFallbackRendererActiveStalled:
    def test_active_today_renders_with_guncelleme_prefix(self) -> None:
        active = ArcView(
            id="arc_render_active",
            headline="MİT Suriye operasyonu",
            summary="MİT operasyonu başlattı.",
            state="OPEN",
            peak_defcon=int(DEFCON.SEVERE),
            category="GÜVENLİK",
            last_update_at=NOW,
            created_at=NOW - timedelta(days=2),
            last_update_summary="MİT bugün üçüncü hedefi vurdu.",
            last_update_headline="MİT üçüncü vuruş",
            last_update_cluster_id="cl_active_render",
            is_active_today=True,
            days_since_last_update=0,
        )
        body = render_fallback_briefing(_payload_with_one_arc(active))
        # Güncelleme prefix carries the LATEST cluster's summary.
        assert f"{ARC_UPDATE_PREFIX} MİT bugün üçüncü hedefi vurdu." in body
        # Stalled marker MUST NOT appear for active arcs.
        assert ARC_STALLED_MARKER not in body
        # Son güncelleme line MUST NOT appear for active arcs.
        assert "**Son güncelleme**" not in body
        # The seed headline (stable across days) is still the header.
        assert "MİT Suriye operasyonu" in body

    def test_stalled_renders_with_son_guncelleme_and_italic_marker(
        self,
    ) -> None:
        stalled = ArcView(
            id="arc_render_stalled",
            headline="Eski hikaye",
            summary="Konu ilk olarak gündeme gelmişti.",
            state="OPEN",
            peak_defcon=int(DEFCON.MATERIAL),
            category="POLİTİKA",
            last_update_at=NOW - timedelta(days=3),
            created_at=NOW - timedelta(days=10),
            last_update_summary="Eski özet.",
            last_update_headline="Eski güncelleme başlığı",
            last_update_cluster_id="cl_stale_render",
            is_active_today=False,
            days_since_last_update=3,
        )
        body = render_fallback_briefing(_payload_with_one_arc(stalled))
        assert "**Son güncelleme** · 3 gün önce" in body
        # Original (seed) summary still appears.
        assert "Konu ilk olarak gündeme gelmişti." in body
        # Italic stalled marker is present.
        assert ARC_STALLED_MARKER in body
        # Güncelleme prefix MUST NOT appear for stalled arcs.
        assert ARC_UPDATE_PREFIX not in body

    def test_active_falls_back_to_seed_summary_when_last_update_empty(
        self,
    ) -> None:
        """A 0-day arc whose linker write went through but the cluster had
        an empty summary should fall back to the seed summary rather than
        emit ``**Güncelleme** · `` (trailing whitespace)."""
        active = ArcView(
            id="arc_render_fallback",
            headline="Yeni hikaye",
            summary="Açılış özeti.",
            state="OPEN",
            peak_defcon=int(DEFCON.MATERIAL),
            category="POLİTİKA",
            last_update_at=NOW,
            created_at=NOW,
            last_update_summary="",  # cluster had no summary
            last_update_headline="",
            last_update_cluster_id="cl_seed_empty",
            is_active_today=True,
            days_since_last_update=0,
        )
        body = render_fallback_briefing(_payload_with_one_arc(active))
        assert f"{ARC_UPDATE_PREFIX} Açılış özeti." in body


# ── TestVoicedCapPrioritization ───────────────────────────────────────────


class TestVoicedCapPrioritization:
    def test_active_today_arcs_sort_before_stalled(self) -> None:
        """Even high-severity stalled arcs come AFTER low-severity active.

        Rule: active-today is the primary sort key. Severity and recency
        are tiebreakers within each active/stalled bucket."""
        stalled_severe = ArcView(
            id="arc_stalled_severe",
            headline="STALLED_SEVERE",
            summary="x",
            state="OPEN",
            peak_defcon=int(DEFCON.SEVERE),
            category=None,
            last_update_at=NOW - timedelta(days=2),
            created_at=NOW - timedelta(days=5),
            is_active_today=False,
            days_since_last_update=2,
        )
        active_routine = ArcView(
            id="arc_active_routine",
            headline="ACTIVE_ROUTINE",
            summary="y",
            state="OPEN",
            peak_defcon=int(DEFCON.ROUTINE),
            category=None,
            last_update_at=NOW,
            created_at=NOW - timedelta(days=1),
            is_active_today=True,
            days_since_last_update=0,
        )
        payload = BriefingPayload(
            date=date(2026, 5, 25),
            run_id="run_sort",
            clusters_by_defcon={},
            open_arc_updates=[stalled_severe, active_routine],
            resolved_arcs=[],
            peak_defcon=int(DEFCON.AMBIENT),
            cluster_count=0,
            arc_count=2,
            open_arc_count=2,
            ambient_count=0,
            failed_sources=[],
            stages_done=["arc-link"],
        )
        body = render_fallback_briefing(payload)
        # The active-routine arc should appear BEFORE the stalled-severe
        # one in the rendered output even though stalled-severe has a
        # more severe peak_defcon.
        assert body.index("ACTIVE_ROUTINE") < body.index("STALLED_SEVERE")


# ── TestTtsExtractorStripsStalledMarker ──────────────────────────────────


class TestTtsExtractorStripsStalledMarker:
    def _briefing_with(self, open_arcs_body: str) -> str:
        # Minimal validator-shape briefing with only the OPEN_ARCS section
        # populated. Other sections get the placeholder no-items text.
        return (
            "# Müşahit Günlük Brifing\n\n"
            "**Tarih** · 25 Mayıs 2026\n\n"
            "---\n\n"
            "## ❯ DEFCON 1-2 · ÖNCELİKLİ\n\n"
            "Bugün bu bölümde öğe yok.\n\n"
            "---\n\n"
            "## ❯ DEFCON 3 · MATERYAL\n\n"
            "Bugün bu bölümde öğe yok.\n\n"
            "---\n\n"
            "## ❯ AÇIK GELİŞMELER · DEVAM EDEN TAKİP\n\n"
            f"{open_arcs_body}\n\n"
            "---\n\n"
            "## ❯ DEFCON 4 · GÜNDEM\n\n"
            "(yok)\n\n"
            "---\n\n"
            "## ❯ DİKKAT · YALNIZCA SOSYALDE\n\n"
            "(yok)\n\n"
            "---\n\n"
            "## ❯ AMBİYANS · DEFCON 5\n\n"
            "(yok)\n\n"
            "---\n\n"
            "## ❯ KAPATILAN HİKAYELER\n\n"
            "(yok)\n\n"
            "---\n\n"
            "## ❯ SİSTEM LOG\n\n"
            "(yok)\n"
        )

    def test_italic_stalled_marker_dropped_from_voiced_text(self) -> None:
        body = (
            "### Eski hikaye · arc_x\n"
            "**Açıldı** · 1 Mayıs 2026 · "
            "**Zirve DEFCON** · MATERYAL · **Kategori** · POLİTİKA\n"
            "**Son güncelleme** · 7 gün önce\n"
            "\n"
            "Konu daha önce gündeme gelmişti.\n"
            "\n"
            f"{ARC_STALLED_MARKER}\n"
        )
        voiced = extract_voiced_briefing(self._briefing_with(body))
        # The italic stalled line is gone.
        assert ARC_STALLED_MARKER not in voiced.open_arcs
        # The seed summary and Son güncelleme line are kept.
        assert "Konu daha önce gündeme gelmişti." in voiced.open_arcs
        assert "Son güncelleme" in voiced.open_arcs

    def test_guncelleme_prefix_kept_in_voiced_text(self) -> None:
        body = (
            "### Aktif hikaye · arc_y\n"
            "**Açıldı** · 23 Mayıs 2026 · "
            "**Zirve DEFCON** · SEVERE · **Kategori** · GÜVENLİK\n"
            "\n"
            f"{ARC_UPDATE_PREFIX} Bugün yeni gelişme yaşandı.\n"
        )
        voiced = extract_voiced_briefing(self._briefing_with(body))
        assert ARC_UPDATE_PREFIX in voiced.open_arcs
        assert "Bugün yeni gelişme yaşandı." in voiced.open_arcs

    def test_stalled_marker_inside_other_section_is_left_alone(self) -> None:
        """Strip is OPEN_ARCS-section-scoped · if the same literal somehow
        appeared in another section it should NOT be stripped (we don't
        currently emit it elsewhere, but the strip is bounded by design).
        """
        # Place the marker into DEFCON 1-2 section instead of OPEN_ARCS.
        briefing = (
            "# Müşahit Günlük Brifing\n\n"
            "**Tarih** · 25 Mayıs 2026\n\n"
            "---\n\n"
            "## ❯ DEFCON 1-2 · ÖNCELİKLİ\n\n"
            f"### Test\n{ARC_STALLED_MARKER}\n\n"
            "---\n\n"
            "## ❯ DEFCON 3 · MATERYAL\n\n"
            "(yok)\n\n"
            "---\n\n"
            "## ❯ AÇIK GELİŞMELER · DEVAM EDEN TAKİP\n\n"
            "(yok)\n\n"
            "---\n\n"
            "## ❯ DEFCON 4 · GÜNDEM\n\n"
            "(yok)\n\n"
            "---\n\n"
            "## ❯ DİKKAT · YALNIZCA SOSYALDE\n\n"
            "(yok)\n\n"
            "---\n\n"
            "## ❯ AMBİYANS · DEFCON 5\n\n"
            "(yok)\n\n"
            "---\n\n"
            "## ❯ KAPATILAN HİKAYELER\n\n"
            "(yok)\n\n"
            "---\n\n"
            "## ❯ SİSTEM LOG\n\n"
            "(yok)\n"
        )
        voiced = extract_voiced_briefing(briefing)
        # The strip applies only to OPEN_ARCS · DEFCON 1-2 keeps it.
        assert ARC_STALLED_MARKER in voiced.defcon_priority

    def test_no_stalled_markers_is_a_noop(self) -> None:
        """Briefings without any stalled marker (all arcs active or no
        open arcs at all) must extract cleanly · no whitespace damage."""
        body = (
            "### Aktif hikaye · arc_z\n"
            "**Açıldı** · 24 Mayıs 2026 · "
            "**Zirve DEFCON** · MATERYAL · **Kategori** · POLİTİKA\n"
            "\n"
            f"{ARC_UPDATE_PREFIX} Tek satır güncelleme.\n"
        )
        voiced = extract_voiced_briefing(self._briefing_with(body))
        assert "Tek satır güncelleme." in voiced.open_arcs

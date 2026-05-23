"""Tests for musahit.writer.payload — DB → BriefingPayload."""

from __future__ import annotations

import json
from collections.abc import Generator
from datetime import datetime
from pathlib import Path

import duckdb
import pytest

from musahit.common.migrations import init_db
from musahit.common.types import ArcState, IngestStatus
from musahit.ingest.sources import seed_sources
from musahit.score.defcon import DEFCON
from musahit.writer.payload import build_payload

RUN_ID = "run_test"
NOW = datetime(2026, 5, 23, 8, 0, 0)


@pytest.fixture()
def db(tmp_path: Path) -> Generator[duckdb.DuckDBPyConnection, None, None]:
    db_path = tmp_path / "x.duckdb"
    init_db(db_path, load_vss=False)
    conn = duckdb.connect(str(db_path))
    seed_sources(conn)
    conn.execute(
        "INSERT INTO pipeline_runs (run_id, started_at, status, stages_done, counts) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            RUN_ID,
            NOW,
            "RUNNING",
            json.dumps(["ingest", "normalize", "cluster", "score", "arc-link"]),
            json.dumps({}),
        ],
    )
    try:
        yield conn
    finally:
        conn.close()


def _seed_cluster(
    conn: duckdb.DuckDBPyConnection,
    cluster_id: str,
    final_defcon: int,
    source_ids: list[str],
    arc_id: str | None = None,
    category: str = "POLİTİKA",
) -> None:
    bands_list: list[str] = []
    for src in source_ids:
        band = conn.execute(
            "SELECT band FROM sources WHERE id = ?", [src]
        ).fetchone()[0]
        bands_list.append(band)
    conn.execute(
        "INSERT INTO clusters (id, created_at, headline, summary, category, raw_defcon, "
        "ceiling_defcon, final_defcon, confidence, bands_present, arc_id, operator_override) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        [
            cluster_id,
            NOW,
            f"H-{cluster_id}",
            f"S-{cluster_id}",
            category,
            final_defcon,
            final_defcon,
            final_defcon,
            "ORTA",
            json.dumps(sorted(set(bands_list))),
            arc_id,
        ],
    )
    for i, src in enumerate(source_ids):
        aid = f"{cluster_id}_a{i}"
        conn.execute(
            "INSERT OR IGNORE INTO ingest_log (run_id, source_id, started_at, completed_at, "
            "status, articles_fetched) VALUES (?, ?, ?, ?, ?, ?)",
            [RUN_ID, src, NOW, NOW, IngestStatus.OK.value, 1],
        )
        conn.execute(
            "INSERT INTO articles (id, source_id, url, fetched_at, published_at, "
            "title, lead, body, language, entities, word_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [aid, src, f"u/{aid}", NOW, NOW, "t", "l", "b", "tr", "[]", 40],
        )
        conn.execute(
            "INSERT INTO cluster_articles (cluster_id, article_id) VALUES (?, ?)",
            [cluster_id, aid],
        )


def _seed_arc(
    conn: duckdb.DuckDBPyConnection,
    arc_id: str,
    state: ArcState,
    peak_defcon: int = int(DEFCON.MATERIAL),
) -> None:
    conn.execute(
        "INSERT INTO arcs (id, created_at, headline, summary, state, last_update_at, "
        "category, peak_defcon, entity_set) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            arc_id,
            NOW,
            f"AH-{arc_id}",
            f"AS-{arc_id}",
            state.value,
            NOW,
            "POLİTİKA",
            peak_defcon,
            json.dumps(["İmamoğlu"]),
        ],
    )


class TestBuildPayload:
    def test_buckets_clusters_by_final_defcon(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        _seed_cluster(db, "cl_p", int(DEFCON.SEVERE), ["sabah", "cumhuriyet"])
        _seed_cluster(db, "cl_m", int(DEFCON.MATERIAL), ["bianet"])
        _seed_cluster(db, "cl_r", int(DEFCON.ROUTINE), ["diken"])

        payload = build_payload(db, RUN_ID)
        assert payload.run_id == RUN_ID
        assert int(DEFCON.SEVERE) in payload.clusters_by_defcon
        assert int(DEFCON.MATERIAL) in payload.clusters_by_defcon
        assert int(DEFCON.ROUTINE) in payload.clusters_by_defcon
        assert payload.cluster_count == 3
        # Peak DEFCON = lowest integer (most severe) across the bucket.
        assert payload.peak_defcon == int(DEFCON.SEVERE)

    def test_open_arcs_only_when_linked_to_today_clusters(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        _seed_arc(db, "arc_today", ArcState.OPEN)
        _seed_arc(db, "arc_other", ArcState.OPEN)
        _seed_cluster(
            db, "cl1", int(DEFCON.MATERIAL), ["bianet"], arc_id="arc_today"
        )
        payload = build_payload(db, RUN_ID)
        ids = [a.id for a in payload.open_arc_updates]
        assert "arc_today" in ids
        # arc_other has no cluster from this run → not in updates
        assert "arc_other" not in ids

    def test_resolved_arcs_listed_when_resolved_today(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        _seed_arc(db, "arc_closed", ArcState.RESOLVED)
        payload = build_payload(db, RUN_ID)
        ids = [a.id for a in payload.resolved_arcs]
        assert "arc_closed" in ids

    def test_open_arc_count_counts_all_open(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        _seed_arc(db, "a1", ArcState.OPEN)
        _seed_arc(db, "a2", ArcState.OPEN)
        _seed_arc(db, "a3", ArcState.WATCH)
        payload = build_payload(db, RUN_ID)
        assert payload.open_arc_count == 2
        assert payload.arc_count == 3

    def test_failed_sources_collected(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        db.execute(
            "INSERT INTO ingest_log (run_id, source_id, started_at, completed_at, "
            "status, articles_fetched, error_detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [RUN_ID, "ap_tr", NOW, NOW, IngestStatus.HTTP_ERROR.value, 0, "HTTP 503"],
        )
        payload = build_payload(db, RUN_ID)
        assert len(payload.failed_sources) == 1
        assert payload.failed_sources[0].source_id == "ap_tr"
        assert payload.failed_sources[0].status == IngestStatus.HTTP_ERROR.value

    def test_unknown_run_id_raises(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        with pytest.raises(ValueError):
            build_payload(db, "run_does_not_exist")

    def test_empty_run_yields_safe_defaults(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        payload = build_payload(db, RUN_ID)
        assert payload.cluster_count == 0
        assert payload.peak_defcon == int(DEFCON.AMBIENT)
        assert payload.open_arc_updates == []
        assert payload.resolved_arcs == []
        assert payload.failed_sources == []

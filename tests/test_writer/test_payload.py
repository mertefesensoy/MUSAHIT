"""Tests for musahit.writer.payload · DB → BriefingPayload."""

from __future__ import annotations

import json
from collections.abc import Generator
from datetime import date, datetime
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


# ── Regression: target_date overrides started_at-derived date (2026-05-24) ─


class TestTargetDatePropagation:
    """The 2026-05-23 smoke run wrote the briefing to
    briefings/2026/05/23/ because ``build_payload`` derived the date
    from ``pipeline_runs.started_at`` (UTC). The TR-local date the
    operator asked for was 2026-05-24 (the run started at 00:28 TR =
    21:28 UTC on the previous day). The ``target_date`` kwarg makes
    the date a first-class input rather than a derived one."""

    def test_target_date_overrides_utc_started_at(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        """When ``target_date`` is passed, it wins over started_at.date()."""
        tr_today = date(2026, 5, 24)  # TR-local "today"
        payload = build_payload(db, RUN_ID, target_date=tr_today)
        assert payload.date == tr_today
        # The fixture's started_at is 2026-05-23 08:00 UTC · without
        # target_date the payload would be dated 2026-05-23.
        assert payload.date != NOW.date()

    def test_no_target_date_falls_back_to_started_at(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        """Legacy behavior preserved for callers that don't pass it."""
        payload = build_payload(db, RUN_ID)
        assert payload.date == NOW.date()

    def test_midnight_crossing_simulation(
        self, tmp_path: Path
    ) -> None:
        """Pipeline runs at 23:59 UTC on day N · target_date is day N+1
        (TR-local 02:59). Without the fix, the briefing would be dated
        day N. With the fix, the briefing is dated day N+1."""
        db_path = tmp_path / "midnight.duckdb"
        init_db(db_path, load_vss=False)
        conn = duckdb.connect(str(db_path))
        try:
            seed_sources(conn)
            late_utc = datetime(2026, 5, 23, 23, 59, 0)
            conn.execute(
                "INSERT INTO pipeline_runs (run_id, started_at, status, "
                "stages_done, counts) VALUES (?, ?, ?, ?, ?)",
                [
                    "run_late",
                    late_utc,
                    "RUNNING",
                    json.dumps([]),
                    json.dumps({}),
                ],
            )
            tr_local_target = date(2026, 5, 24)

            with_fix = build_payload(
                conn, "run_late", target_date=tr_local_target
            )
            without_fix = build_payload(conn, "run_late")

            assert with_fix.date == tr_local_target
            # The legacy path resolves to UTC date · the bug we fixed.
            assert without_fix.date == late_utc.date()
            assert with_fix.date != without_fix.date
        finally:
            conn.close()

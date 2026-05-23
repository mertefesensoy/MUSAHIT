"""The :class:`BriefingPayload` and its DB loader.

The writer model gets handed one fully-assembled :class:`BriefingPayload`
per nightly run. It carries:

* The date and run_id (for the header line + file-system path).
* Clusters bucketed by final DEFCON (1-2 / 3 / 4 / 5+) — the writer's
  primary content.
* Arcs that received updates today (open_arc_updates).
* Arcs that transitioned to RESOLVED today.
* Aggregate counts for the header line.
* Failed sources from the ingest stage's ingest_log for the SİSTEM LOG
  footer.

The loader (:func:`build_payload`) reads from the DB via plain SQL.
The output is a pure-data structure — no DB handles, no callbacks —
which keeps the prompt builder, the fallback renderer, and the writer
tests all trivially testable against fixture payloads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import duckdb

from musahit.common.types import ArcState, IngestStatus
from musahit.score.defcon import DEFCON

# Buckets the briefing actually surfaces. DEFCON 0 (UNTHINKABLE) is
# operator-override gated and never auto-includes; for our purposes 0
# is grouped with 1-2 if it ever lands. DEFCON 5 (AMBIENT) is counted
# but not listed in the briefing body.
BUCKET_PRIORITY: tuple[int, ...] = (0, 1, 2)
BUCKET_MATERIAL: tuple[int, ...] = (3,)
BUCKET_ROUTINE: tuple[int, ...] = (4,)
BUCKET_AMBIENT: tuple[int, ...] = (5,)


@dataclass(frozen=True)
class ClusterView:
    """Subset of a cluster row that the writer/fallback consume."""

    id: str
    headline: str
    summary: str
    category: str | None
    final_defcon: int
    confidence: str | None
    bands_present: list[str]
    arc_id: str | None
    sources: list[dict[str, str]]  # [{source_id, band}, ...]
    is_social_only: bool


@dataclass(frozen=True)
class ArcView:
    """Subset of an arc row that the writer/fallback consume."""

    id: str
    headline: str
    summary: str
    state: str
    peak_defcon: int
    category: str | None
    last_update_at: datetime | None
    created_at: datetime | None


@dataclass(frozen=True)
class FailedSource:
    """One failed-source entry for the SİSTEM LOG footer."""

    source_id: str
    status: str
    error_detail: str


@dataclass(frozen=True)
class BriefingPayload:
    """Everything the writer / fallback need to render the briefing."""

    date: date
    run_id: str
    clusters_by_defcon: dict[int, list[ClusterView]] = field(default_factory=dict)
    open_arc_updates: list[ArcView] = field(default_factory=list)
    resolved_arcs: list[ArcView] = field(default_factory=list)
    peak_defcon: int = int(DEFCON.AMBIENT)
    cluster_count: int = 0
    arc_count: int = 0
    open_arc_count: int = 0
    ambient_count: int = 0
    failed_sources: list[FailedSource] = field(default_factory=list)
    stages_done: list[str] = field(default_factory=list)


# ── Loader ─────────────────────────────────────────────────────────────────


def build_payload(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
) -> BriefingPayload:
    """Read this run's clusters/arcs/log entries and return a payload."""
    run_row = conn.execute(
        "SELECT started_at, stages_done FROM pipeline_runs WHERE run_id = ?",
        [run_id],
    ).fetchone()
    if run_row is None:
        raise ValueError(f"No pipeline_runs row for run_id={run_id!r}")
    started_at, stages_json = run_row
    briefing_date = (started_at or datetime.utcnow()).date()
    stages_done = json.loads(stages_json) if stages_json else []

    clusters_by_defcon = _load_clusters(conn, run_id)
    open_arc_updates, resolved_arcs = _load_arcs(conn, run_id)
    failed_sources = _load_failed_sources(conn, run_id)

    cluster_count = sum(len(v) for v in clusters_by_defcon.values())
    ambient_count = len(clusters_by_defcon.get(int(DEFCON.AMBIENT), []))
    peak_defcon = min(
        (c.final_defcon for buckets in clusters_by_defcon.values() for c in buckets),
        default=int(DEFCON.AMBIENT),
    )
    open_arc_count = conn.execute(
        "SELECT COUNT(*) FROM arcs WHERE state = ?",
        [ArcState.OPEN.value],
    ).fetchone()[0]
    arc_count = conn.execute("SELECT COUNT(*) FROM arcs").fetchone()[0]

    return BriefingPayload(
        date=briefing_date,
        run_id=run_id,
        clusters_by_defcon=clusters_by_defcon,
        open_arc_updates=open_arc_updates,
        resolved_arcs=resolved_arcs,
        peak_defcon=peak_defcon,
        cluster_count=cluster_count,
        arc_count=int(arc_count),
        open_arc_count=int(open_arc_count),
        ambient_count=ambient_count,
        failed_sources=failed_sources,
        stages_done=stages_done,
    )


# ── Internals ──────────────────────────────────────────────────────────────


def _load_clusters(
    conn: duckdb.DuckDBPyConnection, run_id: str
) -> dict[int, list[ClusterView]]:
    cursor = conn.execute(
        """
        SELECT c.id, c.headline, c.summary, c.category, c.final_defcon,
               c.confidence, c.bands_present, c.arc_id
          FROM clusters c
          JOIN cluster_articles ca ON ca.cluster_id = c.id
          JOIN articles a ON a.id = ca.article_id
          JOIN ingest_log l ON l.source_id = a.source_id AND l.run_id = ?
         WHERE c.final_defcon IS NOT NULL
         GROUP BY c.id, c.headline, c.summary, c.category, c.final_defcon,
                  c.confidence, c.bands_present, c.arc_id
         ORDER BY c.final_defcon ASC, c.id
        """,
        [run_id],
    )
    rows = cursor.fetchall()
    out: dict[int, list[ClusterView]] = {}
    for cid, headline, summary, category, final_defcon, conf, bands_json, arc_id in rows:
        bands = json.loads(bands_json) if bands_json else []
        sources = _load_cluster_sources(conn, cid)
        social_bands = {"social_x", "social_reddit"}
        non_social = [b for b in bands if b not in social_bands]
        out.setdefault(int(final_defcon), []).append(
            ClusterView(
                id=cid,
                headline=headline or "",
                summary=summary or "",
                category=category,
                final_defcon=int(final_defcon),
                confidence=conf,
                bands_present=bands,
                arc_id=arc_id,
                sources=sources,
                is_social_only=bool(bands) and not non_social,
            )
        )
    return out


def _load_cluster_sources(
    conn: duckdb.DuckDBPyConnection, cluster_id: str
) -> list[dict[str, str]]:
    cursor = conn.execute(
        """
        SELECT DISTINCT a.source_id, s.band
          FROM cluster_articles ca
          JOIN articles a ON a.id = ca.article_id
          JOIN sources s ON s.id = a.source_id
         WHERE ca.cluster_id = ?
         ORDER BY a.source_id
        """,
        [cluster_id],
    )
    return [{"source_id": sid, "band": band} for sid, band in cursor.fetchall()]


def _load_arcs(
    conn: duckdb.DuckDBPyConnection, run_id: str
) -> tuple[list[ArcView], list[ArcView]]:
    """Open arcs that received updates today + arcs that resolved today."""
    started_at = conn.execute(
        "SELECT started_at FROM pipeline_runs WHERE run_id = ?",
        [run_id],
    ).fetchone()
    run_started = started_at[0] if started_at else None

    open_cursor = conn.execute(
        """
        SELECT DISTINCT a.id, a.headline, a.summary, a.state, a.peak_defcon,
               a.category, a.last_update_at, a.created_at
          FROM arcs a
          JOIN clusters c ON c.arc_id = a.id
          JOIN cluster_articles ca ON ca.cluster_id = c.id
          JOIN articles art ON art.id = ca.article_id
          JOIN ingest_log l ON l.source_id = art.source_id AND l.run_id = ?
         WHERE a.state = ?
         ORDER BY a.peak_defcon ASC, a.id
        """,
        [run_id, ArcState.OPEN.value],
    )
    open_arcs = [
        ArcView(
            id=r[0],
            headline=r[1] or "",
            summary=r[2] or "",
            state=r[3],
            peak_defcon=int(r[4]) if r[4] is not None else int(DEFCON.AMBIENT),
            category=r[5],
            last_update_at=r[6],
            created_at=r[7],
        )
        for r in open_cursor.fetchall()
    ]

    resolved_cursor = conn.execute(
        """
        SELECT a.id, a.headline, a.summary, a.state, a.peak_defcon,
               a.category, a.last_update_at, a.created_at
          FROM arcs a
         WHERE a.state = ?
           AND (? IS NULL OR a.last_update_at >= ?)
         ORDER BY a.peak_defcon ASC, a.id
        """,
        [ArcState.RESOLVED.value, run_started, run_started],
    )
    resolved = [
        ArcView(
            id=r[0],
            headline=r[1] or "",
            summary=r[2] or "",
            state=r[3],
            peak_defcon=int(r[4]) if r[4] is not None else int(DEFCON.AMBIENT),
            category=r[5],
            last_update_at=r[6],
            created_at=r[7],
        )
        for r in resolved_cursor.fetchall()
    ]
    return open_arcs, resolved


def _load_failed_sources(
    conn: duckdb.DuckDBPyConnection, run_id: str
) -> list[FailedSource]:
    cursor = conn.execute(
        """
        SELECT source_id, status, error_detail
          FROM ingest_log
         WHERE run_id = ? AND status <> ?
         ORDER BY source_id
        """,
        [run_id, IngestStatus.OK.value],
    )
    return [
        FailedSource(source_id=sid, status=status, error_detail=err or "")
        for sid, status, err in cursor.fetchall()
    ]


def _ignore_unused() -> Any:  # pragma: no cover
    # Keep the imports stable across refactors.
    return BUCKET_PRIORITY, BUCKET_MATERIAL, BUCKET_ROUTINE, BUCKET_AMBIENT


__all__ = [
    "BUCKET_AMBIENT",
    "BUCKET_MATERIAL",
    "BUCKET_PRIORITY",
    "BUCKET_ROUTINE",
    "ArcView",
    "BriefingPayload",
    "ClusterView",
    "FailedSource",
    "build_payload",
]

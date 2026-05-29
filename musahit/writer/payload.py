"""The :class:`BriefingPayload` and its DB loader.

The writer model gets handed one fully-assembled :class:`BriefingPayload`
per nightly run. It carries:

* The date and run_id (for the header line + file-system path).
* Clusters bucketed by final DEFCON (1-2 / 3 / 4 / 5+) · the writer's
  primary content.
* Arcs that received updates today (open_arc_updates).
* Arcs that transitioned to RESOLVED today.
* Aggregate counts for the header line.
* Failed sources from the ingest stage's ingest_log for the SİSTEM LOG
  footer.

The loader (:func:`build_payload`) reads from the DB via plain SQL.
The output is a pure-data structure · no DB handles, no callbacks ·
which keeps the prompt builder, the fallback renderer, and the writer
tests all trivially testable against fixture payloads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import duckdb

from musahit.arcs.freshness import Freshness, freshness_from_days
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
    """Subset of a cluster row that the writer/fallback consume.

    The trailing three freshness fields (2026-05-29 Group-A) carry the
    recency anchor for this cluster's line in the deterministic itemized
    sections (DEFCON 4 GÜNDEM, AMBİYANS). They default safely so legacy
    fixtures that construct ``ClusterView`` with the original ten fields
    keep rendering as a fresh "bugün" line.
    """

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
    # ── freshness fields ──
    # last_update_at is the recency anchor: the linked arc's
    # ``last_update_at`` when the cluster belongs to an arc, else the
    # cluster's own ``created_at``. days_since_last_update is
    # ``briefing_date − that date`` (calendar days, clamped ≥0); freshness
    # is the classified state. A DEFCON-4 cluster on a 6-day-stale arc thus
    # renders "· 6 gün önce" while a genuinely-today cluster renders "bugün".
    last_update_at: datetime | None = None
    days_since_last_update: int = 0
    freshness: str = Freshness.FRESH.value


@dataclass(frozen=True)
class ArcView:
    """Subset of an arc row that the writer/fallback consume.

    The trailing five fields (``last_update_*`` triplet + ``is_active_today``
    + ``days_since_last_update``) were added by the 2026-05-25 arc-evolution
    work. They default safely so legacy test fixtures continue to construct
    ``ArcView`` with only the original eight fields · in that case the arc
    renders as stalled with a 0-day staleness, falling back to the seed
    ``summary``/``headline`` via the renderer's NULL-safe paths.
    """

    id: str
    headline: str
    summary: str
    state: str
    peak_defcon: int
    category: str | None
    last_update_at: datetime | None
    created_at: datetime | None
    # ── arc-evolution fields ──
    # last_update_* mirror the corresponding columns on `arcs`; they evolve
    # with each joining cluster while headline/summary stay at seed values.
    last_update_summary: str = ""
    last_update_headline: str = ""
    last_update_cluster_id: str | None = None
    # is_active_today is True when last_update_at falls within this run's
    # window (started_at..now). The renderer branches on it: active arcs
    # show a Güncelleme · prefix, stalled arcs show an italic stalled
    # marker plus a Son güncelleme · X gün önce line.
    is_active_today: bool = False
    # days_since_last_update is briefing_date − last_update_at.date(). Drives
    # the recency suffix (bugün/dün/N gün önce) and the freshness state.
    days_since_last_update: int = 0
    # freshness (2026-05-29 Group-A) is the surfacing state derived from
    # days_since_last_update: FRESH (<2d), DORMANT (2-6d), EXPIRED (≥7d).
    # The deterministic AÇIK GELİŞMELER renderer excludes EXPIRED and the
    # TTS preprocessor drops DORMANT lines from the spoken text.
    freshness: str = Freshness.FRESH.value


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
    *,
    target_date: date | None = None,
) -> BriefingPayload:
    """Read this run's clusters/arcs/log entries and return a payload.

    ``target_date`` is the TR-local briefing date · when provided it
    directly drives :attr:`BriefingPayload.date` (and therefore the
    on-disk briefing path). When omitted (legacy callers), the date is
    derived from ``pipeline_runs.started_at`` which is stored in UTC ·
    that path produced the 2026-05-23 / 24 mismatch when the run
    crossed midnight TR-local (00:28 TR = 21:28 UTC). All production
    callers go through the orchestrator / CLI and pass ``target_date``
    explicitly; the fallback exists for backward compat with any test
    fixture that hasn't been migrated. See
    ``docs/implementations/2026-05-24-date-propagation-fix.md``.
    """
    run_row = conn.execute(
        "SELECT started_at, stages_done FROM pipeline_runs WHERE run_id = ?",
        [run_id],
    ).fetchone()
    if run_row is None:
        raise ValueError(f"No pipeline_runs row for run_id={run_id!r}")
    started_at, stages_json = run_row
    if target_date is not None:
        briefing_date = target_date
    else:
        briefing_date = (started_at or datetime.utcnow()).date()
    stages_done = json.loads(stages_json) if stages_json else []

    clusters_by_defcon = _load_clusters(conn, run_id, briefing_date)
    # _load_arcs needs the briefing date (for days_since_last_update) and
    # the run window (for is_active_today). started_at is fetched again
    # inside _load_arcs against the same run_id · briefing_date is computed
    # above and threaded through here.
    open_arc_updates, resolved_arcs = _load_arcs(conn, run_id, briefing_date)
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


def _days_between(briefing_date: date, dt: datetime | None) -> int:
    """Calendar days from ``dt`` to ``briefing_date``, clamped ≥0.

    ``None`` → 0 (treated as today). Shared by the cluster and arc
    loaders so the recency a cluster shows and the recency its arc shows
    are computed identically.
    """
    if dt is None:
        return 0
    return max((briefing_date - dt.date()).days, 0)


def _load_clusters(
    conn: duckdb.DuckDBPyConnection, run_id: str, briefing_date: date
) -> dict[int, list[ClusterView]]:
    """Load this run's scored clusters, bucketed by final DEFCON.

    Each cluster carries a freshness triple (2026-05-29 Group-A) used by
    the deterministic DEFCON-4 / AMBİYANS renderers to show recency. The
    recency anchor is the linked arc's ``last_update_at`` when the cluster
    belongs to an arc (a routine update to a 6-day-stale thread reads
    "6 gün önce"), else the cluster's own ``created_at`` (a brand-new
    DEFCON-4 cluster reads "bugün"). The ``LEFT JOIN arcs`` keeps clusters
    with no arc.
    """
    cursor = conn.execute(
        """
        SELECT c.id, c.headline, c.summary, c.category, c.final_defcon,
               c.confidence, c.bands_present, c.arc_id,
               c.created_at, arc.last_update_at
          FROM clusters c
          JOIN cluster_articles ca ON ca.cluster_id = c.id
          JOIN articles a ON a.id = ca.article_id
          JOIN ingest_log l ON l.source_id = a.source_id AND l.run_id = ?
          LEFT JOIN arcs arc ON arc.id = c.arc_id
         WHERE c.final_defcon IS NOT NULL
         GROUP BY c.id, c.headline, c.summary, c.category, c.final_defcon,
                  c.confidence, c.bands_present, c.arc_id,
                  c.created_at, arc.last_update_at
         ORDER BY c.final_defcon ASC, c.id
        """,
        [run_id],
    )
    rows = cursor.fetchall()
    out: dict[int, list[ClusterView]] = {}
    for (
        cid,
        headline,
        summary,
        category,
        final_defcon,
        conf,
        bands_json,
        arc_id,
        created_at,
        arc_last_update,
    ) in rows:
        bands = json.loads(bands_json) if bands_json else []
        sources = _load_cluster_sources(conn, cid)
        social_bands = {"social_x", "social_reddit"}
        non_social = [b for b in bands if b not in social_bands]
        anchor = arc_last_update or created_at
        days = _days_between(briefing_date, anchor)
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
                last_update_at=anchor,
                days_since_last_update=days,
                freshness=freshness_from_days(days).value,
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
    conn: duckdb.DuckDBPyConnection, run_id: str, briefing_date: date
) -> tuple[list[ArcView], list[ArcView]]:
    """Open arcs that received updates today + arcs that resolved today.

    Each ArcView is enriched with the arc-evolution triplet (last_update_*)
    plus two computed flags:

    * ``is_active_today`` — True when ``last_update_at`` falls within this
      run's window. The window is ``started_at..now`` (we use ``now`` rather
      than the row's ``completed_at`` because the writer runs before
      ``completed_at`` is set). Any arc the linker touched in this run will
      have ``last_update_at = cluster.created_at`` (set within seconds of
      run start) so the comparison ``>= started_at`` cleanly catches it.
    * ``days_since_last_update`` — ``briefing_date − last_update_at.date()``
      in days. Used by the stalled-arc header to show how stale the arc has
      become. ``0`` when ``last_update_at`` is NULL (legacy arcs created
      before migration 004's backfill ran; the renderer treats them as
      active-today candidates with a 0-day staleness).

    Schema columns ``last_update_summary``/``last_update_headline``/
    ``last_update_cluster_id`` were added in migration 004. They may be
    NULL on rows that pre-date the backfill · the renderer falls back to
    the seed ``summary``/``headline`` in that case.
    """
    started_at = conn.execute(
        "SELECT started_at FROM pipeline_runs WHERE run_id = ?",
        [run_id],
    ).fetchone()
    run_started = started_at[0] if started_at else None

    def _is_active_today(last_update_at: datetime | None) -> bool:
        if last_update_at is None or run_started is None:
            return False
        return last_update_at >= run_started

    # All OPEN arcs surface in AÇIK GELİŞMELER — not just those a cluster
    # touched this run (2026-05-29 Group-A). That older this-run filter
    # hid DORMANT arcs (idle 2-6 days, no new cluster today) entirely,
    # which is exactly the recency the brief wants shown. The freshness
    # axis (computed below from the briefing date) decides surfacing:
    # FRESH/DORMANT stay, EXPIRED is excluded by the renderer. After the
    # lifecycle pass auto-resolves expired arcs, OPEN is naturally bounded
    # to the last ``EXPIRE_DAYS`` of active threads.
    open_cursor = conn.execute(
        """
        SELECT a.id, a.headline, a.summary, a.state, a.peak_defcon,
               a.category, a.last_update_at, a.created_at,
               a.last_update_summary, a.last_update_headline,
               a.last_update_cluster_id
          FROM arcs a
         WHERE a.state = ?
         ORDER BY a.peak_defcon ASC, a.id
        """,
        [ArcState.OPEN.value],
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
            last_update_summary=r[8] or "",
            last_update_headline=r[9] or "",
            last_update_cluster_id=r[10],
            is_active_today=_is_active_today(r[6]),
            days_since_last_update=_days_between(briefing_date, r[6]),
            freshness=freshness_from_days(_days_between(briefing_date, r[6])).value,
        )
        for r in open_cursor.fetchall()
    ]

    resolved_cursor = conn.execute(
        """
        SELECT a.id, a.headline, a.summary, a.state, a.peak_defcon,
               a.category, a.last_update_at, a.created_at,
               a.last_update_summary, a.last_update_headline,
               a.last_update_cluster_id
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
            last_update_summary=r[8] or "",
            last_update_headline=r[9] or "",
            last_update_cluster_id=r[10],
            is_active_today=_is_active_today(r[6]),
            days_since_last_update=_days_between(briefing_date, r[6]),
            freshness=freshness_from_days(_days_between(briefing_date, r[6])).value,
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

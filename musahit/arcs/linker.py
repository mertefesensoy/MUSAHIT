"""Arc-link orchestrator.

Reads scored clusters from this run that don't yet belong to an arc,
orders them by ``final_defcon`` ascending (lower integer = more severe,
so the most severe clusters seed arcs first), and for each cluster
either attaches it to an existing OPEN/WATCH arc or creates a new arc.
After every cluster is processed, runs the state-transition cleanup
pass so the OPEN/WATCH/RESOLVED partition reflects today.

Severity direction: ``DEFCON 0`` is the most severe, ``DEFCON 5`` the
least. ``arcs.peak_defcon`` is the smallest integer (most severe) ever
seen in the arc · `peak` in the severity sense, NOT the integer sense.
Updates therefore use ``min(arc.peak_defcon, cluster.final_defcon)``.
This is the same direction issue ADR-005 had to amend in 2026-05-23;
ADR-008's prose still reads ``max(...)`` but the IntEnum direction
makes ``min`` the correct operator. See the implementation doc for the
flagged ADR-008 prose-bug parallel.

FK workaround per ``memory/MEMORY.md``: UPDATE on ``arcs`` would fire
the FK check from ``arc_centroids`` and ``clusters`` even outside an
explicit transaction. The workaround is DELETE child rows → UPDATE
parent → re-INSERT, each statement auto-committing. Same pattern for
``UPDATE clusters SET arc_id = ?`` where ``cluster_articles``,
``cluster_embeddings``, and ``promotion_log`` are children.

The promotion_log entry to the workaround was added on 2026-05-24
after the first smoke run cascaded: 240 clusters all tried to seed
``arc_20260523_0001`` because the missing promotion_log handling made
every cluster UPDATE raise. See
``docs/implementations/2026-05-24-arc-link-bug-fix.md``. Any future
table whose FK targets ``clusters.id`` MUST be added to both
:meth:`ArcLinker._update_cluster_arc_id` and
:meth:`ArcLinker._update_cluster_arc_id_to_value` (per
``memory/MEMORY.md`` § "DuckDB FK Update Pattern").
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import duckdb

from musahit.arcs.matching import CandidateArc, match_arc
from musahit.arcs.stopwords import filter_stopwords
from musahit.arcs.transitions import transition_states
from musahit.cluster.centroid import compute_centroid
from musahit.common.logging import get_logger
from musahit.common.time import utcnow
from musahit.common.types import ArcState
from musahit.score.defcon import DEFCON

_log = get_logger("musahit.arcs")

# Per ADR-008 / matching defaults; constants here let the orchestrator
# log them next to outcomes if needed.
COSINE_THRESHOLD: float = 0.55
JACCARD_THRESHOLD: float = 0.4
WINDOW_DAYS: int = 30


# ── Data shapes ────────────────────────────────────────────────────────────


@dataclass
class _ClusterRow:
    """In-memory snapshot of a cluster that the linker needs to place."""

    id: str
    final_defcon: int
    headline: str
    summary: str
    category: str | None
    created_at: datetime
    centroid: list[float]
    entities: set[str]


@dataclass
class _ArcState:
    """Mutable in-memory state for an existing or freshly created arc."""

    id: str
    state: ArcState
    last_update_at: datetime
    peak_defcon: int
    entities: set[str]
    centroid: list[float]
    member_cluster_ids: list[str] = field(default_factory=list)


@dataclass
class _ArcCache:
    """All OPEN/WATCH arcs from the last 30 days, loaded once per run."""

    arcs: dict[str, _ArcState] = field(default_factory=dict)

    def candidate_view(self) -> list[CandidateArc]:
        return [
            CandidateArc(
                arc_id=a.id,
                centroid=a.centroid,
                entity_set=frozenset(a.entities),
            )
            for a in self.arcs.values()
        ]


# ── ArcLinker ──────────────────────────────────────────────────────────────


class ArcLinker:
    """Orchestrates a full arc-link pass over a run's scored clusters."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn

    # ── Public ──────────────────────────────────────────────────────────

    async def run(self, run_id: str) -> dict[str, int]:
        log = _log.bind(run_id=run_id)
        now = utcnow()

        cache = self._load_arc_cache(now)
        clusters = self._select_pending(run_id)
        log.info(
            "arc_link_start",
            pending_clusters=len(clusters),
            active_arcs=len(cache.arcs),
        )

        joined = 0
        seeded = 0
        errors = 0
        counter = self._next_arc_counter(now.date())

        for cluster in clusters:
            seed_attempted = False
            try:
                filtered_entities = filter_stopwords(cluster.entities)
                arc_id = match_arc(
                    cluster.centroid,
                    filtered_entities,
                    cache.candidate_view(),
                    cosine_threshold=COSINE_THRESHOLD,
                    jaccard_threshold=JACCARD_THRESHOLD,
                )
                if arc_id is not None:
                    self._attach_cluster(cache, arc_id, cluster, filtered_entities)
                    joined += 1
                else:
                    seed_attempted = True
                    new_arc = self._seed_arc(cache, cluster, filtered_entities, counter)
                    seeded += 1
                    log.debug("arc_link_new", arc_id=new_arc.id, cluster_id=cluster.id)
            except Exception as exc:
                log.warning(
                    "arc_link_cluster_failed",
                    cluster_id=cluster.id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                errors += 1
            finally:
                # Advance the counter whether _seed_arc succeeded or raised.
                # A failed seed still consumes its arc_id (manual rollback in
                # _seed_arc removes the orphan arc + arc_centroids rows). If
                # the counter stayed put on failure, every subsequent cluster
                # would retry the same arc_id and the rollback path would
                # bury the real diagnostic under duplicate-PK errors · this
                # is exactly the cascade observed on 2026-05-23.
                if seed_attempted:
                    counter += 1

        transitions = transition_states(self._conn, now)
        self._mark_stage_done(run_id, joined, seeded, transitions)
        log.info(
            "arc_link_done",
            joined=joined,
            seeded=seeded,
            errors=errors,
            **transitions,
        )
        return {
            "joined": joined,
            "seeded": seeded,
            "errors": errors,
            **transitions,
        }

    # ── Reads ───────────────────────────────────────────────────────────

    def _load_arc_cache(self, now: datetime) -> _ArcCache:
        """Load all OPEN/WATCH arcs whose last_update is in the 30-day window."""
        cutoff = now - timedelta(days=WINDOW_DAYS)
        rows = self._conn.execute(
            """
            SELECT a.id, a.state, a.last_update_at, a.peak_defcon,
                   a.entity_set, c.centroid
              FROM arcs a
              JOIN arc_centroids c ON c.arc_id = a.id
             WHERE a.state IN (?, ?)
               AND (a.last_update_at IS NULL OR a.last_update_at >= ?)
            """,
            [ArcState.OPEN.value, ArcState.WATCH.value, cutoff],
        ).fetchall()
        cache = _ArcCache()
        for arc_id, state_val, last_update, peak_defcon, ent_json, centroid in rows:
            entities = set(json.loads(ent_json)) if ent_json else set()
            cache.arcs[arc_id] = _ArcState(
                id=arc_id,
                state=ArcState(state_val),
                last_update_at=last_update or now,
                peak_defcon=int(peak_defcon) if peak_defcon is not None else int(DEFCON.AMBIENT),
                entities=entities,
                centroid=list(centroid),
            )
        return cache

    def _select_pending(self, run_id: str) -> list[_ClusterRow]:
        """Scored clusters in this run that lack ``arc_id``, severity-ordered."""
        cursor = self._conn.execute(
            """
            SELECT c.id, c.final_defcon, c.headline, c.summary, c.category,
                   c.created_at, ce.centroid
              FROM clusters c
              JOIN cluster_embeddings ce ON ce.cluster_id = c.id
              JOIN cluster_articles ca ON ca.cluster_id = c.id
              JOIN articles a ON a.id = ca.article_id
              JOIN ingest_log l ON l.source_id = a.source_id AND l.run_id = ?
             WHERE c.arc_id IS NULL
               AND c.final_defcon IS NOT NULL
             GROUP BY c.id, c.final_defcon, c.headline, c.summary, c.category,
                      c.created_at, ce.centroid
             ORDER BY c.final_defcon ASC
            """,
            [run_id],
        )
        out: list[_ClusterRow] = []
        rows = cursor.fetchall()
        for cid, final_defcon, headline, summary, category, created_at, centroid in rows:
            entities = self._cluster_entities(cid)
            out.append(
                _ClusterRow(
                    id=cid,
                    final_defcon=int(final_defcon),
                    headline=headline or "",
                    summary=summary or "",
                    category=category,
                    created_at=created_at or utcnow(),
                    centroid=list(centroid),
                    entities=entities,
                )
            )
        return out

    def _cluster_entities(self, cluster_id: str) -> set[str]:
        """Union of every member article's entity-text set."""
        cursor = self._conn.execute(
            """
            SELECT a.entities
              FROM cluster_articles ca
              JOIN articles a ON a.id = ca.article_id
             WHERE ca.cluster_id = ?
            """,
            [cluster_id],
        )
        out: set[str] = set()
        for (entities_json,) in cursor.fetchall():
            if not entities_json:
                continue
            try:
                parsed = json.loads(entities_json)
            except (ValueError, TypeError):
                continue
            for item in parsed:
                if isinstance(item, dict):
                    text = item.get("text")
                elif isinstance(item, str):
                    text = item
                else:
                    text = None
                if text:
                    out.add(text)
        return out

    def _next_arc_counter(self, today: Any) -> int:
        prefix = "arc_" + today.strftime("%Y%m%d") + "_"
        row = self._conn.execute(
            "SELECT id FROM arcs WHERE id LIKE ? ORDER BY id DESC LIMIT 1",
            [prefix + "%"],
        ).fetchone()
        if row is None:
            return 1
        try:
            return int(row[0][len(prefix) :]) + 1
        except ValueError:
            return 1

    # ── Mutations ───────────────────────────────────────────────────────

    def _attach_cluster(
        self,
        cache: _ArcCache,
        arc_id: str,
        cluster: _ClusterRow,
        filtered_entities: set[str],
    ) -> None:
        """Link ``cluster`` to ``arc_id`` and refresh the arc's derived fields.

        Updates cluster.arc_id (FK workaround). Updates arc fields (FK
        workaround for arc_centroids). Recomputes the arc centroid as the
        mean of all member cluster centroids by re-querying ``cluster_embeddings``
        joined on the updated arc_id.

        ``arc.peak_defcon`` is updated with ``min`` because lower DEFCON
        integers are more severe · the *peak* (highest severity) is the
        smallest integer ever assigned. ADR-008's prose says ``max`` but
        that's the same prose-bug pattern as ADR-005 had (see impl doc).
        """
        arc = cache.arcs[arc_id]

        # 1) Update cluster.arc_id (FK workaround · cluster_articles +
        # cluster_embeddings reference clusters.id).
        self._update_cluster_arc_id(cluster.id, arc_id)

        # 2) Compute the new centroid as the mean of every now-linked cluster's
        # embedding. We re-query so the result is canonical regardless of
        # which clusters were already attached.
        new_centroid = self._compute_arc_centroid(arc_id)

        # 3) Update arc fields (FK workaround · arc_centroids references arcs.id).
        new_state = ArcState.OPEN if arc.state is ArcState.WATCH else arc.state
        # Severity direction: lower DEFCON integer = MORE severe. peak_defcon
        # is the most severe DEFCON ever seen in the arc → smallest integer →
        # use min, NOT max. ADR-008 § "Arc updates" says max; the IntEnum
        # direction makes that prose wrong (see impl doc; same pattern as the
        # ADR-005 amendment).
        new_peak = min(arc.peak_defcon, cluster.final_defcon)
        new_entities = arc.entities | filtered_entities
        self._update_arc(
            arc_id=arc_id,
            state=new_state,
            last_update_at=cluster.created_at,
            peak_defcon=new_peak,
            entity_set=new_entities,
            new_centroid=new_centroid,
        )

        # 4) Refresh the in-memory cache so subsequent clusters in this run
        # see the new state.
        arc.state = new_state
        arc.last_update_at = cluster.created_at
        arc.peak_defcon = new_peak
        arc.entities = new_entities
        arc.centroid = new_centroid

    def _seed_arc(
        self,
        cache: _ArcCache,
        cluster: _ClusterRow,
        filtered_entities: set[str],
        counter: int,
    ) -> _ArcState:
        """Create a new OPEN arc seeded from ``cluster``.

        DuckDB auto-commits each statement so there is no transaction
        to roll back if the cluster's FK-workaround update fails after
        we have already INSERTed the arc + arc_centroids rows. We
        INSERT those first (required by
        ``clusters.arc_id REFERENCES arcs.id``), then attempt the
        cluster update inside a try/except · on failure we DELETE the
        just-inserted rows by hand and re-raise so the outer loop
        counts this as an error. The counter still advances via the
        ``finally`` block in :meth:`run`, so the next cluster picks a
        fresh arc_id rather than colliding on the orphan one (this is
        the 2026-05-23 smoke-run cascade · see impl doc).
        """
        arc_id = f"arc_{cluster.created_at.strftime('%Y%m%d')}_{counter:04d}"
        now = utcnow()
        self._conn.execute(
            """
            INSERT INTO arcs (
                id, created_at, headline, summary, state, last_update_at,
                category, peak_defcon, entity_set
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                arc_id,
                now,
                cluster.headline,
                cluster.summary,
                ArcState.OPEN.value,
                cluster.created_at,
                cluster.category,
                cluster.final_defcon,
                json.dumps(sorted(filtered_entities)),
            ],
        )
        self._conn.execute(
            """
            INSERT INTO arc_centroids (arc_id, centroid, updated_at)
            VALUES (?, ?, ?)
            """,
            [arc_id, cluster.centroid, now],
        )

        # Now link the cluster to this new arc · same FK workaround applies.
        # If the workaround raises (e.g. a future child table reference we
        # have not yet wired in), DELETE the just-inserted arc rows so
        # we leave no orphan behind before re-raising.
        try:
            self._update_cluster_arc_id(cluster.id, arc_id)
        except Exception:
            self._conn.execute(
                "DELETE FROM arc_centroids WHERE arc_id = ?", [arc_id]
            )
            self._conn.execute(
                "DELETE FROM arcs WHERE id = ?", [arc_id]
            )
            raise

        new_arc = _ArcState(
            id=arc_id,
            state=ArcState.OPEN,
            last_update_at=cluster.created_at,
            peak_defcon=cluster.final_defcon,
            entities=filtered_entities,
            centroid=list(cluster.centroid),
            member_cluster_ids=[cluster.id],
        )
        cache.arcs[arc_id] = new_arc
        return new_arc

    # ── FK-aware write helpers ──────────────────────────────────────────

    def _update_cluster_arc_id(self, cluster_id: str, arc_id: str) -> None:
        """Set ``clusters.arc_id`` using the FK workaround pattern.

        DuckDB rejects ``UPDATE clusters SET arc_id = ...`` when
        ``cluster_articles`` / ``cluster_embeddings`` / ``promotion_log``
        reference the row. Workaround: snapshot every child row,
        DELETE, UPDATE, re-INSERT · each statement auto-commits · no
        explicit transaction (see ``memory/MEMORY.md`` § "DuckDB FK
        Update Pattern").
        """
        member_articles = [
            r[0]
            for r in self._conn.execute(
                "SELECT article_id FROM cluster_articles WHERE cluster_id = ?",
                [cluster_id],
            ).fetchall()
        ]
        embedding_row = self._conn.execute(
            """
            SELECT centroid, embedded_at
              FROM cluster_embeddings
             WHERE cluster_id = ?
            """,
            [cluster_id],
        ).fetchone()
        promotion_row = self._conn.execute(
            """
            SELECT raw_defcon, ceiling_defcon, final_defcon,
                   bands_present, sides_present, confidence,
                   rule_applied, computed_at
              FROM promotion_log
             WHERE cluster_id = ?
            """,
            [cluster_id],
        ).fetchone()

        self._conn.execute(
            "DELETE FROM cluster_articles WHERE cluster_id = ?",
            [cluster_id],
        )
        self._conn.execute(
            "DELETE FROM cluster_embeddings WHERE cluster_id = ?",
            [cluster_id],
        )
        self._conn.execute(
            "DELETE FROM promotion_log WHERE cluster_id = ?",
            [cluster_id],
        )
        self._conn.execute(
            "UPDATE clusters SET arc_id = ? WHERE id = ?",
            [arc_id, cluster_id],
        )
        for article_id in member_articles:
            self._conn.execute(
                """
                INSERT INTO cluster_articles (cluster_id, article_id)
                VALUES (?, ?)
                ON CONFLICT (cluster_id, article_id) DO NOTHING
                """,
                [cluster_id, article_id],
            )
        if embedding_row is not None:
            self._conn.execute(
                """
                INSERT INTO cluster_embeddings (cluster_id, centroid, embedded_at)
                VALUES (?, ?, ?)
                ON CONFLICT (cluster_id) DO NOTHING
                """,
                [cluster_id, embedding_row[0], embedding_row[1]],
            )
        if promotion_row is not None:
            self._conn.execute(
                """
                INSERT INTO promotion_log (
                    cluster_id, raw_defcon, ceiling_defcon, final_defcon,
                    bands_present, sides_present, confidence,
                    rule_applied, computed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (cluster_id) DO NOTHING
                """,
                [cluster_id, *promotion_row],
            )

    def _update_arc(
        self,
        arc_id: str,
        state: ArcState,
        last_update_at: datetime,
        peak_defcon: int,
        entity_set: set[str],
        new_centroid: list[float],
    ) -> None:
        """Update arcs row + arc_centroids row using the FK workaround.

        ``arc_centroids.arc_id`` references ``arcs.id``; ``clusters.arc_id``
        also references it. We snapshot the children, DELETE arc_centroids
        (clusters do NOT need to be deleted · only one child table per
        update direction needs the workaround at a time; clusters keeping
        their arc_id during the UPDATE is fine because we are not
        modifying clusters.arc_id here).

        Actually re-reading DuckDB's docs: an UPDATE on a parent row fires
        the FK check for EVERY referencing table, even if the column being
        updated isn't referenced. So both arc_centroids AND any clusters
        pointing at this arc would trip the check. We clear arc_centroids,
        UPDATE the arcs row, then re-INSERT arc_centroids. Clusters'
        arc_id values stay pointing at the same arc id (the id doesn't
        change), so DuckDB's check on the referencing column passes
        trivially.
        """
        # arc_centroids is the only child we need to DELETE + reinsert
        # because the UPDATE on arcs DOES fire FK checks regardless of
        # whether arc.id changes. Clusters pointing at the arc reference
        # arc.id (which we're not changing) · leaving them alone is fine
        # AS LONG AS the FK enforcement is "id is still valid post-UPDATE".
        # Empirically (see step 11 + memory/MEMORY.md), the safest pattern
        # is DELETE all FK-referencing rows that block the UPDATE. The old
        # arc_centroids row is replaced wholesale by ``new_centroid`` below;
        # snapshotting it is unnecessary.
        member_cluster_ids = [
            r[0]
            for r in self._conn.execute(
                "SELECT id FROM clusters WHERE arc_id = ?", [arc_id]
            ).fetchall()
        ]

        # Clear referencing rows.
        self._conn.execute(
            "DELETE FROM arc_centroids WHERE arc_id = ?", [arc_id]
        )
        # Clusters reference the arc; updating each to NULL temporarily
        # avoids the FK trigger. The cluster's arc_id is restored at the
        # end. Note: clusters has its own children (cluster_articles,
        # cluster_embeddings) so each cluster.arc_id update needs the
        # cluster-level workaround as well.
        for cluster_id in member_cluster_ids:
            self._update_cluster_arc_id_to_value(cluster_id, None)

        # UPDATE the arc itself.
        now = utcnow()
        self._conn.execute(
            """
            UPDATE arcs
               SET state           = ?,
                   last_update_at  = ?,
                   peak_defcon     = ?,
                   entity_set      = ?
             WHERE id = ?
            """,
            [
                state.value,
                last_update_at,
                peak_defcon,
                json.dumps(sorted(entity_set)),
                arc_id,
            ],
        )

        # Re-INSERT children.
        self._conn.execute(
            """
            INSERT INTO arc_centroids (arc_id, centroid, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT (arc_id) DO NOTHING
            """,
            [arc_id, new_centroid, now],
        )
        for cluster_id in member_cluster_ids:
            self._update_cluster_arc_id_to_value(cluster_id, arc_id)

    def _update_cluster_arc_id_to_value(
        self, cluster_id: str, arc_id: str | None
    ) -> None:
        """Same FK workaround as :meth:`_update_cluster_arc_id` for arbitrary value.

        Kept in lock-step with :meth:`_update_cluster_arc_id` · any new
        child table referencing ``clusters.id`` must be added here too.
        """
        member_articles = [
            r[0]
            for r in self._conn.execute(
                "SELECT article_id FROM cluster_articles WHERE cluster_id = ?",
                [cluster_id],
            ).fetchall()
        ]
        embedding_row = self._conn.execute(
            "SELECT centroid, embedded_at FROM cluster_embeddings WHERE cluster_id = ?",
            [cluster_id],
        ).fetchone()
        promotion_row = self._conn.execute(
            """
            SELECT raw_defcon, ceiling_defcon, final_defcon,
                   bands_present, sides_present, confidence,
                   rule_applied, computed_at
              FROM promotion_log
             WHERE cluster_id = ?
            """,
            [cluster_id],
        ).fetchone()

        self._conn.execute(
            "DELETE FROM cluster_articles WHERE cluster_id = ?", [cluster_id]
        )
        self._conn.execute(
            "DELETE FROM cluster_embeddings WHERE cluster_id = ?", [cluster_id]
        )
        self._conn.execute(
            "DELETE FROM promotion_log WHERE cluster_id = ?", [cluster_id]
        )
        self._conn.execute(
            "UPDATE clusters SET arc_id = ? WHERE id = ?", [arc_id, cluster_id]
        )
        for article_id in member_articles:
            self._conn.execute(
                "INSERT INTO cluster_articles (cluster_id, article_id) "
                "VALUES (?, ?) ON CONFLICT DO NOTHING",
                [cluster_id, article_id],
            )
        if embedding_row is not None:
            self._conn.execute(
                "INSERT INTO cluster_embeddings (cluster_id, centroid, embedded_at) "
                "VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                [cluster_id, embedding_row[0], embedding_row[1]],
            )
        if promotion_row is not None:
            self._conn.execute(
                """
                INSERT INTO promotion_log (
                    cluster_id, raw_defcon, ceiling_defcon, final_defcon,
                    bands_present, sides_present, confidence,
                    rule_applied, computed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (cluster_id) DO NOTHING
                """,
                [cluster_id, *promotion_row],
            )

    # ── Centroid recompute ──────────────────────────────────────────────

    def _compute_arc_centroid(self, arc_id: str) -> list[float]:
        """Mean of every linked cluster's centroid (post-link)."""
        rows = self._conn.execute(
            """
            SELECT ce.centroid
              FROM clusters c
              JOIN cluster_embeddings ce ON ce.cluster_id = c.id
             WHERE c.arc_id = ?
            """,
            [arc_id],
        ).fetchall()
        if not rows:
            return []
        vectors = [list(r[0]) for r in rows]
        return compute_centroid(vectors)

    # ── stages_done ─────────────────────────────────────────────────────

    def _mark_stage_done(
        self,
        run_id: str,
        joined: int,
        seeded: int,
        transitions: dict[str, int],
    ) -> None:
        row = self._conn.execute(
            "SELECT stages_done, counts FROM pipeline_runs WHERE run_id = ?",
            [run_id],
        ).fetchone()
        stages = json.loads(row[0]) if row and row[0] else []
        counts: dict[str, Any] = json.loads(row[1]) if row and row[1] else {}
        if "arc-link" not in stages:
            stages.append("arc-link")
        counts["arcs_joined"] = joined
        counts["arcs_seeded"] = seeded
        counts.update({f"arc_{k}": v for k, v in transitions.items()})
        self._conn.execute(
            "UPDATE pipeline_runs SET stages_done = ?, counts = ? WHERE run_id = ?",
            [json.dumps(stages), json.dumps(counts), run_id],
        )


__all__ = [
    "COSINE_THRESHOLD",
    "JACCARD_THRESHOLD",
    "WINDOW_DAYS",
    "ArcLinker",
]

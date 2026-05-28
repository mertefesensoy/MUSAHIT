"""Cluster orchestrator: embed + greedy single-pass cosine clustering.

Reads articles from this run that lack embeddings, embeds them via the
injected :class:`EmbeddingClient`, partitions by language, and assigns
each article to either an existing cluster (cosine ≥ threshold within
the 24-hour window) or a fresh one — single greedy pass over each
language bucket sorted by ``published_at``.

Order-dependence: the greedy pass is sensitive to the order articles
arrive in. We sort by ``published_at`` ascending so a re-run of the
same input produces the same MEMBERSHIP partition; cluster IDs may
shift if the daily counter has changed (e.g., manual cluster creation),
but which-article-with-which-other-article is stable.

Filters (per the build-plan):

* ``word_count < 10`` → skip entirely (Reddit empty-body case; no
  embedding written, no cluster row touched).
* ``word_count < 30`` → "headline-only": embedded and considered for
  joining an existing cluster, but cannot SEED a new cluster. Skipped
  if no cluster matches.
* ``word_count ≥ 30`` → full-text: joins best match if any, otherwise
  creates a new cluster.

Cluster IDs follow the ``cl_YYYYMMDD_NNNN`` pattern from ADR-006. The
daily counter advances across languages — one global sequence per day.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import duckdb

from musahit.cluster.centroid import compute_centroid, cosine_similarity
from musahit.cluster.embedder import DIMENSION, EmbeddingClient
from musahit.common.logging import get_logger
from musahit.common.time import utcnow

_log = get_logger("musahit.cluster")

DEFAULT_COSINE_THRESHOLD: float = 0.7
DEFAULT_WINDOW_HOURS: int = 24
EMBED_BATCH_LIMIT: int = 50  # rate-limit batches passed to the embedder

# Filter thresholds (per build-plan). Tweakable, but moving them is an
# operational decision that should surface in the implementation doc.
MIN_WORD_COUNT_FOR_EMBEDDING: int = 10
MIN_WORD_COUNT_FOR_NEW_CLUSTER: int = 30


class EmbeddingUnavailableError(RuntimeError):
    """Raised when the embedder cannot supply a vector for every eligible
    article. Surfaces as a clean, re-runnable stage failure instead of
    the cryptic ``zip() argument 2 is shorter than argument 1`` that the
    strict zip would otherwise emit.

    The stage that catches this in the orchestrator marks ``cluster`` as
    a failed_stages entry · ``cluster`` does NOT enter ``stages_done`` ·
    so ``--stage cluster --force`` on a later run will retry cleanly.
    """


# ── Data shapes ────────────────────────────────────────────────────────────


@dataclass
class _Article:
    id: str
    source_id: str
    band: str  # JSON value from sources.band
    title: str
    lead: str
    published_at: datetime
    language: str
    word_count: int


@dataclass
class _ClusterState:
    """In-memory accumulator. Persisted to DB at end of run."""

    id: str
    created_at: datetime
    last_update_at: datetime
    centroid: list[float]
    member_ids: list[str] = field(default_factory=list)
    bands: set[str] = field(default_factory=set)
    member_vectors: list[list[float]] = field(default_factory=list)


# ── Clusterer class ────────────────────────────────────────────────────────


class Clusterer:
    """Greedy single-pass cosine clusterer over bge-m3 embeddings."""

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        embedder: EmbeddingClient,
        cosine_threshold: float = DEFAULT_COSINE_THRESHOLD,
        window_hours: int = DEFAULT_WINDOW_HOURS,
    ) -> None:
        self._conn = conn
        self._embedder = embedder
        self._threshold = cosine_threshold
        self._window = timedelta(hours=window_hours)

    # ── Public entry point ──────────────────────────────────────────────

    async def run(self, run_id: str) -> dict[str, int]:
        log = _log.bind(run_id=run_id)
        articles = self._select_eligible(run_id)
        log.info("cluster_start", eligible=len(articles))

        # Phase 1: filter + embed. Articles below the embedding floor are
        # dropped entirely; everything else needs a vector for either
        # cluster joining (headline-only) or cluster creation (full).
        embeddable = [a for a in articles if a.word_count >= MIN_WORD_COUNT_FOR_EMBEDDING]
        skipped_empty = len(articles) - len(embeddable)

        # Empty-input fast path · no eligible articles this run means
        # nothing to embed and nothing to cluster. The stage marks done
        # cleanly · this is the steady-state idempotent re-run case.
        if not embeddable:
            self._mark_stage_done(run_id, new_clusters=0, joins=0)
            log.info(
                "cluster_done",
                new=0,
                joined=0,
                skipped_headline=0,
                skipped_empty=skipped_empty,
            )
            return {
                "new_clusters": 0,
                "joins": 0,
                "skipped_headline": 0,
                "skipped_empty": skipped_empty,
            }

        vectors = await self._embed_articles(embeddable)

        # Total-failure guard (Issue 1) · _embed_articles' wrapper
        # returned [] because the embed call raised at the transport
        # level (Ollama unreachable). Distinguish from "successfully
        # embedded zero" which Solution B no longer produces · the
        # embedder now returns one slot per input.
        if embeddable and not vectors:
            log.warning(
                "cluster_embed_incomplete",
                expected=len(embeddable),
                got=0,
            )
            raise EmbeddingUnavailableError(
                f"embedding returned 0 vectors for {len(embeddable)} "
                f"articles · cluster stage cannot proceed"
            )

        # Length contract · Solution B requires the embedder to return
        # one slot per input (a vector or None). Anything else is a
        # contract violation · fail loudly.
        if len(vectors) != len(embeddable):
            log.error(
                "cluster_embed_contract_violation",
                expected=len(embeddable),
                got=len(vectors),
            )
            raise EmbeddingUnavailableError(
                f"embedder returned {len(vectors)} vectors for "
                f"{len(embeddable)} articles · alignment broken"
            )

        # Solution B · partial alignment. Filter Nones to obtain the
        # (article, vector) tuples that can actually be clustered. The
        # Nones are the bge-m3-poison articles (or transport/validation
        # failures); they are dropped this run · their article rows
        # remain WITHOUT an embedding so a later re-run picks them up.
        embedded_pairs: list[tuple[_Article, list[float]]] = [
            (a, v)
            for a, v in zip(embeddable, vectors, strict=True)
            if v is not None
        ]
        dropped = len(embeddable) - len(embedded_pairs)
        if dropped:
            dropped_ids = [
                a.id
                for a, v in zip(embeddable, vectors, strict=True)
                if v is None
            ]
            log.warning(
                "cluster_embed_partial",
                dropped=dropped,
                clustered=len(embedded_pairs),
                dropped_ids=dropped_ids[:20],
            )
        if not embedded_pairs:
            raise EmbeddingUnavailableError(
                f"all {len(embeddable)} articles failed to embed · "
                f"cluster cannot proceed"
            )

        # Persist embeddings ONLY for the successful pairs.
        self._persist_embeddings(
            [a for a, _ in embedded_pairs],
            [v for _, v in embedded_pairs],
        )

        # Phase 2: language-partitioned greedy single-pass.
        # bucket → ordered list of (article, vector) tuples by published_at.
        buckets: dict[str, list[tuple[_Article, list[float]]]] = {}
        for a, v in embedded_pairs:
            buckets.setdefault(a.language, []).append((a, v))
        for items in buckets.values():
            items.sort(key=lambda pair: pair[0].published_at)

        counter = self._next_counter(utcnow().date())
        clusters_created: list[_ClusterState] = []
        join_count = 0
        new_count = 0
        skipped_headline = 0

        for language, pairs in buckets.items():
            language_clusters: list[_ClusterState] = []
            for article, vector in pairs:
                best = self._best_match(article, vector, language_clusters)
                if best is not None:
                    self._add_to_cluster(best, article, vector)
                    join_count += 1
                    continue
                if article.word_count < MIN_WORD_COUNT_FOR_NEW_CLUSTER:
                    log.debug(
                        "cluster_headline_no_match",
                        article_id=article.id,
                        language=language,
                    )
                    skipped_headline += 1
                    continue
                new_cluster = self._create_cluster(article, vector, counter)
                counter += 1
                language_clusters.append(new_cluster)
                clusters_created.append(new_cluster)
                new_count += 1

        self._persist_clusters(clusters_created)
        self._mark_stage_done(run_id, new_count, join_count)

        log.info(
            "cluster_done",
            new=new_count,
            joined=join_count,
            skipped_headline=skipped_headline,
            skipped_empty=skipped_empty,
        )
        return {
            "new_clusters": new_count,
            "joins": join_count,
            "skipped_headline": skipped_headline,
            "skipped_empty": skipped_empty,
        }

    # ── Reads ───────────────────────────────────────────────────────────

    def _select_eligible(self, run_id: str) -> list[_Article]:
        """Return articles ingested in ``run_id`` that lack embeddings."""
        cursor = self._conn.execute(
            """
            SELECT a.id, a.source_id, s.band, a.title, a.lead,
                   a.published_at, a.language, a.word_count
              FROM articles a
              JOIN sources s ON s.id = a.source_id
              JOIN ingest_log l ON l.source_id = a.source_id AND l.run_id = ?
              LEFT JOIN article_embeddings e ON e.article_id = a.id
             WHERE e.article_id IS NULL
            """,
            [run_id],
        )
        rows = cursor.fetchall()
        out: list[_Article] = []
        for id_, src_id, band, title, lead, published_at, language, wc in rows:
            out.append(
                _Article(
                    id=id_,
                    source_id=src_id,
                    band=band or "",
                    title=title or "",
                    lead=lead or "",
                    published_at=published_at or utcnow(),
                    language=language or "unknown",
                    word_count=int(wc or 0),
                )
            )
        return out

    def _next_counter(self, today: Any) -> int:
        """Find the next ``NNNN`` for ``cl_YYYYMMDD_*`` cluster IDs."""
        prefix = "cl_" + today.strftime("%Y%m%d") + "_"
        row = self._conn.execute(
            "SELECT id FROM clusters WHERE id LIKE ? ORDER BY id DESC LIMIT 1",
            [prefix + "%"],
        ).fetchone()
        if row is None:
            return 1
        tail = row[0][len(prefix) :]
        try:
            return int(tail) + 1
        except ValueError:
            return 1

    # ── Embedding phase ─────────────────────────────────────────────────

    async def _embed_articles(
        self, articles: list[_Article]
    ) -> list[list[float] | None]:
        """Build the embedding inputs and call the embedder.

        Returns the embedder's aligned ``list[list[float] | None]``
        directly · Nones at positions where a single article could not
        be embedded (Solution B). On a TOTAL embed exception (Ollama
        unreachable, etc.) returns ``[]`` so ``run()``'s Issue-1 guard
        can raise :class:`EmbeddingUnavailableError` cleanly · do NOT
        swallow per-item Nones, they are normal Solution-B output.
        """
        texts = [self._embedding_input(a) for a in articles]
        if not texts:
            return []
        try:
            return await self._embedder.embed(texts)
        except Exception as exc:
            _log.warning(
                "cluster_embed_failed",
                count=len(texts),
                error=f"{type(exc).__name__}: {exc}",
                stage_impact="cluster_will_fail_and_be_rerunnable",
            )
            return []

    def _embedding_input(self, article: _Article) -> str:
        """``title + "\\n\\n" + lead`` — the production input shape."""
        return f"{article.title}\n\n{article.lead}".strip()

    # ── Greedy single-pass core ─────────────────────────────────────────

    def _best_match(
        self,
        article: _Article,
        vector: list[float],
        candidates: list[_ClusterState],
    ) -> _ClusterState | None:
        """Return the in-window cluster with the highest cosine ≥ threshold."""
        best: _ClusterState | None = None
        best_sim = -1.0
        for cluster in candidates:
            if article.published_at - cluster.last_update_at > self._window:
                continue
            sim = cosine_similarity(vector, cluster.centroid)
            if sim >= self._threshold and sim > best_sim:
                best = cluster
                best_sim = sim
        return best

    def _add_to_cluster(
        self, cluster: _ClusterState, article: _Article, vector: list[float]
    ) -> None:
        cluster.member_ids.append(article.id)
        cluster.member_vectors.append(vector)
        cluster.bands.add(article.band)
        cluster.centroid = compute_centroid(cluster.member_vectors)
        if article.published_at > cluster.last_update_at:
            cluster.last_update_at = article.published_at

    def _create_cluster(
        self,
        article: _Article,
        vector: list[float],
        counter: int,
    ) -> _ClusterState:
        date_part = utcnow().date().strftime("%Y%m%d")
        cluster_id = f"cl_{date_part}_{counter:04d}"
        now = utcnow()
        return _ClusterState(
            id=cluster_id,
            created_at=now,
            last_update_at=article.published_at,
            centroid=list(vector),
            member_ids=[article.id],
            bands={article.band},
            member_vectors=[list(vector)],
        )

    # ── Writes ──────────────────────────────────────────────────────────

    def _persist_embeddings(
        self,
        articles: Iterable[_Article],
        vectors: list[list[float]],
    ) -> None:
        """INSERT OR IGNORE into article_embeddings (PK = article_id)."""
        if not vectors:
            return
        now = utcnow()
        rows: list[tuple[Any, ...]] = []
        for article, vec in zip(articles, vectors, strict=True):
            if len(vec) != DIMENSION:
                _log.warning(
                    "cluster_bad_embedding_dim",
                    article_id=article.id,
                    dim=len(vec),
                )
                continue
            rows.append((article.id, vec, now))
        if not rows:
            return
        self._conn.executemany(
            """
            INSERT INTO article_embeddings (article_id, embedding, embedded_at)
            VALUES (?, ?, ?)
            ON CONFLICT (article_id) DO NOTHING
            """,
            rows,
        )

    def _persist_clusters(self, clusters: list[_ClusterState]) -> None:
        if not clusters:
            return

        now = utcnow()

        # clusters table
        self._conn.executemany(
            """
            INSERT INTO clusters (
                id, created_at, headline, summary, category,
                raw_defcon, ceiling_defcon, final_defcon, confidence,
                bands_present, arc_id, operator_override
            )
            VALUES (?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, NULL, NULL)
            ON CONFLICT (id) DO NOTHING
            """,
            [
                (c.id, c.created_at, json.dumps(sorted(c.bands)))
                for c in clusters
            ],
        )

        # cluster_articles table
        membership_rows: list[tuple[str, str]] = []
        for c in clusters:
            for article_id in c.member_ids:
                membership_rows.append((c.id, article_id))
        if membership_rows:
            self._conn.executemany(
                """
                INSERT INTO cluster_articles (cluster_id, article_id)
                VALUES (?, ?)
                ON CONFLICT (cluster_id, article_id) DO NOTHING
                """,
                membership_rows,
            )

        # cluster_embeddings table
        self._conn.executemany(
            """
            INSERT INTO cluster_embeddings (cluster_id, centroid, embedded_at)
            VALUES (?, ?, ?)
            ON CONFLICT (cluster_id) DO NOTHING
            """,
            [(c.id, c.centroid, now) for c in clusters],
        )

    def _mark_stage_done(
        self, run_id: str, new_clusters: int, joins: int
    ) -> None:
        row = self._conn.execute(
            "SELECT stages_done, counts FROM pipeline_runs WHERE run_id = ?",
            [run_id],
        ).fetchone()
        stages = json.loads(row[0]) if row and row[0] else []
        counts = json.loads(row[1]) if row and row[1] else {}
        if "cluster" not in stages:
            stages.append("cluster")
        counts["clusters_new"] = new_clusters
        counts["clusters_joins"] = joins
        self._conn.execute(
            "UPDATE pipeline_runs SET stages_done = ?, counts = ? WHERE run_id = ?",
            [json.dumps(stages), json.dumps(counts), run_id],
        )


__all__ = [
    "DEFAULT_COSINE_THRESHOLD",
    "DEFAULT_WINDOW_HOURS",
    "MIN_WORD_COUNT_FOR_EMBEDDING",
    "MIN_WORD_COUNT_FOR_NEW_CLUSTER",
    "Clusterer",
    "EmbeddingUnavailableError",
]

"""Score-stage orchestrator: worker LLM + retry + promotion + persistence.

Reads unscored clusters from this run, asks the worker model for a
:class:`WorkerResponse`, retries on validation failure, falls back to a
canned `UNCLASSIFIED + AMBIENT + low-confidence` response after the
last retry, applies promotion-ceiling rules (ADR-005), demotes the
final by one tier during the 7-day bootstrap window (ADR-004), and
writes the score back to the cluster plus an audit row in
`promotion_log` (ADR-005 § Promotion log).

Per ADR-012 a single bad cluster never aborts the stage — wrap every
cluster in try/except and log + skip on unexpected failure (this is the
outer net beyond the structured retry + fallback that already happens
inside ``_classify_one``).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import duckdb
from pydantic import ValidationError

from musahit.common.logging import get_logger
from musahit.common.time import utcnow
from musahit.common.types import Band, Category
from musahit.score.defcon import DEFCON
from musahit.score.llm_client import LlmClient
from musahit.score.promotion import (
    apply_bootstrap_demotion,
    bootstrap_demoted,
    compute_ceiling,
    confidence,
    final_defcon,
    ideological_sides,
)
from musahit.score.prompt import ClusterArticle, build_classification_prompt
from musahit.score.schema import WorkerResponse, parse_worker_response

_log = get_logger("musahit.score")

DEFAULT_MAX_RETRIES: int = 2


@dataclass
class _ClusterInput:
    """The per-cluster payload assembled by the classifier."""

    cluster_id: str
    created_at: datetime
    articles: list[ClusterArticle]
    bands: set[str]


# ── Classifier ─────────────────────────────────────────────────────────────


class Classifier:
    """Score stage: worker classification + deterministic promotion."""

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        llm: LlmClient,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._conn = conn
        self._llm = llm
        self._max_retries = max_retries

    async def run(self, run_id: str) -> dict[str, int]:
        log = _log.bind(run_id=run_id)
        clusters = self._select_pending(run_id)
        log.info("score_start", pending=len(clusters))

        scored = 0
        fallback_count = 0
        errors = 0

        for cluster in clusters:
            try:
                worker, used_fallback = await self._classify_one(cluster)
                self._persist(cluster, worker)
                scored += 1
                if used_fallback:
                    fallback_count += 1
            except Exception as exc:
                log.warning(
                    "score_cluster_failed",
                    cluster_id=cluster.cluster_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                errors += 1

        self._mark_stage_done(run_id, scored, fallback_count)
        log.info(
            "score_done",
            scored=scored,
            fallbacks=fallback_count,
            errors=errors,
        )
        return {"scored": scored, "fallbacks": fallback_count, "errors": errors}

    # ── Worker call with retry + fallback ───────────────────────────────

    async def _classify_one(
        self, cluster: _ClusterInput
    ) -> tuple[WorkerResponse, bool]:
        """Call the worker; retry on validation errors; final fallback."""
        prompt = build_classification_prompt(cluster.articles)
        attempt = 0
        last_error: Exception | None = None
        while attempt <= self._max_retries:
            raw = await self._llm.generate(prompt)
            try:
                return parse_worker_response(raw), False
            except ValidationError as exc:
                last_error = exc
                _log.debug(
                    "score_worker_invalid_response",
                    cluster_id=cluster.cluster_id,
                    attempt=attempt,
                )
                attempt += 1

        _log.warning(
            "score_worker_fallback",
            cluster_id=cluster.cluster_id,
            error=str(last_error) if last_error else "",
        )
        return _FALLBACK_RESPONSE, True

    # ── Reads ───────────────────────────────────────────────────────────

    def _select_pending(self, run_id: str) -> list[_ClusterInput]:
        """Clusters created on this run that have no DEFCON score yet."""
        cursor = self._conn.execute(
            """
            SELECT c.id, c.created_at
              FROM clusters c
              JOIN cluster_articles ca ON ca.cluster_id = c.id
              JOIN articles a ON a.id = ca.article_id
              JOIN ingest_log l ON l.source_id = a.source_id AND l.run_id = ?
             WHERE c.final_defcon IS NULL
             GROUP BY c.id, c.created_at
             ORDER BY c.created_at
            """,
            [run_id],
        )
        cluster_rows = cursor.fetchall()
        inputs: list[_ClusterInput] = []
        for cluster_id, created_at in cluster_rows:
            articles, bands = self._load_cluster_articles(cluster_id)
            inputs.append(
                _ClusterInput(
                    cluster_id=cluster_id,
                    created_at=created_at,
                    articles=articles,
                    bands=bands,
                )
            )
        return inputs

    def _load_cluster_articles(
        self, cluster_id: str
    ) -> tuple[list[ClusterArticle], set[str]]:
        cursor = self._conn.execute(
            """
            SELECT a.source_id, s.band, a.title, a.lead
              FROM cluster_articles ca
              JOIN articles a ON a.id = ca.article_id
              JOIN sources s ON s.id = a.source_id
             WHERE ca.cluster_id = ?
             ORDER BY a.published_at
            """,
            [cluster_id],
        )
        articles: list[ClusterArticle] = []
        bands: set[str] = set()
        for source_id, band, title, lead in cursor.fetchall():
            articles.append(
                ClusterArticle(
                    source_id=source_id,
                    band=band or "",
                    title=title or "",
                    lead=lead or "",
                )
            )
            if band:
                bands.add(band)
        return articles, bands

    # ── Promotion + persistence ─────────────────────────────────────────

    def _persist(self, cluster: _ClusterInput, worker: WorkerResponse) -> None:
        """Apply promotion rules + bootstrap demotion; write clusters + log.

        DuckDB FK limitation: an ``UPDATE clusters`` on a row referenced
        by ``cluster_articles`` (and/or ``cluster_embeddings``) fires the
        FK check even when no referenced column changes (see DuckDB §
        Constraints, "Foreign keys"). We work around it by snapshotting
        the membership rows, ``DELETE``-ing the referencing rows, running
        the ``UPDATE``, and re-``INSERT``-ing the membership. Each
        statement auto-commits — no explicit transaction — so the FK
        checker sees a clean ``cluster_articles`` between the DELETE and
        the UPDATE. The same workaround handles ``cluster_embeddings``.
        """
        bands_set = self._bands_to_enum(cluster.bands)
        ceiling, rule = compute_ceiling(bands_set)
        sides = sorted(ideological_sides(bands_set))
        conf = confidence(bands_set, num_sources=len(cluster.articles))
        raw = DEFCON(worker.defcon)
        capped = final_defcon(raw, ceiling)
        if bootstrap_demoted(cluster.created_at, self._conn):
            capped = apply_bootstrap_demotion(capped)

        # Snapshot all referencing rows (cluster_articles + cluster_embeddings).
        member_article_ids = [
            row[0]
            for row in self._conn.execute(
                "SELECT article_id FROM cluster_articles WHERE cluster_id = ?",
                [cluster.cluster_id],
            ).fetchall()
        ]
        embedding_row = self._conn.execute(
            """
            SELECT centroid, embedded_at
              FROM cluster_embeddings
             WHERE cluster_id = ?
            """,
            [cluster.cluster_id],
        ).fetchone()

        # Clear FK-referencing rows so UPDATE doesn't trip DuckDB's check.
        self._conn.execute(
            "DELETE FROM cluster_articles WHERE cluster_id = ?",
            [cluster.cluster_id],
        )
        self._conn.execute(
            "DELETE FROM cluster_embeddings WHERE cluster_id = ?",
            [cluster.cluster_id],
        )

        # clusters table — fill in the score columns plus headline/summary/category.
        self._conn.execute(
            """
            UPDATE clusters
               SET headline       = ?,
                   summary        = ?,
                   category       = ?,
                   raw_defcon     = ?,
                   ceiling_defcon = ?,
                   final_defcon   = ?,
                   confidence     = ?,
                   bands_present  = ?
             WHERE id = ?
            """,
            [
                worker.headline,
                worker.summary,
                worker.category.value,
                int(raw),
                int(ceiling),
                int(capped),
                conf.value,
                json.dumps(sorted(cluster.bands)),
                cluster.cluster_id,
            ],
        )

        # Re-insert the referencing rows.
        for article_id in member_article_ids:
            self._conn.execute(
                """
                INSERT INTO cluster_articles (cluster_id, article_id)
                VALUES (?, ?)
                ON CONFLICT (cluster_id, article_id) DO NOTHING
                """,
                [cluster.cluster_id, article_id],
            )
        if embedding_row is not None:
            self._conn.execute(
                """
                INSERT INTO cluster_embeddings (cluster_id, centroid, embedded_at)
                VALUES (?, ?, ?)
                ON CONFLICT (cluster_id) DO NOTHING
                """,
                [cluster.cluster_id, embedding_row[0], embedding_row[1]],
            )

        # promotion_log audit row — UPSERT so reruns are idempotent.
        self._conn.execute(
            """
            INSERT INTO promotion_log (
                cluster_id, raw_defcon, ceiling_defcon, final_defcon,
                bands_present, sides_present, confidence, rule_applied, computed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (cluster_id) DO UPDATE SET
                raw_defcon     = excluded.raw_defcon,
                ceiling_defcon = excluded.ceiling_defcon,
                final_defcon   = excluded.final_defcon,
                bands_present  = excluded.bands_present,
                sides_present  = excluded.sides_present,
                confidence     = excluded.confidence,
                rule_applied   = excluded.rule_applied,
                computed_at    = excluded.computed_at
            """,
            [
                cluster.cluster_id,
                int(raw),
                int(ceiling),
                int(capped),
                json.dumps(sorted(cluster.bands)),
                json.dumps(sides),
                conf.value,
                rule,
                utcnow(),
            ],
        )

    @staticmethod
    def _bands_to_enum(bands: Iterable[str]) -> set[Band]:
        """Coerce JSON band strings (from sources.band) to :class:`Band` enums."""
        result: set[Band] = set()
        for raw in bands:
            try:
                result.add(Band(raw))
            except ValueError:
                # Unknown band string — log via the global logger and skip.
                # The promotion rules then see this cluster as having fewer
                # bands than reality; acceptable for a defensive case the
                # registry would normally make impossible.
                _log.warning("score_unknown_band", band=raw)
        return result

    # ── stages_done ─────────────────────────────────────────────────────

    def _mark_stage_done(
        self, run_id: str, scored: int, fallback_count: int
    ) -> None:
        row = self._conn.execute(
            "SELECT stages_done, counts FROM pipeline_runs WHERE run_id = ?",
            [run_id],
        ).fetchone()
        stages = json.loads(row[0]) if row and row[0] else []
        counts: dict[str, Any] = json.loads(row[1]) if row and row[1] else {}
        if "score" not in stages:
            stages.append("score")
        counts["clusters_scored"] = scored
        counts["clusters_score_fallbacks"] = fallback_count
        self._conn.execute(
            "UPDATE pipeline_runs SET stages_done = ?, counts = ? WHERE run_id = ?",
            [json.dumps(stages), json.dumps(counts), run_id],
        )


# ── Final fallback response ────────────────────────────────────────────────

_FALLBACK_RESPONSE: WorkerResponse = WorkerResponse(
    defcon=int(DEFCON.AMBIENT),
    category=Category.UNCLASSIFIED,
    confidence_self="low",
    entities=[],
    summary="",
    headline="",
)


__all__ = [
    "DEFAULT_MAX_RETRIES",
    "Classifier",
]

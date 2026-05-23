"""Normalize stage orchestrator.

Reads ``raw_articles`` rows that have not yet been normalized for this
``run_id``, dispatches each row to a per-kind extractor, enriches the
extraction with language detection / entity tagging / lead / word_count,
and writes the canonical row into ``articles``.

Dispatch lookup is keyed on ``source.kind`` (which the orchestrator
joins from the ``sources`` table). ``content_type`` is recorded but
``source.kind`` is the authoritative discriminator — it matches the
ingester that produced the row, and the per-kind extractor it pairs
with. Each extractor is invoked with whatever subset of the row it
actually needs.

Per ADR-012 a single bad row does not abort the stage: extractor
exceptions are logged and the row is skipped. Other rows still land.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import duckdb

from musahit.common.logging import get_logger
from musahit.common.types import SourceKind
from musahit.normalize.entities import extract_entities
from musahit.normalize.extractors.html import extract_html_body
from musahit.normalize.extractors.pdf import extract_pdf_body
from musahit.normalize.extractors.reddit import extract_reddit_body
from musahit.normalize.extractors.rss import extract_rss_body
from musahit.normalize.language import detect_language

_log = get_logger("musahit.normalize")

LEAD_MAX_CHARS: int = 500


# ── Dataclasses ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExtractedArticle:
    """The canonical shape written to ``articles``.

    Extractors populate ``title``, ``body``, and (when available) the
    extractor-specific portion. The Normalizer enriches with ``lead``,
    ``language``, ``entities``, ``word_count`` before the INSERT.
    """

    title: str
    body: str
    lead: str = ""
    published_at: datetime | None = None
    language: str = ""
    entities: list[dict[str, object]] = field(default_factory=list)
    word_count: int = 0


@dataclass(frozen=True)
class RawArticleRow:
    """The subset of ``raw_articles`` the Normalizer reads."""

    id: str
    source_id: str
    url: str
    fetched_at: datetime
    raw_content: bytes
    content_type: str | None
    headers: dict[str, Any]
    canonical_timestamp: datetime | None


ExtractorFn = Callable[[RawArticleRow], ExtractedArticle]


# ── Default dispatcher ─────────────────────────────────────────────────────


def _default_extractor_for(kind: SourceKind) -> ExtractorFn | None:
    """Map ``source.kind`` to the appropriate extractor function.

    Each adapter unpacks the row to the arguments the per-kind extractor
    actually needs, then composes an :class:`ExtractedArticle` with
    ``title`` / ``body`` populated. Language / entities / lead / word_count
    are filled by :meth:`Normalizer._enrich`.
    """
    if kind is SourceKind.HTML:
        def _html(row: RawArticleRow) -> ExtractedArticle:
            title, body = extract_html_body(row.raw_content, row.source_id, row.headers)
            return ExtractedArticle(title=title, body=body, published_at=row.canonical_timestamp)
        return _html
    if kind is SourceKind.RSS:
        def _rss(row: RawArticleRow) -> ExtractedArticle:
            title, body = extract_rss_body(row.headers)
            return ExtractedArticle(title=title, body=body, published_at=row.canonical_timestamp)
        return _rss
    if kind is SourceKind.PDF:
        def _pdf(row: RawArticleRow) -> ExtractedArticle:
            title, body = extract_pdf_body(row.headers)
            return ExtractedArticle(title=title, body=body, published_at=row.canonical_timestamp)
        return _pdf
    if kind is SourceKind.API:
        def _api(row: RawArticleRow) -> ExtractedArticle:
            title, body = extract_reddit_body(row.raw_content)
            return ExtractedArticle(title=title, body=body, published_at=row.canonical_timestamp)
        return _api
    return None


# ── Normalizer class ───────────────────────────────────────────────────────


class Normalizer:
    """Orchestrates the normalize stage.

    Constructor knobs:

    * ``extractor_factory`` — overrides :func:`_default_extractor_for`.
      Tests inject a callable that returns a fake :class:`ExtractorFn`
      per :class:`SourceKind` so the dispatch path is unit-testable
      without invoking trafilatura/pdfplumber/etc.
    """

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        extractor_factory: Callable[[SourceKind], ExtractorFn | None] | None = None,
    ) -> None:
        self._conn = conn
        self._factory = extractor_factory or _default_extractor_for

    # ── Public ──────────────────────────────────────────────────────────

    async def run(self, run_id: str) -> dict[str, int]:
        """Normalize every raw_articles row from ``run_id`` not yet processed.

        Returns ``{"normalized": <count>, "skipped": <count>}``.
        """
        log = _log.bind(run_id=run_id)
        rows = self._select_pending(run_id)
        log.info("normalize_start", pending=len(rows))

        normalized = 0
        skipped = 0
        for row in rows:
            extractor = self._factory(self._source_kind_for(row.source_id))
            if extractor is None:
                log.warning(
                    "normalize_no_extractor",
                    source_id=row.source_id,
                )
                skipped += 1
                continue
            try:
                article = extractor(row)
                enriched = self._enrich(article)
                self._insert_article(row, enriched)
                normalized += 1
            except Exception as exc:
                log.warning(
                    "normalize_row_failed",
                    article_id=row.id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                skipped += 1

        self._mark_stage_done(run_id, normalized)
        log.info("normalize_done", normalized=normalized, skipped=skipped)
        return {"normalized": normalized, "skipped": skipped}

    # ── Reads ───────────────────────────────────────────────────────────

    def _select_pending(self, run_id: str) -> list[RawArticleRow]:
        """LEFT JOIN ``raw_articles`` against ``articles`` to find untouched rows.

        Scoping by ``run_id`` keys off ``ingest_log`` so a rerun of the
        normalize stage only re-evaluates rows that landed in this run's
        ingest pass. Articles already present (whether from this run or a
        prior idempotent rerun) are skipped — they're matched by
        ``article.id = raw_articles.id`` (the shared ADR-014 hash).
        """
        cursor = self._conn.execute(
            """
            SELECT r.id, r.source_id, r.url, r.fetched_at, r.raw_content,
                   r.content_type, r.headers, r.canonical_timestamp
              FROM raw_articles r
              JOIN ingest_log l ON l.source_id = r.source_id AND l.run_id = ?
              LEFT JOIN articles a ON a.id = r.id
             WHERE a.id IS NULL
            """,
            [run_id],
        )
        rows: list[RawArticleRow] = []
        for (
            id_,
            source_id,
            url,
            fetched_at,
            raw_content,
            content_type,
            headers_text,
            canonical_ts,
        ) in cursor.fetchall():
            headers = json.loads(headers_text) if headers_text else {}
            rows.append(
                RawArticleRow(
                    id=id_,
                    source_id=source_id,
                    url=url,
                    fetched_at=fetched_at,
                    raw_content=bytes(raw_content) if raw_content is not None else b"",
                    content_type=content_type,
                    headers=headers,
                    canonical_timestamp=canonical_ts,
                )
            )
        return rows

    def _source_kind_for(self, source_id: str) -> SourceKind:
        row = self._conn.execute(
            "SELECT kind FROM sources WHERE id = ?", [source_id]
        ).fetchone()
        return SourceKind(row[0]) if row else SourceKind.HTML  # safe-ish fallback

    # ── Enrichment ──────────────────────────────────────────────────────

    def _enrich(self, article: ExtractedArticle) -> ExtractedArticle:
        body = article.body or ""
        lead = body[:LEAD_MAX_CHARS]
        return dataclasses.replace(
            article,
            lead=lead,
            language=detect_language(body),
            entities=extract_entities(body),
            word_count=len(body.split()),
        )

    # ── Writes ──────────────────────────────────────────────────────────

    def _insert_article(self, row: RawArticleRow, article: ExtractedArticle) -> None:
        """INSERT-or-IGNORE the enriched article (id = raw_articles.id)."""
        self._conn.execute(
            """
            INSERT INTO articles (
                id, source_id, url, fetched_at, published_at,
                title, lead, body, language, entities, word_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO NOTHING
            """,
            [
                row.id,
                row.source_id,
                row.url,
                row.fetched_at,
                article.published_at,
                article.title,
                article.lead,
                article.body,
                article.language,
                json.dumps(article.entities, ensure_ascii=False),
                article.word_count,
            ],
        )

    def _mark_stage_done(self, run_id: str, normalized: int) -> None:
        """Append "normalize" to stages_done and add the normalize count.

        ``pipeline_runs.stages_done`` is a JSON array; we append rather
        than overwrite so the operator can see the stage ordering on a
        partially-completed run.
        """
        row = self._conn.execute(
            "SELECT stages_done, counts FROM pipeline_runs WHERE run_id = ?",
            [run_id],
        ).fetchone()
        stages = json.loads(row[0]) if row and row[0] else []
        counts = json.loads(row[1]) if row and row[1] else {}
        if "normalize" not in stages:
            stages.append("normalize")
        counts["articles_normalized"] = normalized
        self._conn.execute(
            "UPDATE pipeline_runs SET stages_done = ?, counts = ? WHERE run_id = ?",
            [json.dumps(stages), json.dumps(counts), run_id],
        )


__all__ = [
    "ExtractedArticle",
    "ExtractorFn",
    "LEAD_MAX_CHARS",
    "Normalizer",
    "RawArticleRow",
]

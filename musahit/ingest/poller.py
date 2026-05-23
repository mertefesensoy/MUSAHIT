# ============================================================================
# FILE-PROTECTED · musahit/ingest/poller.py
# Modifications require an ADR amendment + explicit operator override.
# See BOOTSTRAP.md § File protection list.
# ============================================================================
"""Ingest orchestrator (build step 8).

This module binds the four per-kind ingesters (RSS, HTML, PDF/Resmî Gazete,
API/Reddit) into one nightly run. The :class:`Ingester` Protocol from step 4
makes dispatch uniform: every implementation exposes the same async
``fetch(source) -> IngestResult`` signature, so the poller treats them as
interchangeable values keyed by ``source.kind``.

Run lifecycle (single pass through :meth:`IngestPoller.run`):

1. Upsert a ``pipeline_runs`` row with ``status=RUNNING``,
   ``started_at=utcnow()``, ``stages_done=[]``. Idempotent: re-running the
   same ``run_id`` updates the row in place.
2. Build a list of (source, ingester) pairs. Sources whose ``kind`` has no
   ingester get a ``SKIPPED`` ingest_log row immediately — defensive only;
   the current registry has no orphan kinds.
3. Fan out :func:`_run_one_source` across all sources under an
   :class:`asyncio.Semaphore` (default cap = 8). Each coroutine:
       a. takes a slot,
       b. records ``started_at``,
       c. ``asyncio.wait_for`` the ingester's fetch with the per-source
          timeout (``max(default_timeout_seconds, rate_limit_seconds * 12)``),
       d. catches timeout → ``TIMEOUT``, any other exception → ``PARSE_ERROR``,
       e. records ``completed_at`` and writes an ``ingest_log`` row,
       f. releases the slot.
4. After ``asyncio.gather`` settles, sum new-article counts, write
   ``stages_done=["ingest"]`` and ``counts={"articles": <total>}`` back to
   the ``pipeline_runs`` row. Status STAYS ``RUNNING``: the next stage in
   the pipeline (normalize) advances stages_done and eventually marks the
   run ``COMPLETED``.

Per ADR-012 the poller never aborts on a single source failure — failure
isolation is the whole point of this layer.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import duckdb

from musahit.common.config import Settings
from musahit.common.logging import get_logger
from musahit.common.time import tr_local_date, utcnow
from musahit.common.types import IngestStatus, PipelineStatus, SourceKind
from musahit.ingest import Ingester, IngestResult
from musahit.ingest.html import HtmlIngester
from musahit.ingest.reddit import RedditIngester
from musahit.ingest.resmi_gazete import ResmiGazeteIngester
from musahit.ingest.rss import RssIngester
from musahit.ingest.sources import SOURCES, Source

_log = get_logger("musahit.ingest.poller")

DEFAULT_MAX_CONCURRENT: int = 8
DEFAULT_TIMEOUT_SECONDS: float = 60.0
# Per-source timeout floor multiplier: a source with a 10s rate limit gets
# at least 120s before the poller pulls the plug.
_TIMEOUT_RATELIMIT_MULTIPLIER: int = 12

IngesterFactory = Callable[[Source], Ingester | None]


# ── Dispatch ────────────────────────────────────────────────────────────────


def get_ingester(
    source: Source,
    conn: duckdb.DuckDBPyConnection,
    settings: Settings | None = None,
) -> Ingester | None:
    """Return the ingester for ``source.kind`` or ``None`` for unknown kinds.

    Dispatches by enum value:

    * :attr:`SourceKind.RSS`     → :class:`RssIngester`
    * :attr:`SourceKind.HTML`    → :class:`HtmlIngester`
    * :attr:`SourceKind.PDF`     → :class:`ResmiGazeteIngester`
    * :attr:`SourceKind.API`     → :class:`RedditIngester`
    * :attr:`SourceKind.DEFERRED` → ``None`` (logged warning)
    * anything else              → ``None`` (logged warning)

    PDF is currently 1:1 with Resmî Gazete. If another PDF source enters
    the registry, dispatch by ``source.id`` here — do NOT silently fan
    every PDF source to the Gazette implementation.
    """
    log = _log.bind(source_id=source.id, kind=source.kind.value)
    if source.kind is SourceKind.RSS:
        return RssIngester(conn=conn)
    if source.kind is SourceKind.HTML:
        return HtmlIngester(conn=conn)
    if source.kind is SourceKind.PDF:
        # NOTE: PDF currently means Resmî Gazete only. If a second PDF
        # source is added, branch on source.id here.
        return ResmiGazeteIngester(conn=conn)
    if source.kind is SourceKind.API:
        return RedditIngester(conn=conn, settings=settings)
    log.warning("poller_no_ingester_for_kind")
    return None


# ── Poller ──────────────────────────────────────────────────────────────────


class IngestPoller:
    """Orchestrates a full ingest pass across every configured source.

    Constructor knobs (matching the project-wide DI pattern):

    * ``sources`` — defaults to the canonical :data:`SOURCES` tuple. Tests
      narrow it to a small subset.
    * ``ingester_factory`` — overrides the production :func:`get_ingester`
      dispatcher. Tests pass a fake factory that returns scripted
      :class:`Ingester` instances per source.
    * ``settings`` — propagated into :func:`get_ingester` so Reddit gets
      credentials. Ignored when ``ingester_factory`` is set.
    * ``max_concurrent`` — semaphore cap on simultaneous fetches.
    * ``default_timeout_seconds`` — per-source ceiling; bumped per source
      by the rate-limit multiplier.
    """

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        sources: tuple[Source, ...] = SOURCES,
        ingester_factory: IngesterFactory | None = None,
        settings: Settings | None = None,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        default_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._conn = conn
        self._sources = sources
        self._settings = settings
        if ingester_factory is None:
            ingester_factory = lambda source: get_ingester(  # noqa: E731
                source, conn=conn, settings=settings
            )
        self._factory: IngesterFactory = ingester_factory
        self._max_concurrent = max_concurrent
        self._default_timeout_seconds = default_timeout_seconds

    # ── Public entry point ──────────────────────────────────────────────

    async def run(self, run_id: str | None = None) -> dict[str, Any]:
        """Run the full ingest stage. Returns a small summary dict.

        Returned shape::

            {
                "run_id":  "<run id>",
                "total":   <sum of OK ingest counts>,
                "results": [(source_id, IngestResult), ...]
            }
        """
        run_id = run_id or self._default_run_id()
        log = _log.bind(run_id=run_id)
        log.info("poller_start", sources=len(self._sources))

        self._upsert_run_start(run_id)

        sem = asyncio.Semaphore(self._max_concurrent)
        tasks = [
            asyncio.create_task(self._run_one_source(run_id, source, sem))
            for source in self._sources
        ]
        pair_results = await asyncio.gather(*tasks, return_exceptions=False)

        total = sum(
            r.count for _, r in pair_results if r.status is IngestStatus.OK
        )
        self._finalize_run(run_id, total)
        log.info(
            "poller_done",
            total=total,
            ok=sum(1 for _, r in pair_results if r.status is IngestStatus.OK),
            failures=sum(1 for _, r in pair_results if r.status is not IngestStatus.OK),
        )
        return {"run_id": run_id, "total": total, "results": pair_results}

    # ── Per-source coroutine ────────────────────────────────────────────

    async def _run_one_source(
        self, run_id: str, source: Source, sem: asyncio.Semaphore
    ) -> tuple[str, IngestResult]:
        async with sem:
            return source.id, await self._fetch_one(run_id, source)

    async def _fetch_one(self, run_id: str, source: Source) -> IngestResult:
        log = _log.bind(run_id=run_id, source_id=source.id)
        ingester = self._factory(source)
        started_at = utcnow()

        if ingester is None:
            result = IngestResult(
                status=IngestStatus.SKIPPED,
                error="no ingester for source.kind",
            )
            self._write_ingest_log(
                run_id, source.id, started_at, utcnow(), result
            )
            return result

        timeout = max(
            self._default_timeout_seconds,
            float(source.rate_limit_seconds * _TIMEOUT_RATELIMIT_MULTIPLIER),
        )

        try:
            coro: Awaitable[IngestResult] = ingester.fetch(source)
            result = await asyncio.wait_for(coro, timeout=timeout)
        except TimeoutError:
            log.warning("poller_source_timeout", timeout_seconds=timeout)
            result = IngestResult(
                status=IngestStatus.TIMEOUT,
                error=f"poller timeout after {timeout:.0f}s",
            )
        except Exception as exc:
            # The Ingester Protocol promises not to raise for expected failures,
            # but defensive: catch anything (incl. DB errors) and record it.
            log.warning(
                "poller_source_exception",
                error=f"{type(exc).__name__}: {exc}",
            )
            result = IngestResult(
                status=IngestStatus.PARSE_ERROR,
                error=f"{type(exc).__name__}: {exc}",
            )

        completed_at = utcnow()
        self._write_ingest_log(run_id, source.id, started_at, completed_at, result)
        return result

    # ── Persistence helpers ─────────────────────────────────────────────

    def _upsert_run_start(self, run_id: str) -> None:
        """Insert or reset the ``pipeline_runs`` row to RUNNING for this run.

        Re-running an existing ``run_id`` resets ``started_at``,
        ``completed_at``, ``stages_done``, and ``counts`` — the row now
        represents the *current* attempt, not the historical first attempt.
        """
        started_at = utcnow()
        self._conn.execute(
            """
            INSERT INTO pipeline_runs (
                run_id, started_at, completed_at, status, stages_done, counts
            )
            VALUES (?, ?, NULL, ?, ?, ?)
            ON CONFLICT (run_id) DO UPDATE SET
                started_at   = excluded.started_at,
                completed_at = NULL,
                status       = excluded.status,
                stages_done  = excluded.stages_done,
                counts       = excluded.counts
            """,
            [
                run_id,
                started_at,
                PipelineStatus.RUNNING.value,
                json.dumps([]),
                json.dumps({}),
            ],
        )

    def _finalize_run(self, run_id: str, total: int) -> None:
        """After the gather settles, record stages_done and counts."""
        self._conn.execute(
            """
            UPDATE pipeline_runs
               SET stages_done = ?, counts = ?
             WHERE run_id = ?
            """,
            [
                json.dumps(["ingest"]),
                json.dumps({"articles": total}),
                run_id,
            ],
        )

    def _write_ingest_log(
        self,
        run_id: str,
        source_id: str,
        started_at: datetime,
        completed_at: datetime,
        result: IngestResult,
    ) -> None:
        """Insert (or replace) the ingest_log row for this (run_id, source_id)."""
        self._conn.execute(
            """
            INSERT INTO ingest_log (
                run_id, source_id, started_at, completed_at,
                status, articles_fetched, error_detail
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id, source_id) DO UPDATE SET
                started_at       = excluded.started_at,
                completed_at     = excluded.completed_at,
                status           = excluded.status,
                articles_fetched = excluded.articles_fetched,
                error_detail     = excluded.error_detail
            """,
            [
                run_id,
                source_id,
                started_at,
                completed_at,
                result.status.value,
                result.count,
                result.error,
            ],
        )

    # ── Misc ────────────────────────────────────────────────────────────

    def _default_run_id(self) -> str:
        return "run_" + tr_local_date().isoformat().replace("-", "")


__all__ = [
    "DEFAULT_MAX_CONCURRENT",
    "DEFAULT_TIMEOUT_SECONDS",
    "IngestPoller",
    "IngesterFactory",
    "get_ingester",
]

"""Source polling and ingestion modules.

Defines the :class:`Ingester` protocol that every per-kind implementation
(`rss`, `html`, `pdf`, `api`, …) must satisfy, plus the :class:`IngestResult`
return type referenced by the orchestrator in ADR-012 § Failure isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from musahit.common.types import IngestStatus
from musahit.ingest.sources import Source

# User-Agent string sent on every outgoing fetch. ADR-003 § Polite scraping.
USER_AGENT: str = "MUSAHIT/0.1"


@dataclass(frozen=True)
class IngestResult:
    """Per-source ingest outcome (ADR-012 § Failure isolation).

    Attributes:
        status: One of the IngestStatus enum members.
        count: Number of new rows written to ``raw_articles``. Always ``0`` for
            non-OK statuses; may be ``0`` for OK if the feed had no entries.
        error: Optional human-readable error detail; ``None`` on success.
    """

    status: IngestStatus
    count: int = 0
    error: str | None = None


class Ingester(Protocol):
    """Protocol every per-kind ingester implements.

    Implementations own their own HTTP client, parser, and persistence path.
    The orchestrator iterates :data:`SOURCES`, dispatches by ``source.kind``,
    and aggregates :class:`IngestResult` values into the ``ingest_log`` table.

    Per ADR-012, ``fetch`` MUST NOT raise for expected failures (timeouts,
    HTTP errors, parse errors). It returns a structured :class:`IngestResult`
    so a single broken source does not abort the run.
    """

    async def fetch(self, source: Source) -> IngestResult: ...


__all__ = ["USER_AGENT", "IngestResult", "Ingester", "Source"]

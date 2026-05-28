"""Embedding clients — production (Ollama / bge-m3) and test (deterministic fake).

The :class:`EmbeddingClient` Protocol is the seam every downstream stage
will use to acquire vectors. Step 10 introduces it; the score stage
(step 11) and arc-link stage (step 12) reuse the same Protocol with the
same :class:`FakeEmbeddingClient` for tests.

Per ADR-002, the production embedder is bge-m3 served by a local Ollama
instance on ``http://localhost:11434``. Embeddings are 1024-dimensional
floats; the cluster_embeddings / article_embeddings columns are
``FLOAT[1024]`` (ADR-006).
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from typing import Any, Protocol

import httpx

from musahit.common.logging import get_logger

_log = get_logger("musahit.cluster.embedder")

DIMENSION: int = 1024
DEFAULT_OLLAMA_URL: str = "http://localhost:11434"
DEFAULT_MODEL: str = "bge-m3"
# The pre-Issue-2 default was 50. Lowered to 16 after the 2026-05-28
# Ollama-OOM incident · smaller per-call batches reduce the peak memory
# footprint when bge-m3 is loaded alongside other 4-5GB models. Per-batch
# retries + adaptive halving handle the rare batches that still 500.
DEFAULT_BATCH_SIZE: int = 16
DEFAULT_TIMEOUT_SECONDS: float = 60.0
DEFAULT_MAX_RETRIES: int = 3
DEFAULT_BACKOFF_BASE_SECONDS: float = 2.0


# ── Protocol ───────────────────────────────────────────────────────────────


class EmbeddingClient(Protocol):
    """Async embedding API. Returns one 1024-dim vector per input text."""

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


# ── Ollama implementation ──────────────────────────────────────────────────


class OllamaEmbeddingClient:
    """Production embedder: POSTs batches to Ollama's ``/api/embed``.

    Ollama's modern embed endpoint (since v0.1.30+) accepts an ``input``
    list and returns matching ``embeddings``. The client slices the
    caller's full ``texts`` list into ``batch_size``-chunks (default 16
    after the 2026-05-28 OOM incident, down from 50). Each batch is
    retried with exponential backoff on transient errors (HTTP 5xx,
    timeout, connection); a batch that still fails after retries is
    halved recursively down to single items. A single item that fails
    every retry raises an :class:`httpx.HTTPError` (or the originating
    exception) so the Clusterer's guard surfaces a clean
    EmbeddingUnavailableError.

    Order preservation: batches are processed sequentially; halving
    splits a slice in place and recursively embeds each half, then
    concatenates left+right · the output position of every input text
    is preserved.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_URL,
        model: str = DEFAULT_MODEL,
        batch_size: int = DEFAULT_BATCH_SIZE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._batch_size = batch_size
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds
        self._injected_client = client

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        if self._injected_client is not None:
            return await self._embed_with(self._injected_client, texts)
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            return await self._embed_with(client, texts)

    async def _embed_with(
        self, client: httpx.AsyncClient, texts: list[str]
    ) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            vectors = await self._embed_batch_resilient(client, batch)
            out.extend(vectors)
        return out

    async def _embed_batch_resilient(
        self, client: httpx.AsyncClient, batch: list[str]
    ) -> list[list[float]]:
        """Try the batch with retries; on persistent failure, halve and recurse.

        Recursive base case: a single-item batch that still fails after
        all retries · re-raises the last exception so the caller's
        length-mismatch guard fires.
        """
        try:
            return await self._embed_batch_with_retry(client, batch)
        except Exception as exc:
            if len(batch) <= 1:
                _log.warning(
                    "embed_item_failed",
                    item_index_in_batch=0,
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise
            mid = len(batch) // 2
            _log.warning(
                "embed_batch_halved",
                batch_size=len(batch),
                left_size=mid,
                right_size=len(batch) - mid,
                error=f"{type(exc).__name__}: {exc}",
            )
            left = await self._embed_batch_resilient(client, batch[:mid])
            right = await self._embed_batch_resilient(client, batch[mid:])
            return left + right

    async def _embed_batch_with_retry(
        self, client: httpx.AsyncClient, batch: list[str]
    ) -> list[list[float]]:
        """One batch · 1 + max_retries attempts · exponential backoff."""
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await self._post_batch(client, batch)
            except (httpx.HTTPStatusError, httpx.TransportError,
                    httpx.TimeoutException) as exc:
                last_exc = exc
                if not _is_retryable(exc):
                    raise
                if attempt < self._max_retries:
                    sleep_for = self._backoff_base_seconds * (2 ** attempt)
                    _log.warning(
                        "embed_batch_retry",
                        attempt=attempt,
                        batch_size=len(batch),
                        sleep_seconds=sleep_for,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    await asyncio.sleep(sleep_for)
                    continue
                raise
        # Defensive · should be unreachable; satisfies the type checker.
        assert last_exc is not None
        raise last_exc

    async def _post_batch(
        self, client: httpx.AsyncClient, batch: list[str]
    ) -> list[list[float]]:
        response = await client.post(
            f"{self._base_url}/api/embed",
            json={"model": self._model, "input": batch},
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data["embeddings"]


def _is_retryable(exc: Exception) -> bool:
    """Transient errors worth retrying: 5xx, timeout, connection failures.

    4xx (except 408/429) are caller bugs — retrying won't help, so they
    surface immediately.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status >= 500 or status in (408, 429)
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


# ── Fake (testing) implementation ──────────────────────────────────────────


class FakeEmbeddingClient:
    """Deterministic, network-free embedder for tests.

    Strategy: bag-of-words over whitespace tokens. Each token hashes to a
    fixed bucket in the 1024-dim vector and contributes a unit weight.
    The vector is L2-normalised at the end so two embeddings that share
    a token set produce a high cosine.

    Properties this gives:

    * Determinism — identical text → identical vector across runs.
    * Similarity preservation — texts sharing 80%+ tokens have cosine
      ≥ 0.8; disjoint texts have cosine < 0.5 (typically ≈ 0).
    * 1024-dim — matches the production embedder so the same DB schema
      and same cosine threshold are used in tests.

    Downstream Ollama-using stages (score, arc-link, write) should reuse
    this fake in their own tests — keep the pattern uniform.
    """

    def __init__(self, dimension: int = DIMENSION) -> None:
        self._dimension = dimension

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self._dimension
        for token in (text or "").lower().split():
            digest = hashlib.md5(token.encode("utf-8"), usedforsecurity=False).hexdigest()
            idx = int(digest[:8], 16) % self._dimension
            vec[idx] += 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0.0:
            vec = [x / norm for x in vec]
        return vec


__all__ = [
    "DEFAULT_BACKOFF_BASE_SECONDS",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_MODEL",
    "DEFAULT_OLLAMA_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "DIMENSION",
    "EmbeddingClient",
    "FakeEmbeddingClient",
    "OllamaEmbeddingClient",
]

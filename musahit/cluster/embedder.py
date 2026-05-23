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

import hashlib
import math
from typing import Any, Protocol

import httpx

DIMENSION: int = 1024
DEFAULT_OLLAMA_URL: str = "http://localhost:11434"
DEFAULT_MODEL: str = "bge-m3"
DEFAULT_BATCH_SIZE: int = 50
DEFAULT_TIMEOUT_SECONDS: float = 60.0


# ── Protocol ───────────────────────────────────────────────────────────────


class EmbeddingClient(Protocol):
    """Async embedding API. Returns one 1024-dim vector per input text."""

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


# ── Ollama implementation ──────────────────────────────────────────────────


class OllamaEmbeddingClient:
    """Production embedder: POSTs batches to Ollama's ``/api/embed``.

    Ollama's modern embed endpoint (since v0.1.30+) accepts an ``input``
    list and returns matching ``embeddings``. The client batches the
    caller's full ``texts`` list into ``batch_size``-chunks so a 500-
    article night becomes ~10 HTTP calls rather than one big payload.

    Failures bubble up — the Clusterer is the layer that decides whether
    to translate them into IngestStatus-style outcomes for the run
    summary.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_URL,
        model: str = DEFAULT_MODEL,
        batch_size: int = DEFAULT_BATCH_SIZE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._batch_size = batch_size
        self._timeout_seconds = timeout_seconds
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
            response = await client.post(
                f"{self._base_url}/api/embed",
                json={"model": self._model, "input": batch},
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            out.extend(data["embeddings"])
        return out


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
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_MODEL",
    "DEFAULT_OLLAMA_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "DIMENSION",
    "EmbeddingClient",
    "FakeEmbeddingClient",
    "OllamaEmbeddingClient",
]
